# -*- coding: utf-8 -*-
"""JSON 机械修复（值字符串转义）测试。"""
from __future__ import annotations

import json

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
