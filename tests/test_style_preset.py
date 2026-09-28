# -*- coding: utf-8 -*-
"""翻译风格预设测试：init 风格选项写入 user_rules。"""
from __future__ import annotations

from booktr.pipeline import TRANSLATION_STYLES, apply_style_preset


def test_standard_returns_unchanged():
    """standard 预设不改动 user_rules。"""
    base = "保留原文中的全角写法"
    assert apply_style_preset(base, "standard") == base


def test_unknown_key_returns_unchanged():
    """未知风格 key 安全回退，不改动 user_rules。"""
    base = "保留原文中的全角写法"
    assert apply_style_preset(base, "klingon") == base


def test_shanghai_appends_rules():
    """上海话预设追加方言规则块。"""
    base = "保留原文中的全角写法"
    out = apply_style_preset(base, "shanghai")
    assert out.startswith(base)
    assert "## 翻译风格：上海话" in out
    assert "简体中文（上海话）" in out
    assert "阿拉" in out


def test_shanghai_does_not_override_existing_rules():
    """追加不覆盖模板原有全角/专名规则。"""
    base = "全角写法；人名、地名、机构名等专名应译出中文"
    out = apply_style_preset(base, "shanghai")
    assert "全角写法" in out
    assert "专名应译出中文" in out


def test_shanghai_on_empty_rules():
    """空 user_rules 时直接返回规则块。"""
    out = apply_style_preset("", "shanghai")
    assert out.startswith("## 翻译风格：上海话")
    assert "全角写法" not in out.splitlines()[0]


def test_preset_table_has_standard_first():
    """预设表首项为标准，便于默认选择。"""
    assert TRANSLATION_STYLES[0][0] == "standard"
    assert TRANSLATION_STYLES[0][1] == "标准"
    keys = [k for k, _, _ in TRANSLATION_STYLES]
    assert "shanghai" in keys
    assert len(set(keys)) == len(keys)
