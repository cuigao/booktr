# -*- coding: utf-8 -*-
"""JSON 机械修复（值字符串转义）测试。"""
from __future__ import annotations

import json
import os

import pytest

from booktr import llm
from booktr.llm import REPAIR_METHOD_ESCAPE


# ── api_key 解析与 required 开关 ──────────────────────────────────────


def _make_cfg(llm_dict, data_dir):
    from booktr.config import Config
    import tempfile, os
    return Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  data_dir=data_dir, data={"llm": llm_dict})


def test_api_key_priority_config_over_env(tmp_path, monkeypatch):
    """config 的 llm.api_key 直接值优先于环境变量。"""
    monkeypatch.setenv("TEST_KEY_ENV", "ENV_KEY")
    cfg = _make_cfg({"provider": "openai-compatible",
                     "api_key_env": "TEST_KEY_ENV",
                     "api_key": "CONFIG_KEY"}, str(tmp_path))
    client = llm.LLMClient(cfg)
    assert client.api_key == "CONFIG_KEY"


def test_api_key_env_fallback(tmp_path, monkeypatch):
    """未填 config.api_key 时回退环境变量。"""
    monkeypatch.setenv("TEST_KEY_ENV", "ENV_KEY")
    cfg = _make_cfg({"provider": "openai-compatible",
                     "api_key_env": "TEST_KEY_ENV"}, str(tmp_path))
    client = llm.LLMClient(cfg)
    assert client.api_key == "ENV_KEY"


def test_api_key_required_default_true(tmp_path):
    """默认 api_key_required=True。"""
    cfg = _make_cfg({"provider": "openai-compatible"}, str(tmp_path))
    assert llm.LLMClient(cfg).api_key_required is True


def test_api_key_required_false_skips_check(tmp_path, monkeypatch):
    """api_key_required=False 且空 key 时不抛 LLMError（本地 ollama 免 key）。"""
    monkeypatch.delenv("BOOKTR_API_KEY", raising=False)
    cfg = _make_cfg({"provider": "openai-compatible",
                     "api_key_required": False}, str(tmp_path))
    client = llm.LLMClient(cfg)
    assert client.api_key_required is False
    assert client.api_key == ""


# ── parse_json_response：合法 JSON ──────────────────────────────────────


def test_parse_valid_json_no_repair_flag():
    resp = '{"translation": "你好", "confidence": 0.9, "notes": ["保留引号\u201c引\u201d"]}'
    data = llm.parse_json_response(resp)
    assert data["translation"] == "你好"
    assert "repaired" not in data  # 合法 JSON 不附加修复标记


# ── 未转义引号（93% 场景）──────────────────────────────────────────────


def test_parse_unescaped_quote_repairs_and_preserves():
    # 值字符串内含未转义引号（用户引述的话语）
    resp = '{"translation": "他说\u201c真棒\u201d然后说"真的"走了", "confidence": 0.9, "glossary_conflicts": [], "notes": [], "needs_human": false}'
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert REPAIR_METHOD_ESCAPE in data["repair_methods"]
    # 引号被保留（转义而非删除）
    assert "\u201c真棒\u201d" in data["translation"]
    assert "真的" in data["translation"]


def test_parse_unescaped_quote_in_notes():
    resp = '{"translation": "译文", "confidence": 0.9, "glossary_conflicts": [], "notes": ["保留\"引号\"", "正常"], "needs_human": false}'
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert data["notes"][0] == '保留"引号"'


# ── 未转义换行（7% 场景）──────────────────────────────────────────────


def test_parse_unescaped_newline_repair():
    resp = '{"translation": "第一行\n第二行", "confidence": 0.9, "glossary_conflicts": [], "notes": [], "needs_human": false}'
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert "\n" in data["translation"]


# ── 修复失败（真损坏）仍抛 LLMError → 走 LLM repair ────────────────────


def test_parse_unrepairable_raises():
    resp = '{"translation": "abc'  # 结构残缺，修复无法恢复
    with pytest.raises(llm.LLMError):
        llm.parse_json_response(resp)


# ── 门控：非 translation schema 不误伤 ────────────────────────────────


def test_parse_summary_not_gated():
    # summary 响应无 translation key，修复即使"成功"也不附加 repaired
    resp = '{"summary": "概要内容", "entities": ["a"], "content_type": "其他"}'
    data = llm.parse_json_response(resp)
    assert "repaired" not in data


# ── repair_value_strings 对合法 JSON 近似恒等 ─────────────────────────


def test_repair_identity_on_valid_json():
    s = '{"translation": "你好，世界", "confidence": 0.9}'
    out = llm.repair_value_strings(s)
    assert json.loads(out) == {"translation": "你好，世界", "confidence": 0.9}


def test_repair_escapes_value_quotes():
    s = '{"translation": "他说"真的"吗", "confidence": 0.9}'
    out = llm.repair_value_strings(s)
    data = json.loads(out)
    assert data["translation"] == '他说"真的"吗'


# ── 数组括号闭合修复（CLOSE_ARRAY）────────────────────────────────────


def test_parse_missing_array_close():
    # notes 数组缺闭合 ]，needs_human 被吞进数组
    resp = ('{"translation": "译文", "confidence": 0.92, "glossary_conflicts": [], '
            '"notes": ["备注兼差", "needs_human": false}')
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert data["repair_methods"] == [llm.REPAIR_METHOD_ESCAPE, llm.REPAIR_METHOD_CLOSE_ARRAY]
    assert data["needs_human"] is False
    assert data["notes"] == ["备注兼差"]


def test_parse_missing_close_multiline():
    # 多行缩进 + 缺 ]（notes 数组未闭合，needs_human 被吞入）
    resp = ('{"translation": "x", "confidence": 0.9,\n'
            '  "notes": ["note one",\n'
            '  "needs_human": false}')
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert llm.REPAIR_METHOD_CLOSE_ARRAY in data["repair_methods"]
    assert data["needs_human"] is False


def test_repair_array_closure_balanced_noop():
    # 括号平衡的合法数组不触发补 ]
    legal = '{"translation": "x", "notes": ["a", "b"], "needs_human": false}'
    assert llm.repair_array_closure(legal) is None
    data = llm.parse_json_response(legal)
    assert "repaired" not in data


def test_parse_escape_fallback_when_block_extraction_fails():
    """_extract_balanced_json 因值内未转义引号返回 None 时，用完整文本兜底修复。"""
    resp = ('{"translation": "收到"ＯＫ"的回复", "confidence": 0.95, '
            '"glossary_conflicts": [], "notes": [], "needs_human": false}')
    data = llm.parse_json_response(resp)
    assert data["repaired"] is True
    assert data["repair_methods"] == [llm.REPAIR_METHOD_ESCAPE]
    assert "ＯＫ" in data["translation"]


def test_repair_method_order_escape_then_close():
    """方法顺序：先值转义，后补数组闭合。"""
    resp = ('{"translation": "甲"乙", "notes": ["丙", "needs_human": false}')
    data = llm.parse_json_response(resp)
    assert data["repair_methods"] == [llm.REPAIR_METHOD_ESCAPE, llm.REPAIR_METHOD_CLOSE_ARRAY]

# ── _extract_content：空内容/截断显式报错 ──────────────────────────────


def _client(tmp_path, **extra):
    llm_cfg = {"provider": "openai-compatible", "api_key_required": False,
               "max_tokens": 4096, "stream": False}
    llm_cfg.update(extra)
    return llm.LLMClient(_make_cfg(llm_cfg, str(tmp_path)))


def test_extract_content_ok(tmp_path):
    c = _client(tmp_path)
    data = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    assert c._extract_content(data) == "hi"


def test_extract_content_truncated_raises(tmp_path):
    """finish_reason=length 且正文为空 → 明确提示提高 max_tokens。"""
    c = _client(tmp_path)
    data = {"choices": [{"message": {"content": "", "reasoning": "x" * 50},
                         "finish_reason": "length"}]}
    with pytest.raises(llm.LLMError) as ei:
        c._extract_content(data)
    assert "max_tokens" in str(ei.value)


def test_extract_content_none_raises(tmp_path):
    c = _client(tmp_path)
    data = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
    with pytest.raises(llm.LLMError):
        c._extract_content(data)


def test_extract_content_bad_shape_raises(tmp_path):
    c = _client(tmp_path)
    with pytest.raises(llm.LLMError):
        c._extract_content({"choices": []})


# ── reasoning_effort 透传 ──────────────────────────────────────────────


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {}}


def _capture_body(client, monkeypatch, **kw):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _FakeResp()

    monkeypatch.setattr("booktr.llm.requests.post", fake_post)
    fn = kw.pop("_fn", "chat")
    if fn == "chat":
        client.chat("sys", "usr", **kw)
    else:
        client.chat_multi([{"role": "user", "content": "usr"}], **kw)
    return captured


def test_reasoning_effort_sent_when_set(tmp_path, monkeypatch):
    c = _client(tmp_path)
    body = _capture_body(c, monkeypatch, reasoning_effort="none")
    assert body["reasoning_effort"] == "none"


def test_reasoning_effort_absent_by_default(tmp_path, monkeypatch):
    c = _client(tmp_path)
    body = _capture_body(c, monkeypatch)
    assert "reasoning_effort" not in body


def test_reasoning_effort_multi(tmp_path, monkeypatch):
    c = _client(tmp_path)
    body = _capture_body(c, monkeypatch, _fn="chat_multi", reasoning_effort="low")
    assert body["reasoning_effort"] == "low"


# ── 流式聚合 + 截空翻倍重试 + reasoning 记录 ────────────────────────────


class _FakeStream:
    """模拟流式响应对象，供 _post_stream 的 iter_lines() 使用。"""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


def _sse(*events):
    lines = []
    for e in events:
        lines.append("data: " + json.dumps(e, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def test_stream_aggregates_content_and_reasoning(tmp_path, monkeypatch):
    c = _client(tmp_path, stream=True)
    lines = _sse(
        {"choices": [{"delta": {"reasoning": "think1"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning": "think2"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5,
                                  "total_tokens": 8}},
    )
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        captured["body"] = json
        return _FakeStream(lines)

    monkeypatch.setattr("booktr.llm.requests.post", fake_post)
    out = c.chat("s", "u")
    assert out == "hello world"
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}


def test_stream_length_empty_doubles_maxtokens(tmp_path, monkeypatch):
    """finish=length 且正文空 → 第二次请求 max_tokens 翻倍。"""
    c = _client(tmp_path, stream=True, max_tokens=1000, max_tokens_ceiling=4096)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(json.get("max_tokens"))
        if len(calls) == 1:
            return _FakeStream(_sse(
                {"choices": [{"delta": {"reasoning": "x" * 100}, "finish_reason": "length"}]}))
        return _FakeStream(_sse(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    monkeypatch.setattr("booktr.llm.requests.post", fake_post)
    out = c.chat("s", "u")
    assert out == "ok"
    assert calls == [1000, 2000]


def test_stream_ceiling_caps_doubling(tmp_path, monkeypatch):
    """翻倍不超过 ceiling；仍空则报错。"""
    c = _client(tmp_path, stream=True, max_tokens=3000, max_tokens_ceiling=4096)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(json.get("max_tokens"))
        return _FakeStream(_sse(
            {"choices": [{"delta": {"reasoning": "x" * 10}, "finish_reason": "length"}]}))

    monkeypatch.setattr("booktr.llm.requests.post", fake_post)
    with pytest.raises(llm.LLMError):
        c.chat("s", "u")
    assert calls == [3000, 4096]  # 3000*2=6000 被 ceiling 4096 截断


def test_stream_network_error_retries(tmp_path, monkeypatch):
    c = _client(tmp_path, stream=True, max_retries=1)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(1)
        if len(calls) == 1:
            raise llm.requests.ConnectionError("boom")
        return _FakeStream(_sse(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    monkeypatch.setattr("booktr.llm.requests.post", fake_post)
    monkeypatch.setattr("booktr.llm.time.sleep", lambda *a: None)
    assert c.chat("s", "u") == "ok"
    assert len(calls) == 2


def test_log_records_reasoning(tmp_cfg, monkeypatch):
    tmp_cfg.set("openai-compatible", "llm", "provider")
    tmp_cfg.set(False, "llm", "api_key_required")
    c = llm.LLMClient(tmp_cfg)
    c.stream = True
    monkeypatch.setattr("booktr.llm.requests.post", lambda *a, **k: _FakeStream(_sse(
        {"choices": [{"delta": {"reasoning": "R"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "C"}, "finish_reason": "stop"}]})))
    c.chat("s", "u", tag="qa")
    logdir = tmp_cfg.get("llm_logs", "dir", default="")
    import glob
    files = glob.glob(os.path.join(logdir, "qa_*.json"))
    assert files
    entry = json.load(open(files[-1], encoding="utf-8"))
    assert entry["reasoning"] == "R"
    assert entry["reasoning_len"] == 1


# ── 失败调用记录 reasoning / finish_reason（诊断） ──────────────────────


def test_llmerror_carries_diagnostics():
    e = llm.LLMError("m", reasoning="think", finish_reason="length",
                     usage={"prompt_tokens": 1})
    assert str(e) == "m"
    assert e.reasoning == "think"
    assert e.finish_reason == "length"
    assert e.usage == {"prompt_tokens": 1}
    # 默认值
    d = llm.LLMError("x")
    assert d.reasoning == "" and d.finish_reason is None and d.usage == {}


def test_log_records_reasoning_on_failure(tmp_cfg, monkeypatch):
    """截空失败（finish=length）也记录 reasoning / finish_reason。"""
    tmp_cfg.set("openai-compatible", "llm", "provider")
    tmp_cfg.set(False, "llm", "api_key_required")
    c = llm.LLMClient(tmp_cfg)
    c.stream = True
    c.max_tokens = 3000
    c.max_tokens_ceiling = 4096
    monkeypatch.setattr("booktr.llm.requests.post", lambda *a, **k: _FakeStream(_sse(
        {"choices": [{"delta": {"reasoning": "x" * 100}, "finish_reason": "length"}]})))
    with pytest.raises(llm.LLMError):
        c.chat("s", "u", tag="qa")
    logdir = tmp_cfg.get("llm_logs", "dir", default="")
    import glob
    files = glob.glob(os.path.join(logdir, "qa_*.json"))
    assert files
    entry = json.load(open(files[-1], encoding="utf-8"))
    assert entry["ok"] is False
    assert entry["reasoning"] == "x" * 100
    assert entry["reasoning_len"] == 100
    assert entry["finish_reason"] == "length"


def test_log_records_partial_reasoning_on_stream_error(tmp_cfg, monkeypatch):
    """流式中途网络中断（重试耗尽）也带出已累加的部分 reasoning。"""
    tmp_cfg.set("openai-compatible", "llm", "provider")
    tmp_cfg.set(False, "llm", "api_key_required")
    c = llm.LLMClient(tmp_cfg)
    c.stream = True
    c.max_retries = 0

    class _BoomStream:
        status_code = 200

        def iter_lines(self, decode_unicode=False):
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"reasoning": "partial"}, "finish_reason": None}]})
            raise llm.requests.ConnectionError("boom")

        def close(self):
            pass

    monkeypatch.setattr("booktr.llm.requests.post",
                        lambda *a, **k: _BoomStream())
    monkeypatch.setattr("booktr.llm.time.sleep", lambda *a: None)
    with pytest.raises(llm.LLMError):
        c.chat("s", "u", tag="qa")
    logdir = tmp_cfg.get("llm_logs", "dir", default="")
    import glob
    files = glob.glob(os.path.join(logdir, "qa_*.json"))
    assert files
    entry = json.load(open(files[-1], encoding="utf-8"))
    assert entry["reasoning"] == "partial"
