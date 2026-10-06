# -*- coding: utf-8 -*-
"""翻译风格预设测试：init 风格选项写入 user_rules。"""
from __future__ import annotations

from booktr.pipeline import (
    TRANSLATION_STYLES,
    apply_style_preset,
    strip_style_presets,
)


def test_standard_returns_unchanged():
    """base 无风格块时，standard 不改动 user_rules。"""
    base = "保留原文中的全角写法"
    assert apply_style_preset(base, "standard") == base


def test_unknown_key_returns_unchanged():
    """未知风格 key 安全回退（剥离风格块后返回），不改动普通规则。"""
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


def test_switching_style_replaces_not_appends():
    """faithful → fluent：旧的忠实块被剥离，不与之并存。"""
    out = apply_style_preset(apply_style_preset("基础规则", "faithful"), "fluent")
    assert "翻译风格：忠实优先" not in out
    assert out.count("翻译风格：流畅优先") == 1
    assert "基础规则" in out


def test_standard_strips_existing_style():
    """选标准 = 剥离全部风格块、不追加。"""
    base = "基础规则\n\n## 翻译风格：流畅优先\n- 旧块"
    out = apply_style_preset(base, "standard")
    assert "翻译风格" not in out
    assert out == "基础规则"


def test_keep_preserves_existing_style_verbatim():
    """keep=True：原样沿用 base（含自定义风格块），不剥离、不追加。"""
    base = "基础规则\n\n## 翻译风格：特别定制\n- 用户自定义块"
    assert apply_style_preset(base, "faithful", keep=True) == base


def test_strip_removes_all_known_and_custom_blocks():
    """strip_style_presets 清除所有风格块（含自定义标题），保留普通规则。"""
    text = ("规则A\n\n## 翻译风格：忠实优先\n- x\n\n"
            "## 翻译风格：特别定制\n- y\n\n## 其它章节\n- z")
    out = strip_style_presets(text)
    assert "翻译风格" not in out
    assert "规则A" in out and "## 其它章节" in out and "- z" in out


def test_strip_no_style_returns_unchanged():
    """无风格块时 strip 原样返回（不触碰普通规则）。"""
    base = "保留原文中的全角写法"
    assert strip_style_presets(base) == base


def test_preset_table_has_standard_first():
    """预设表首项为标准，便于默认选择。"""
    assert TRANSLATION_STYLES[0][0] == "standard"
    assert TRANSLATION_STYLES[0][1] == "标准"
    keys = [k for k, _, _ in TRANSLATION_STYLES]
    assert "shanghai" in keys
    assert len(set(keys)) == len(keys)
