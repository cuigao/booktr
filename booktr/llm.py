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
from datetime import datetime

import requests

from .config import Config

log = logging.getLogger("booktr.llm")


class LLMError(RuntimeError):
    pass


# JSON 机械修复方法（parse_json_response 后处理），规范字段 repair_methods 的取值。
REPAIR_METHOD_ESCAPE = "ESCAPE_VALUE_STRINGS"  # 值字符串转义（未转义引号 / 裸换行）
REPAIR_METHOD_CLOSE_ARRAY = "CLOSE_ARRAY"  # 数组括号闭合修复（缺 ]，已知 key 先验补全）

# 数组闭合修复时用于定位"数组被提前截断"的已知结构 key（已知的先验）。
# 这些 key 出现在数组元素结束后本应闭合数组的位置，却被 LLM 直接写成了字符串元素。
_ARRAY_KNOWN_KEYS = (
    "needs_human", "confidence", "glossary_conflicts", "notes", "translation",
)


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        llm = cfg.get("llm", default={})
        self.provider = llm.get("provider", "mock")
        self.base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
        self.model = llm.get("model", "gpt-4o-mini")
        api_env = llm.get("api_key_env", "BOOKTR_API_KEY")
        self.api_key = os.environ.get(api_env, "")
        self.temperature = llm.get("temperature", 0.3)
        self.max_tokens = llm.get("max_tokens", 4096)
        self.timeout = llm.get("timeout", 120)
        self.max_retries = llm.get("max_retries", 3)
        self.rpm = llm.get("max_requests_per_minute", 60)
        self._min_interval = 60.0 / max(self.rpm, 1)
        self._last_call = 0.0
        self._stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float | None = None,
             tag: str = "chat") -> str:
        """单轮对话，返回文本。每次调用（含 mock）都完整记录到 llm_logs。"""
        t0 = time.monotonic()
        if self.provider == "mock":
            resp = self._mock(system, user)
            self._log(tag, system, user, resp, ok=True,
                      duration_ms=(time.monotonic() - t0) * 1000)
            return resp
        if not self.api_key:
            err = (f"未设置 API key（环境变量 {self.cfg.get('llm','api_key_env',default='BOOKTR_API_KEY')}）。"
                   "或在 data/config.json 将 llm.provider 设为 mock 进行离线测试。")
            self._log(tag, system, user, "", ok=False, error=err,
                      duration_ms=(time.monotonic() - t0) * 1000)
            raise LLMError(err)
        try:
            resp, usage = self._openai_chat(system, user, temperature)
        except LLMError as e:
            self._log(tag, system, user, "", ok=False, error=str(e),
                      duration_ms=(time.monotonic() - t0) * 1000)
            raise
        self._log(tag, system, user, resp, ok=True, usage=usage,
                  duration_ms=(time.monotonic() - t0) * 1000)
        return resp

    # ------------------------------------------------------------------
    def chat_multi(self, messages: list[dict], temperature: float | None = None,
                   tag: str = "chat_multi", task_id: str = "",
                   context_id: str = "") -> str:
        """多轮对话，messages = [{"role": "system"|"user"|"assistant", "content": ...}]。

        返回最后一条 assistant 消息的文本。完整记录到 llm_logs。
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
        if not self.api_key:
            err = (f"未设置 API key（环境变量 {self.cfg.get('llm','api_key_env',default='BOOKTR_API_KEY')}）。"
                   "或在 data/config.json 将 llm.provider 设为 mock 进行离线测试。")
            self._log(tag, system, f"[{len(messages)} msgs]", "",
                      ok=False, error=err,
                      duration_ms=(time.monotonic() - t0) * 1000,
                      messages=messages, task_id=task_id, context_id=context_id)
            raise LLMError(err)
        try:
            resp, usage = self._openai_chat_multi(messages, temperature)
        except LLMError as e:
            self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                      "", ok=False, error=str(e),
                      duration_ms=(time.monotonic() - t0) * 1000,
                      messages=messages, task_id=task_id, context_id=context_id)
            raise
        self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                  resp, ok=True, usage=usage,
                  duration_ms=(time.monotonic() - t0) * 1000,
                  messages=messages, task_id=task_id, context_id=context_id)
        return resp

    def _openai_chat_multi(self, messages: list[dict],
                           temperature: float | None) -> tuple[str, dict]:
        """多轮对话底层调用。"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = self.base_url + "/chat/completions"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                r = requests.post(url, headers=headers, json=body, timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json()
                    try:
                        content = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as e:
                        last_err = LLMError(f"API 响应格式异常: {e}")
                        raise LLMError(f"API 响应格式异常: {e}")
                    if content is None:
                        last_err = LLMError("LLM 返回空内容")
                        raise LLMError("LLM 返回空内容")
                    usage = self._record(data)
                    return content, usage
                last_err = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            except LLMError:
                pass  # last_err 已在抛出前设置，进入重试循环
            delay = 2 ** attempt
            log.warning("LLM multi 调用失败(%s)，%.1fs 后重试: %s", attempt + 1, delay, last_err)
            time.sleep(delay)
        raise LLMError(f"LLM multi 调用最终失败: {last_err}")

    # ------------------------------------------------------------------
    def _openai_chat(self, system: str, user: str, temperature: float | None) -> tuple[str, dict]:
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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = self.base_url + "/chat/completions"
        last_err: Exception | None = None
        retries = 0
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                r = requests.post(url, headers=headers, json=body, timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json()
                    try:
                        content = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as e:
                        raise LLMError(f"API 响应格式异常: {e}")
                    if content is None:
                        raise LLMError("LLM 返回空内容")
                    usage = self._record(data)
                    return content, usage
                last_err = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            retries += 1
            delay = 2 ** attempt
            log.warning("LLM 调用失败(%s)，%.1fs 后重试: %s", attempt + 1, delay, last_err)
            time.sleep(delay)
        raise LLMError(f"LLM 调用最终失败: {last_err}")

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
             task_id: str = "", context_id: str = "") -> None:
        """完整记录一次 LLM 调用（成功或失败，含 mock）。"""
        d = self.cfg.get("llm_logs", "dir", default="")
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(d, f"{tag}_{ts}.json")
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
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
        if task_id:
            entry["task_id"] = task_id
        if context_id:
            entry["context_id"] = context_id
        if messages is not None:
            entry["messages"] = messages
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
