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
                   tag: str = "chat_multi") -> str:
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
                      resp, ok=True, duration_ms=(time.monotonic() - t0) * 1000)
            return resp
        if not self.api_key:
            err = (f"未设置 API key（环境变量 {self.cfg.get('llm','api_key_env',default='BOOKTR_API_KEY')}）。"
                   "或在 data/config.json 将 llm.provider 设为 mock 进行离线测试。")
            self._log(tag, system, f"[{len(messages)} msgs]", "",
                      ok=False, error=err,
                      duration_ms=(time.monotonic() - t0) * 1000)
            raise LLMError(err)
        try:
            resp, usage = self._openai_chat_multi(messages, temperature)
        except LLMError as e:
            self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                      "", ok=False, error=str(e),
                      duration_ms=(time.monotonic() - t0) * 1000)
            raise
        self._log(tag, system, f"[{len(messages)} msgs] {last_user[:200]}",
                  resp, ok=True, usage=usage,
                  duration_ms=(time.monotonic() - t0) * 1000)
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
                    content = data["choices"][0]["message"]["content"]
                    usage = self._record(data)
                    return content, usage
                last_err = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
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
                    content = data["choices"][0]["message"]["content"]
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
             duration_ms: float = 0.0) -> None:
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
        # 尝试解析响应中的 JSON（若为结构化输出）
        try:
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


def parse_json_response(text: str) -> dict:
    """从 LLM 输出中提取 JSON（容忍 markdown 围栏与前后缀）。

    用平衡花括号匹配提取完整 JSON 对象，避免抓到半截/嵌套错块。
    解析失败抛 LLMError（调用方据此自愈重试）。
    """
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
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError(f"无法解析 LLM JSON 输出: {text[:300]}")
