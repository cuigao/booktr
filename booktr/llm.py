"""LLM 适配层：OpenAI 兼容接口 + mock 模式。

provider: openai-compatible | mock
- openai-compatible: base_url + api_key(env) + model，支持任意兼容服务
- mock: 不调用网络，返回确定性结果用于管线测试
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import requests

from .config import Config

log = logging.getLogger("booktr.llm")


class LLMError(RuntimeError):
    """LLM 调用错误。可携带诊断信息（诊断失败时模型"纠结"的内容）。

    reasoning：失败前已累加的思考内容（若有）；finish_reason/usage 同理。
    这些字段供失败日志记录，便于事后诊断。
    """

    def __init__(self, message: str = "", reasoning: str = "",
                 finish_reason: str | None = None, usage: dict | None = None):
        super().__init__(message)
        self.reasoning = reasoning
        self.finish_reason = finish_reason
        self.usage = usage or {}


class LLMRepetitionError(LLMError):
    """检测到 LLM 输出陷入周期性重复（循环）。

    携带已累加的部分正文/思考与循环特征（period/repeats/fired_at_chars），
    供上层记录并"同参数重试"。作为 LLMError 子类，未特殊处理时会退化为一
    般的失败日志；``_request`` 会优先捕获本类并走独立的循环重试计数。
    """

    def __init__(self, message: str = "", reasoning: str = "",
                 finish_reason: str | None = None, usage: dict | None = None,
                 content: str = "", period: int = 0, repeats: int = 0,
                 fired_at_chars: int = 0, fragment: str = ""):
        super().__init__(message, reasoning=reasoning,
                         finish_reason=finish_reason, usage=usage)
        self.content = content
        self.period = period
        self.repeats = repeats
        self.fired_at_chars = fired_at_chars
        self.fragment = fragment


class LLMTimeoutError(LLMError):
    """单个调用超过总时长上限（wall-clock call timeout）。

    覆盖"一直在输出、但不结束"的意外超长响应（非周期性，loop guard 抓不到；
    持续有分块，read timeout 抓不到）。携带已累加的部分内容与 elapsed/limit。
    作为 LLMError 子类：未特殊处理时退化为一般失败；``_request`` 会优先捕获并
    走独立的调用重试计数。
    """

    def __init__(self, message: str = "", reasoning: str = "",
                 finish_reason: str | None = None, usage: dict | None = None,
                 content: str = "", elapsed_s: float = 0.0, limit_s: float = 0.0):
        super().__init__(message, reasoning=reasoning,
                         finish_reason=finish_reason, usage=usage)
        self.content = content
        self.elapsed_s = elapsed_s
        self.limit_s = limit_s


# JSON 机械修复方法（parse_json_response 后处理），规范字段 repair_methods 的取值。
REPAIR_METHOD_ESCAPE = "ESCAPE_VALUE_STRINGS"  # 值字符串转义（未转义引号 / 裸换行）
REPAIR_METHOD_CLOSE_ARRAY = "CLOSE_ARRAY"  # 数组括号闭合修复（缺 ]，已知 key 先验补全）

# 数组闭合修复时用于定位"数组被提前截断"的已知结构 key（已知的先验）。
# 这些 key 出现在数组元素结束后本应闭合数组的位置，却被 LLM 直接写成了字符串元素。
_ARRAY_KNOWN_KEYS = (
    "needs_human", "confidence", "glossary_conflicts", "notes", "translation",
)


_WS_RE = re.compile(r"\s+")


def _has_word(block: str) -> bool:
    """块内是否含至少一个字母/数字/汉字（防纯标点、纯空白连发被误判为循环）。"""
    for ch in block:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _kmp_min_period(s: str) -> int:
    """KMP 失配函数求最小周期 P（O(n)，与 P 大小无关）。

    P = len(s) - failure[-1]；若 s 无周期则 P == len(s)。
    """
    n = len(s)
    if n == 0:
        return 0
    fail = [0] * n
    for i in range(1, n):
        j = fail[i - 1]
        while j > 0 and s[i] != s[j]:
            j = fail[j - 1]
        if s[i] == s[j]:
            j += 1
        fail[i] = j
    return n - fail[-1]


def find_repetition(text: str, window: int = 16384, min_repeats: int = 2,
                    min_span: int = 2048, norm_ws: bool = True) -> dict | None:
    """检测文本**末尾窗口**是否陷入周期性重复（循环）；是则返回循环特征。

    仅识别"精确周期重复"（空白折叠后逐字符相同）——这是模型被上下文片段卡住
    的机械特征。判定用 KMP 最小周期，与周期大小无关（可捕获 5 字的 ``Hmm.``
    到 7k+ 字的超长块）。

    判定条件（全部满足）：
    - ``len(text) >= window``（窗口须满，保证至少能看到 2 次重复）；
    - 最小周期 ``P`` 满足 ``0 < P < n``；
    - ``repeats = n // P >= min_repeats``；
    - ``P * repeats >= min_span``（重复段总长下限；短周期需更多次才成立，
      如 ``Hmm.`` 需连续 4 百多次，而 7k 长块 2 次即可）；
    - 块内至少含一个字母/数字/汉字（``_has_word``）。

    返回 ``{period, repeats, span, fragment, window}``；不构成循环返回 None。
    """
    if not text or len(text) < window:
        return None
    tail = text[-window:]
    if norm_ws:
        tail = _WS_RE.sub(" ", tail).rstrip()
    n = len(tail)
    if n < 2:
        return None
    period = _kmp_min_period(tail)
    if period <= 0 or period >= n:
        return None
    repeats = n // period
    span = repeats * period
    if repeats < min_repeats or span < min_span:
        return None
    if not _has_word(tail[:period]):
        return None
    return {"period": period, "repeats": repeats, "span": span,
            "fragment": tail[:period][:120], "window": window}


class LLMClient:
    def __init__(self, cfg: Config, llm_override: dict | None = None,
                 log_enabled: bool = True, run_tag: str = ""):
        """llm_override：覆盖 llm 配置（如 qa.supervisor），仅替换给定键。

        非 None 的覆盖值优先；``api_key_required`` 为 None 时表示沿用主配置。
        log_enabled=False 时本 client 的调用不写 llm_logs（用于体量很大的判官调用）。
        run_tag：可选运行标识（如实验的 variant/run），写入每条日志的 ``run`` 字段，
        便于将日志直接关联到具体运行，无需按时间/内容猜测。
        """
        self.cfg = cfg
        self.log_enabled = bool(log_enabled)
        self.run_tag = run_tag or ""
        llm = dict(cfg.get("llm", default={}))
        if llm_override:
            over = {k: v for k, v in llm_override.items() if v is not None}
            # 空字符串视为"未设置"，继承主配置（api_key 例外：允许显式清空）
            over = {k: v for k, v in over.items()
                    if v != "" or k == "api_key"}
            llm.update(over)
        self.provider = llm.get("provider", "mock")
        self.base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
        self.model = llm.get("model", "gpt-4o-mini")
        api_env = llm.get("api_key_env", "BOOKTR_API_KEY")
        # 优先级：config 直接值 api_key > 环境变量 api_key_env
        self.api_key = llm.get("api_key") or os.environ.get(api_env, "")
        self.api_key_required = bool(llm.get("api_key_required", True))
        self.temperature = llm.get("temperature", 0.3)
        self.max_tokens = llm.get("max_tokens", 4096)
        self.max_tokens_ceiling = llm.get("max_tokens_ceiling", 524288)
        self.timeout = llm.get("timeout", 120)
        self.connect_timeout = llm.get("connect_timeout", 20)
        self.stream = bool(llm.get("stream", True))
        self.max_retries = llm.get("max_retries", 3)
        # 输出循环（周期性重复）防护
        self.loop_guard = bool(llm.get("loop_guard", True))
        self.loop_window = int(llm.get("loop_window", 16384))
        self.loop_min_repeats = int(llm.get("loop_min_repeats", 2))
        self.loop_min_span = int(llm.get("loop_min_span", 2048))
        self.loop_check_every = int(llm.get("loop_check_every", 512))
        self.loop_retries = int(llm.get("loop_retries", 2))
        self.loop_temp_bump = float(llm.get("loop_temp_bump", 0.1))
        self.loop_norm = bool(llm.get("loop_norm", True))
        # 单调用总时长上限（wall-clock）：覆盖"一直输出但不结束"的意外超长响应。
        # 0 = 关闭。可按 tag 前缀覆盖（如 qa 更长）。
        self.max_call_seconds = int(llm.get("max_call_seconds", 300))
        self.max_call_seconds_by_tag = llm.get(
            "max_call_seconds_by_tag", {"qa": 1200, "term": 600}) or {}
        self.call_retries = int(llm.get("call_retries", 1))
        self.rpm = llm.get("max_requests_per_minute", 60)
        self._min_interval = 60.0 / max(self.rpm, 1)
        self._last_call = 0.0
        self._stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float | None = None,
             tag: str = "chat", reasoning_effort: str | None = None,
             on_delta=None) -> str:
        """单轮对话，返回文本。每次调用（含 mock）都完整记录到 llm_logs。

        reasoning_effort：推理模型思考等级（OpenAI 规范字段，如 "none"/"low"/
        "high"）；空则请求体不含该字段。
        on_delta(kind, text)：流式实时回调（kind ∈ {"content","reasoning"}），
        用于诊断（区分"服务停滞"与"网络切断"）。"""
        t0 = time.monotonic()
        if self.provider == "mock":
            resp = self._mock(system, user)
            self._log(tag, system, user, resp, ok=True,
                      duration_ms=(time.monotonic() - t0) * 1000)
            return resp
        if self.api_key_required and not self.api_key:
            err = (f"未设置 API key（可在 config.json 的 llm.api_key 直接填写，"
                   f"或设置环境变量 {self.cfg.get('llm','api_key_env',default='BOOKTR_API_KEY')}；"
                   "本地免 key 服务可将 llm.api_key_required 设为 false）。")
            self._log(tag, system, user, "", ok=False, error=err,
                      duration_ms=(time.monotonic() - t0) * 1000)
            raise LLMError(err)
        diag: dict = {}
        try:
            resp, usage, reasoning = self._openai_chat(system, user, temperature,
                                                       reasoning_effort=reasoning_effort,
                                                       on_delta=on_delta, diag=diag,
                                                       tag=tag)
        except LLMError as e:
            self._log(tag, system, user, "", ok=False, error=str(e),
                      reasoning=getattr(e, "reasoning", ""),
                      finish_reason=getattr(e, "finish_reason", None),
                      usage=getattr(e, "usage", None),
                      duration_ms=(time.monotonic() - t0) * 1000, diag=diag)
            raise
        self._log(tag, system, user, resp, ok=True, usage=usage, reasoning=reasoning,
                  duration_ms=(time.monotonic() - t0) * 1000, diag=diag)
        return resp

    # ------------------------------------------------------------------
    def chat_multi(self, messages: list[dict], temperature: float | None = None,
                   tag: str = "chat_multi", task_id: str = "",
                   context_id: str = "", reasoning_effort: str | None = None,
                   on_delta=None) -> str:
        """多轮对话，messages = [{"role": "system"|"user"|"assistant", "content": ...}]。

        返回最后一条 assistant 消息的文本。完整记录到 llm_logs。
        on_delta(kind, text)：流式实时回调（见 chat）。
        """
        t0 = time.monotonic()
        system = ""
        user_history = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_history.append(m)
        # 日志：仅记录最后一条 user 消息作为代表
        last_user = next((m["content"] for m in reversed(user_history)
                          if m["role"] == "user"), "")
        if self.provider == "mock":
            resp = self._mock(system, last_user)
            self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                      resp, ok=True, duration_ms=(time.monotonic() - t0) * 1000,
                      messages=messages, task_id=task_id, context_id=context_id)
            return resp
        if self.api_key_required and not self.api_key:
            err = (f"未设置 API key（可在 config.json 的 llm.api_key 直接填写，"
                   f"或设置环境变量 {self.cfg.get('llm','api_key_env',default='BOOKTR_API_KEY')}；"
                   "本地免 key 服务可将 llm.api_key_required 设为 false）。")
            self._log(tag, system, f"[{len(messages)} msgs]", "",
                      ok=False, error=err,
                      duration_ms=(time.monotonic() - t0) * 1000,
                      messages=messages, task_id=task_id, context_id=context_id)
            raise LLMError(err)
        diag: dict = {}
        try:
            resp, usage, reasoning = self._openai_chat_multi(messages, temperature,
                                                             reasoning_effort=reasoning_effort,
                                                             on_delta=on_delta, diag=diag,
                                                             tag=tag)
        except LLMError as e:
            self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                      "", ok=False, error=str(e),
                      reasoning=getattr(e, "reasoning", ""),
                      finish_reason=getattr(e, "finish_reason", None),
                      usage=getattr(e, "usage", None),
                      duration_ms=(time.monotonic() - t0) * 1000,
                      messages=messages, task_id=task_id, context_id=context_id,
                      diag=diag)
            raise
        self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                  resp, ok=True, usage=usage, reasoning=reasoning,
                  duration_ms=(time.monotonic() - t0) * 1000,
                  messages=messages, task_id=task_id, context_id=context_id,
                  diag=diag)
        return resp

    def _openai_chat_multi(self, messages: list[dict],
                           temperature: float | None,
                           reasoning_effort: str | None = None,
                           on_delta=None, diag: dict | None = None,
                           tag: str = "") -> tuple[str, dict, str]:
        """多轮对话底层调用。返回 (content, usage, reasoning)。"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        return self._request(body, "multi", on_delta=on_delta, diag=diag, tag=tag)

    # ------------------------------------------------------------------
    def _openai_chat(self, system: str, user: str, temperature: float | None,
                     reasoning_effort: str | None = None,
                     on_delta=None, diag: dict | None = None,
                     tag: str = "") -> tuple[str, dict, str]:
        """单轮对话底层调用。返回 (content, usage, reasoning)。"""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        return self._request(body, "single", on_delta=on_delta, diag=diag, tag=tag)

    def _record_loop_abort(self, diag: dict, e: "LLMRepetitionError",
                           t_abort: float) -> None:
        """把一次循环中止记入诊断（供日志/失败信息留存完整异常轮次）。"""
        diag.setdefault("loop_aborts", []).append({
            "period": getattr(e, "period", 0),
            "repeats": getattr(e, "repeats", 0),
            "fired_at_chars": getattr(e, "fired_at_chars", 0),
            "fragment": getattr(e, "fragment", ""),
            "finish_reason": getattr(e, "finish_reason", None),
            "content": getattr(e, "content", "")[:2000],
            "reasoning": (getattr(e, "reasoning", "") or "")[-20000:],
            "elapsed_s": round(time.monotonic() - t_abort, 1),
        })

    def _record_wall_abort(self, diag: dict, e: "LLMTimeoutError",
                           t_abort: float) -> None:
        """把一次总时长超限中止记入诊断（供日志/失败信息留存异常轮次）。"""
        diag.setdefault("wall_aborts", []).append({
            "elapsed_s": round(getattr(e, "elapsed_s", 0.0), 1),
            "limit_s": round(getattr(e, "limit_s", 0.0), 1),
            "finish_reason": getattr(e, "finish_reason", None),
            "content": getattr(e, "content", "")[:2000],
            "reasoning": (getattr(e, "reasoning", "") or "")[-20000:],
            "recorded_s": round(time.monotonic() - t_abort, 1),
        })

    def _request(self, base_body: dict, kind: str, on_delta=None,
                 diag: dict | None = None, tag: str = "") -> tuple[str, dict, str]:
        """统一请求入口：流式（默认）或非流式。

        重试分两类、互不占用预算：
        - **网络/格式重试**：requests 异常 / HTTP / 解析类，退避重试至 ``max_retries``；
        - **循环重试**：检测到周期性重复（``LLMRepetitionError``）后以**相同参数**
          （仅按 ``loop_temp_bump`` 递增 temperature）重试至 ``loop_retries`` 次，
          用于打破模型的自我锚定。
        另有"正文被 reasoning 截空"时翻倍 max_tokens 重试一次。

        诊断累积写入 ``diag``（若提供），由 ``_log`` 留存完整异常轮次。
        返回 (content, usage, reasoning)。
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url + "/chat/completions"
        last_err: Exception | None = None
        doubled = False  # 是否已因 length 空正文而翻倍重试
        net_attempts = 0
        loop_aborts = 0
        call_aborts = 0
        empty_aborts = 0
        # 诊断：尽力记录失败前已累加的 reasoning / finish_reason / usage（供失败日志）
        last_reasoning = ""
        last_finish: str | None = None
        last_usage: dict = {}
        while True:
            self._rate_limit()
            body = dict(base_body)
            # 循环重试：按次数递增温度，帮助跳出锚定（上限 base+0.3）
            if loop_aborts > 0:
                base_t = base_body.get("temperature", self.temperature)
                body["temperature"] = min(base_t + self.loop_temp_bump * loop_aborts,
                                          base_t + 0.3)
            t_abort = time.monotonic()
            try:
                if self.stream:
                    content, reasoning, usage, finish = self._post_stream(
                        url, headers, body, on_delta=on_delta,
                        diag=diag if diag is not None else {}, tag=tag)
                else:
                    content, reasoning, usage, finish = self._post_once(
                        url, headers, body, tag=tag)
            except LLMRepetitionError as e:
                # 循环中止：走独立计数，不消耗网络重试预算
                last_err = e
                if diag is not None:
                    self._record_loop_abort(diag, e, t_abort)
                if loop_aborts < self.loop_retries:
                    loop_aborts += 1
                    log.warning("LLM 输出陷入循环（period=%s repeats=%s @%s字符），"
                                "调整 temperature 重试 %s/%s",
                                getattr(e, "period", "?"), getattr(e, "repeats", "?"),
                                getattr(e, "fired_at_chars", "?"),
                                loop_aborts, self.loop_retries)
                    continue
                break
            except LLMTimeoutError as e:
                # 单调用总时长超限：独立计数，同参数重试（不消耗网络/循环预算）
                last_err = e
                if diag is not None:
                    self._record_wall_abort(diag, e, t_abort)
                if call_aborts < self.call_retries:
                    call_aborts += 1
                    log.warning("LLM 调用超时（%.0fs > %.0fs上限），同参数重试 %s/%s",
                                getattr(e, "elapsed_s", 0), getattr(e, "limit_s", 0),
                                call_aborts, self.call_retries)
                    continue
                break
            except (requests.RequestException, ValueError, LLMError) as e:
                last_err = e
                # 网络/HTTP/格式类错误：尽量带出已累加的诊断信息（若异常未携带）
                if not getattr(e, "reasoning", ""):
                    e.reasoning = last_reasoning
                if getattr(e, "finish_reason", None) is None:
                    e.finish_reason = last_finish
                if not getattr(e, "usage", None):
                    e.usage = last_usage
                # 网络/HTTP/格式类错误：正常退避重试
                if net_attempts >= self.max_retries:
                    break
                delay = 2 ** net_attempts
                net_attempts += 1
                log.warning("LLM 调用失败(%s)，%.1fs 后重试: %s", net_attempts, delay, last_err)
                time.sleep(delay)
                continue

            last_reasoning, last_finish, last_usage = reasoning, finish, usage

            # 正文为空且被截断（reasoning 吃满预算）→ 翻倍 max_tokens 重试一次
            if not content and finish == "length" and not doubled:
                doubled = True
                old = base_body.get("max_tokens", self.max_tokens)
                new = min(max(old * 2, 1), self.max_tokens_ceiling)
                if new > old:
                    base_body = {**base_body, "max_tokens": new}
                    log.warning("LLM 正文被 reasoning 截空，max_tokens %s→%s 重试", old, new)
                    continue
            if content:
                return content, usage, reasoning
            # 有 reasoning 但无正文 / 其它空内容
            rl = len(reasoning or "")
            if finish == "length":
                last_err = LLMError(
                    f"LLM 正文被截断为空（finish_reason=length，reasoning {rl} 字符，"
                    f"max_tokens={base_body.get('max_tokens', self.max_tokens)}）；"
                    "已尝试翻倍仍不足，请提高 llm.max_tokens_ceiling",
                    reasoning=reasoning, finish_reason=finish, usage=usage)
                break
            # 其它空内容（finish=None/stop 等）：同参数重试一次（复用 call_retries 预算）
            last_err = LLMError(f"LLM 返回空内容（finish_reason={finish}）",
                                reasoning=reasoning, finish_reason=finish, usage=usage)
            if empty_aborts < self.call_retries:
                empty_aborts += 1
                log.warning("LLM 返回空内容（finish_reason=%s），同参数重试 %s/%s",
                            finish, empty_aborts, self.call_retries)
                continue
            break
        if isinstance(last_err, LLMError):
            if diag is not None and diag.get("loop_aborts") and not getattr(
                    last_err, "loop_aborts", None):
                last_err.loop_aborts = diag["loop_aborts"]
            if diag is not None and diag.get("wall_aborts") and not getattr(
                    last_err, "wall_aborts", None):
                last_err.wall_aborts = diag["wall_aborts"]
            raise last_err
        raise LLMError(f"LLM 调用最终失败: {last_err}",
                       reasoning=last_reasoning, finish_reason=last_finish,
                       usage=last_usage)

    def _call_cap(self, tag: str) -> float:
        """给定 tag 的单调用总时长上限（秒）；0 表示不限制。

        按 ``tag`` 前缀（``split("_")[0]``，如 ``qa`` / ``term`` / ``judge``）
        在 ``max_call_seconds_by_tag`` 中查找覆盖，否则用全局 ``max_call_seconds``。
        """
        prefix = (tag or "").split("_")[0]
        cap = self.max_call_seconds_by_tag.get(prefix)
        if cap is None:
            cap = self.max_call_seconds
        try:
            return float(cap)
        except (TypeError, ValueError):
            return float(self.max_call_seconds)

    def _detect_loop(self, content: str, reasoning: str) -> dict | None:
        """对已得的 content/reasoning 做循环检测（非流式用；流式在过程中检测）。"""
        if not self.loop_guard:
            return None
        for field in (reasoning, content):
            h = find_repetition(field, window=self.loop_window,
                                min_repeats=self.loop_min_repeats,
                                min_span=self.loop_min_span, norm_ws=self.loop_norm)
            if h:
                h["field"] = "reasoning" if field is reasoning else "content"
                return h
        return None

    def _post_once(self, url: str, headers: dict, body: dict,
                   tag: str = "") -> tuple[str, str, dict, str]:
        """非流式：返回 (content, reasoning, usage, finish_reason)。

        非流式无中途检测机会，故以 read timeout 兼作总时长上限：
        ``min(self.timeout, cap)``（cap=0 时不设上限）。
        """
        cap = self._call_cap(tag)
        tmo = self.timeout if not cap else min(self.timeout, cap)
        r = requests.post(url, headers=headers, json=body, timeout=tmo)
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = self._record(data)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"API 响应格式异常: {e}")
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or ""
        h = self._detect_loop(content, reasoning)
        if h:
            raise LLMRepetitionError(
                f"LLM 输出陷入循环（非流式，period={h['period']} repeats={h['repeats']}）",
                reasoning=reasoning, finish_reason=choice.get("finish_reason"),
                usage=usage, content=content, period=h["period"],
                repeats=h["repeats"], fired_at_chars=len(reasoning) or len(content),
                fragment=h["fragment"])
        return (content, reasoning, usage, choice.get("finish_reason"))

    def _post_stream(self, url: str, headers: dict, body: dict,
                     on_delta=None, diag: dict | None = None,
                     tag: str = "") -> tuple[str, str, dict, str]:
        """流式：逐块累加 content/reasoning，返回 (content, reasoning, usage, finish)。

        流式下每个 chunk 都会重置读取超时，长思考不再误判为网络超时。
        on_delta(kind, text) 为可选的实时回调（kind ∈ {"content","reasoning"}），
        供未来实时输出使用。

        两层防护（互补）：
        - **read timeout**（``timeout=(connect, self.timeout)``，默认 120s/间隔）：
          只对"完全无数据的停顿"生效（真正网络卡死），合法长响应因持续有分块而不触发。
        - **call timeout**（``self._call_cap(tag)``，默认 300s，qa/term 更长）：逐**SSE
          分块**核对总时长（不能按字符计数——静默/滴答流可能不产生字符），超过即以
          ``LLMTimeoutError`` 提前关闭连接止损。
        - **loop guard**：每累加约 ``loop_check_every`` 字符检测周期性重复，命中抛
          ``LLMRepetitionError``。
        """
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        cap = self._call_cap(tag)
        read_tmo = self.timeout if not cap else min(self.timeout, cap)
        t_start = time.monotonic()
        r = requests.post(url, headers=headers, json=body,
                          timeout=(self.connect_timeout, read_tmo), stream=True)
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict = {}
        finish: str | None = None
        n_reason = n_content = 0
        next_check = self.loop_check_every if self.loop_guard else 0
        try:
            for raw in r.iter_lines():
                # 总时长核对：逐分块检查（流式滴答/静默时字符数不增，故不能按字符门控）
                if cap and (time.monotonic() - t_start) > cap:
                    raise LLMTimeoutError(
                        f"LLM 调用超时（已 {time.monotonic() - t_start:.0f}s > "
                        f"{cap:.0f}s 上限）",
                        reasoning="".join(reasoning_parts), finish_reason=finish,
                        usage=usage, content="".join(content_parts),
                        elapsed_s=time.monotonic() - t_start, limit_s=cap)
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    d = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if d.get("usage"):
                    usage = self._record(d)
                choices = d.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}
                rc = delta.get("reasoning")
                if rc:
                    reasoning_parts.append(rc)
                    n_reason += len(rc)
                    if on_delta:
                        on_delta("reasoning", rc)
                cc = delta.get("content")
                if cc:
                    content_parts.append(cc)
                    n_content += len(cc)
                    if on_delta:
                        on_delta("content", cc)
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                # 循环检测（每 loop_check_every 字符一次）
                if self.loop_guard and (n_reason + n_content) >= next_check:
                    next_check = (n_reason + n_content) + self.loop_check_every
                    h = self._detect_loop("".join(content_parts),
                                          "".join(reasoning_parts))
                    if h:
                        raise LLMRepetitionError(
                            f"LLM 输出陷入循环（period={h['period']} "
                            f"repeats={h['repeats']}，field={h.get('field')}）",
                            reasoning="".join(reasoning_parts), finish_reason=finish,
                            usage=usage, content="".join(content_parts),
                            period=h["period"], repeats=h["repeats"],
                            fired_at_chars=n_reason + n_content,
                            fragment=h["fragment"])
        except requests.RequestException as e:
            # 流式中途网络中断：尽量带出已累加的 reasoning，供失败日志诊断
            raise LLMError(f"流式读取中断: {e}",
                           reasoning="".join(reasoning_parts),
                           finish_reason=finish, usage=usage)
        finally:
            r.close()
        if not usage:
            self._stats["calls"] += 1
        return ("".join(content_parts), "".join(reasoning_parts), usage, finish)

    def _extract_content(self, data: dict) -> str:
        """从 200 响应提取正文，空内容/截断显式报错（而非静默返回空串）。

        推理模型（如 deepseek-v4.1 系列）会先输出 ``reasoning`` 并占用
        ``max_tokens`` 预算；预算不足时正文为空且 finish_reason == "length"。
        此处将这种情形转为 LLMError，避免"ok 但空串"被上层静默吞掉。
        """
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"API 响应格式异常: {e}")
        content = msg.get("content")
        if content:
            return content
        finish = choice.get("finish_reason")
        reason_len = len(msg.get("reasoning") or "")
        if finish == "length":
            raise LLMError(
                f"LLM 正文被截断为空（finish_reason=length，reasoning {reason_len} 字符，"
                f"max_tokens={self.max_tokens}）；请提高 llm.max_tokens")
        if content is None:
            raise LLMError("LLM 返回空内容")
        raise LLMError(f"LLM 返回空内容（finish_reason={finish}）")

    def _record(self, data: dict) -> dict:
        use = (data.get("usage") or {})
        self._stats["calls"] += 1
        self._stats["prompt_tokens"] += use.get("prompt_tokens", 0)
        self._stats["completion_tokens"] += use.get("completion_tokens", 0)
        return use

    def _rate_limit(self) -> None:
        if self.provider == "mock":
            return
        now = time.monotonic()
        dt = now - self._last_call
        if dt < self._min_interval:
            time.sleep(self._min_interval - dt)
        self._last_call = time.monotonic()

    def _mock(self, system: str, user: str) -> str:
        """离线 mock：根据 system prompt 是否要求 JSON 决定输出格式。"""
        self._stats["calls"] += 1
        # 从 user 里抓需要处理的内容（|TEXT| 或 |DST| 之后的内容）
        m = re.search(r"\|(?:TEXT|DST)\|\n(.*?)(?:\n## |$)", user, re.S)
        src = m.group(1).strip() if m else user[-200:]
        self._stats["completion_tokens"] += max(1, len(src))
        wants_json = "JSON" in system and "只输出 JSON" in system
        if wants_json:
            return json.dumps(
                {
                    "translation": src,
                    "confidence": 1.0,
                    "glossary_conflicts": [],
                    "notes": [],
                    "needs_human": False,
                    "terms": [],
                    "issues": [],
                    "summary": "",
                    "entities": [],
                    "content_type": "其他",
                    "pages": [],
                    "relations": [],
                },
                ensure_ascii=False,
            )
        return src

    # ------------------------------------------------------------------
    def stats_report(self) -> dict:
        return dict(self._stats)

    def _log(self, tag: str, system: str, user: str, response: str,
             ok: bool = True, error: str = "", usage: dict | None = None,
             duration_ms: float = 0.0, messages: list[dict] | None = None,
             task_id: str = "", context_id: str = "", reasoning: str = "",
             finish_reason: str | None = None, diag: dict | None = None) -> None:
        """完整记录一次 LLM 调用（成功或失败，含 mock）。

        ``diag``：可选诊断（如 ``loop_aborts``：本次调用中被检测到循环而中止、
        随后重试的异常轮次全文），写入日志便于事后追溯"曾经的循环"。
        """
        if not self.log_enabled:
            return
        d = self.cfg.get("llm_logs", "dir", default="")
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        ended = datetime.now()
        started = ended - timedelta(milliseconds=duration_ms or 0)
        stamp = ended.strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(d, f"{tag}_{stamp}.json")
        entry = {
            "ts": ended.isoformat(timespec="milliseconds"),
            "started_at": started.isoformat(timespec="milliseconds"),
            "ended_at": ended.isoformat(timespec="milliseconds"),
            "tag": tag,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "ok": ok,
            "error": error,
            "duration_ms": round(duration_ms, 1),
            "usage": usage or {},
            "system": system,
            "user": user,
            "response": response,
        }
        if self.run_tag:
            entry["run"] = self.run_tag
        if reasoning:
            entry["reasoning"] = reasoning
            entry["reasoning_len"] = len(reasoning)
        if finish_reason is not None:
            entry["finish_reason"] = finish_reason
        if task_id:
            entry["task_id"] = task_id
        if context_id:
            entry["context_id"] = context_id
        if messages is not None:
            entry["messages"] = messages
        if diag and diag.get("loop_aborts"):
            entry["loop_detected"] = True
            entry["loop_abort_count"] = len(diag["loop_aborts"])
            entry["loop_aborts"] = diag["loop_aborts"]
        if diag and diag.get("wall_aborts"):
            entry["wall_abort_count"] = len(diag["wall_aborts"])
            entry["wall_aborts"] = diag["wall_aborts"]
        # 尝试解析响应中的 JSON（若为结构化输出）
        try:
            if response:
                parsed = parse_json_response(response)
                entry["parsed"] = parsed
        except (LLMError, json.JSONDecodeError):
            pass
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)


def _extract_balanced_json(text: str) -> str | None:
    """用平衡花括号提取第一个完整的 JSON 对象。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def repair_value_strings(text: str) -> str | None:
    """对已知 key 值字符串做机械转义修复，修复后重新解析失败返回 None。

    覆盖两类常见 LLM JSON 错误：
    - 字符串值内未转义的 `"`（93%）
    - 字符串值内裸换行 / 控制字符（7%）

    单遍状态机：只对"值字符串内部"的裸引号/控制字符转义，结构层不变。
    对已合法 JSON 近似恒等（合法 JSON 的值内不会出现后跟文本的裸引号）。
    """
    out = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                out.append(ch)
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if ch == '"':
                # 看下一非空白字符判断是否结束引号
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                nxt = text[j] if j < n else ""
                if nxt in (",", ":", "}", "]", ""):
                    out.append(ch)  # 结构结束引号，保留并退出字符串
                    in_str = False
                else:
                    out.append("\\\"")  # 值内裸引号，转义
                i += 1
                continue
            if ord(ch) < 0x20:
                out.append("\\n")  # 裸控制字符 → 转义为 \n
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        # 结构层
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def repair_array_closure(text: str) -> str | None:
    """对数组括号不平衡（缺 `]`）做保守修复，返回修复后文本；无可补返回 None。

    触发条件：数组未闭合（`[` 数 > `]` 数），且**数组内部**（括号深度 ≥1）最后一个
    元素结束后紧跟一个已知结构 key（needs_human/confidence/glossary_conflicts/notes/
    translation）。说明 LLM 把该 key 误写成了数组元素，应在它之前补 `]` 闭合数组。

    只匹配"处于数组深度 ≥1 且恰好跳回对象层"的位置，避免把对象层正常
    `translation 值 → confidence key` 的过渡误判为数组缺 `]`。

    已知缺陷（保守门控）：仅凭括号不平衡 + 已知 key 先验判定。若某数组确实未闭合，
    但其最后一个元素值恰好是已知 key 名（如 notes 里正常写了 "notes"），可能误补。
    该概率极低，当前接受；后续可加更严格的结构判定。
    """
    import re as _re

    if text.count("[") <= text.count("]"):
        return None
    # 状态机：跟踪数组深度；在数组内（depth>=1）检测 `", <ws> "KNOWN_KEY"` 的结束引号位置
    keys = set(_ARRAY_KNOWN_KEYS)
    n = len(text)
    i = 0
    arr_depth = 0
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                # 字符串结束：判断这是否是"数组元素结束引号"
                in_str = False
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] == ",":
                    # 逗号后可能是下一个数组元素字符串，或已知 key（结构）
                    k = j + 1
                    while k < n and text[k].isspace():
                        k += 1
                    if k < n and text[k] == '"':
                        # 提取引号内的 key 名
                        k2 = k + 1
                        start_key = k2
                        while k2 < n and text[k2] != '"':
                            k2 += 1
                        key_name = text[start_key:k2]
                        if arr_depth >= 1 and key_name in keys:
                            # 数组内元素结束 → 已知 key：补 ] 于逗号后
                            close_pos = j  # 逗号位置
                            return text[:close_pos] + "]" + text[close_pos:]
            i += 1
            continue
        # 结构层
        if ch == '"':
            in_str = True
        elif ch == "[":
            arr_depth += 1
        elif ch == "]":
            arr_depth -= 1
        i += 1
    return None


def parse_json_response(text: str) -> dict:
    """从 LLM 输出中提取 JSON（容忍 markdown 围栏与前后缀）。

    用平衡花括号匹配提取完整 JSON 对象，避免抓到半截/嵌套错块。
    解析失败时先做值字符串机械修复（repair_value_strings），
    修复成功且含 translation key 则附加 repaired/repair_methods 规范字段；
    修复也失败则抛 LLMError（调用方据此自愈重试）。
    """
    if text is None:
        raise LLMError("LLM 响应为空")
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    block = _extract_balanced_json(text)
    if block:
        try:
            data = json.loads(block)
            if isinstance(data, dict):
                return data  # 合法 JSON，无修复标记
        except json.JSONDecodeError:
            pass
    # 修复候选：优先 block；block 提取失败（如值内未转义引号使 in_str 翻转）则用完整文本
    candidates = [block] if block else [text]
    for base in candidates:
        # 方法1：值字符串转义（未转义引号/裸换行）
        data = _try_repair(base, lambda s: repair_value_strings(s),
                           [REPAIR_METHOD_ESCAPE])
        if data is not None:
            return data
        # 方法2：值转义后再补数组闭合 ]（Case1/2：需先转义值内引号才能看清数组边界）
        data = _try_repair(base, lambda s: repair_array_closure(repair_value_strings(s)),
                           [REPAIR_METHOD_ESCAPE, REPAIR_METHOD_CLOSE_ARRAY])
        if data is not None:
            return data
    raise LLMError(f"无法解析 LLM JSON 输出: {text[:300]}")


def _try_repair(base: str, repair_fn, methods: list[str]) -> dict | None:
    """对 base 应用修复函数，成功且含 translation key 则附加修复规范字段返回。"""
    repaired = repair_fn(base)
    if not repaired:
        return None
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "translation" in data:
        data["repaired"] = True
        data["repair_methods"] = list(methods)
        return data
    return None
