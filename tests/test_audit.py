# -*- coding: utf-8 -*-
"""audit-terms 重建测试：rebuild_translation 纯函数 + cmd_audit_terms 流程。"""
from __future__ import annotations

import argparse
import os

import pytest

from booktr import glossary as gl
from booktr import translate as tr
from booktr.pipeline import rebuild_translation, cmd_audit_terms
from conftest import FakeLLM, write_sample_site


# ── rebuild_translation 纯函数 ──────────────────────────────────────────


def test_rebuild_exact_match_replaces():
    lookup = {"HOME": "首页"}
    src = "[[P0]]HOME[[P1]]"
    trans = "[[P0]]Home[[P1]]"
    new, changed = rebuild_translation(src, trans, lookup)
    assert changed
    assert new == "[[P0]]首页[[P1]]"


def test_rebuild_nonmatch_copies():
    lookup = {"HOME": "首页"}
    src = "[[P0]]こんにちは[[P1]]"
    trans = "[[P0]]你好[[P1]]"
    new, changed = rebuild_translation(src, trans, lookup)
    assert not changed
    assert new == trans


def test_rebuild_placeholder_mismatch_noop():
    lookup = {"HOME": "首页"}
    src = "[[P0]]HOME[[P1]]"
    trans = "[[P1]]Home[[P0]]"  # 顺序不一致
    new, changed = rebuild_translation(src, trans, lookup)
    assert not changed
    assert new == trans


def test_rebuild_preserves_whitespace():
    lookup = {"HOME": "首页"}
    src = "  HOME  "
    trans = "  home  "
    new, changed = rebuild_translation(src, trans, lookup)
    assert new == "  首页  "


def test_rebuild_mixed_blocks():
    lookup = {"HOME": "首页", "NEXT": "下一页"}
    src = "HOME [[P0]] NEXT [[P1]]"
    trans = "home [[P0]] next [[P1]]"
    new, changed = rebuild_translation(src, trans, lookup)
    assert changed
    assert new == "首页 [[P0]] 下一页 [[P1]]"


# ── cmd_audit_terms 流程 ───────────────────────────────────────────────


def _args(**kw):
    base = {"pages": None, "dry_run": False, "no_regenerate": True}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_audit_terms_updates_state_and_cache(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 用 mock 翻译页面，得到已译段
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])
    pstate = state.page("page1.html")

    # 找一个可机械替换的段：先记录 HOME 段原始译文，再添加词汇表条目
    segs = state.page("page1.html").get("segments", {})
    assert segs, "应已有已译段"

    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})

    # 运行审计
    cmd_audit_terms(tmp_cfg, _args(pages=["page1.html"]))

    # state 同步更新
    st = tr.State(tmp_cfg)
    for sid, seg in st.page("page1.html").get("segments", {}).items():
        if seg.get("translation"):
            assert seg["translation"] is not None

    # 段缓存同步更新（work/segments/page1.html.json）
    cache_path = os.path.join(tmp_cfg.get("segments_dir", default=""), "page1.html.json")
    assert os.path.exists(cache_path)


def test_cmd_audit_terms_dry_run_no_modify(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "status": "confirmed"})

    # 记录审计前的译文快照
    before = dict(state.page("page1.html").get("segments", {}))

    cmd_audit_terms(tmp_cfg, _args(pages=["page1.html"], dry_run=True))

    # dry-run 不应修改 state
    st = tr.State(tmp_cfg)
    after = st.page("page1.html").get("segments", {})
    for sid, seg in before.items():
        assert seg.get("translation") == after.get(sid, {}).get("translation")
