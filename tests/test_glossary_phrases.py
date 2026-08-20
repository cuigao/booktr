# -*- coding: utf-8 -*-
"""词汇表 / 短语记忆测试：相关检索、机械替换、upsert、add-term 清理、提示词格式。"""
from __future__ import annotations

import pytest

from booktr import glossary as gl
from booktr import phrases as phrases_mod
from booktr import prompts


# ── 词汇表 ──────────────────────────────────────────────────────────────


def test_upsert_add(tmp_cfg):
    ok, msg = gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    assert ok
    items = gl.load(tmp_cfg)
    assert any(it["src"] == "HOME" and it["dst"] == "首页" for it in items)
    # confirmed → read_only
    it = next(i for i in items if i["src"] == "HOME")
    assert it["read_only"] is True


def test_upsert_conflict_overwrites(tmp_cfg):
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    ok, msg = gl.upsert(tmp_cfg, {"src": "HOME", "dst": "主页", "status": "confirmed"})
    assert ok
    assert "不同译文" in msg
    items = gl.load(tmp_cfg)
    matching = [i for i in items if i["src"] == "HOME"]
    assert len(matching) == 1
    assert matching[0]["dst"] == "主页"


def test_lookup_read_only_exact(tmp_cfg):
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    assert gl.lookup_read_only(tmp_cfg, "HOME") == "首页"
    # 不精确匹配 → None
    assert gl.lookup_read_only(tmp_cfg, "HOMEX") is None


def test_lookup_read_only_skips_auto_candidate(tmp_cfg):
    gl.merge_candidates(tmp_cfg, [{"src": "アルバム", "dst": "专辑"}])
    # auto-candidate 非 read_only → 不机械替换
    assert gl.lookup_read_only(tmp_cfg, "アルバム") is None


def test_relevant_relaxed_match_ignore_case_ws(tmp_cfg):
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    # 大小写/空白差异仍命中（宽松匹配）
    hits = gl.relevant(tmp_cfg, "click  HOME  link")
    assert any(it["src"] == "HOME" for it in hits)


def test_relevant_no_match(tmp_cfg):
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    assert gl.relevant(tmp_cfg, "全然无关的文本") == []


def test_add_term_cleans_phrase(tmp_cfg):
    # 先写入短语记忆同名词条
    phrases_mod.add(tmp_cfg, "HOME", "主页")
    assert phrases_mod.lookup(tmp_cfg, "HOME") == "主页"
    # add-term 添加词汇表并清理短语记忆
    gl.add_term(tmp_cfg, "HOME", "首页", note="导航")
    assert phrases_mod.lookup(tmp_cfg, "HOME") is None
    assert gl.lookup_read_only(tmp_cfg, "HOME") == "首页"


# ── 短语记忆 ────────────────────────────────────────────────────────────


def test_phrases_add_and_lookup(tmp_cfg):
    assert phrases_mod.add(tmp_cfg, "RETURN", "返回")
    assert phrases_mod.lookup(tmp_cfg, "RETURN") == "返回"


def test_phrases_add_blocked_by_glossary_read_only(tmp_cfg):
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})
    # 词汇表已有 read_only 条目 → 短语记忆不写入
    assert phrases_mod.add(tmp_cfg, "HOME", "主页") is False
    assert phrases_mod.lookup(tmp_cfg, "HOME") is None


def test_phrases_add_rejects_too_long(tmp_cfg):
    assert phrases_mod.add(tmp_cfg, "x" * 100, "y") is False
    assert phrases_mod.lookup(tmp_cfg, "x" * 100) is None


def test_phrases_relevant_relaxed(tmp_cfg):
    phrases_mod.add(tmp_cfg, "BACK NUMBER", "往期回顾")
    hits = phrases_mod.relevant(tmp_cfg, "go to back  number page")
    assert any(it["src"] == "BACK NUMBER" for it in hits)


# ── format_term_hints ───────────────────────────────────────────────────


def test_format_term_hints_with_note(tmp_cfg):
    items = [{"src": "HOME", "dst": "首页", "note": "导航入口"}]
    out = prompts.format_term_hints(items)
    assert "## 推荐翻译译文" in out
    assert "HOME → 首页" in out
    assert "使用场景：导航入口" in out


def test_format_term_hints_no_note_no_label(tmp_cfg):
    # 无 note 的条目不标注使用场景，靠标题统一说明
    items = [{"src": "No.90", "dst": "第90号"}, {"src": "CONTENTS", "dst": "CONTENTS"}]
    out = prompts.format_term_hints(items)
    assert "HOME → 首页" not in out
    assert "No.90 → 第90号" in out
    # 无任何 └ 使用场景 行
    assert "└" not in out


def test_format_term_hints_empty(tmp_cfg):
    assert prompts.format_term_hints([]) == ""
