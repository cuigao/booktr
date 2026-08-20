# -*- coding: utf-8 -*-
"""JSON 机械修复（值字符串转义）测试。"""
from __future__ import annotations

import json

import pytest

from booktr import llm
from booktr.llm import REPAIR_METHOD_ESCAPE


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
