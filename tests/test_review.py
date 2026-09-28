# -*- coding: utf-8 -*-
"""审核流程测试：interactive_review 各操作 + _finalize_review 状态流转。"""
from __future__ import annotations

import os

import pytest

from booktr import glossary as gl
from booktr import review as review_mod
from booktr import translate as tr
from conftest import FakeLLM, write_sample_site


def _make_translated_page(tmp_cfg, tmp_path):
    """用 mock 翻译一个页面，返回 (State, 段ID列表)。"""
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])
    pstate = state.page("page1.html")
    sid = next(iter(pstate.get("segments", {})))
    return state, sid


def _queue(tmp_cfg, page, sid, reason, detail="", status="open"):
    return [{"page": page, "segment_id": sid, "src": "原文", "reason": reason,
             "detail": detail, "status": status}]


def _disable_auto_regenerate(tmp_cfg):
    tmp_cfg.set(False, "review", "auto_regenerate")


# ── interactive_review 队列操作 ────────────────────────────────────────


def test_review_accept(tmp_cfg, tmp_path):
    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence"))

    n = review_mod.interactive_review(tmp_cfg, prompt="a", max_items=1)
    assert n == 1
    items = review_mod.load_queue(tmp_cfg)
    assert items[0]["status"] == "accepted"


def test_review_delete_clears_translation(tmp_cfg, tmp_path):
    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    # 有审核条目的页面处于 review 状态（真实条件）
    state.page("page1.html")["status"] = "review"
    state.save()
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence"))

    review_mod.interactive_review(tmp_cfg, prompt="d", max_items=1)

    # 段译文被清除
    st = tr.State(tmp_cfg)
    pstate = st.page("page1.html")
    assert pstate["segments"][sid]["translation"] is None
    # 页面转 pending
    assert pstate["status"] == "pending"
    # done_pages 移除
    assert "page1.html" not in st.data["done_pages"]
    # 队列状态 deleted
    assert review_mod.load_queue(tmp_cfg)[0]["status"] == "deleted"


def test_review_confirm_adds_glossary(tmp_cfg, tmp_path):
    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    detail = "术语「アルバム」已有不同译文，将覆盖为「专辑」"
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid,
                                          "glossary_conflict", detail))

    review_mod.interactive_review(tmp_cfg, prompt="c", max_items=1)

    # 确认操作应把冲突术语加入词汇表（提取「」中的 src）
    items = gl.load(tmp_cfg)
    assert any(it["src"] == "アルバム" for it in items)


def test_review_delete_purges_tm_and_notes(tmp_cfg, tmp_path):
    from booktr import notes as notes_mod
    from booktr import tm as tm_mod
    from booktr import util

    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    state.page("page1.html")["status"] = "review"
    state.save()
    util.append_jsonl(tm_mod._path(tmp_cfg),
                      {"key": "k", "src": "原文", "dst": "旧译",
                       "page": "page1.html", "segment_id": int(sid), "usage_count": 1})
    notes_mod.add(tmp_cfg, "page1.html", int(sid), "q", "旧说明")
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence"))

    review_mod.interactive_review(tmp_cfg, prompt="d", max_items=1)

    recs = util.read_jsonl(tm_mod._path(tmp_cfg))
    assert not any(r["page"] == "page1.html" and str(r["segment_id"]) == str(sid) for r in recs)
    ns = notes_mod.all_notes(tmp_cfg)
    assert not any(n.get("page") == "page1.html" and str(n.get("segment_id")) == str(sid) for n in ns)
    assert review_mod.load_queue(tmp_cfg)[0]["status"] == "deleted"


def test_review_delete_declined_skips(tmp_cfg, tmp_path):
    from booktr import notes as notes_mod
    from booktr import tm as tm_mod
    from booktr import util

    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    state.page("page1.html")["status"] = "review"
    state.save()
    util.append_jsonl(tm_mod._path(tmp_cfg),
                      {"key": "k", "src": "原文", "dst": "旧译",
                       "page": "page1.html", "segment_id": int(sid), "usage_count": 1})
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence"))

    review_mod.interactive_review(tmp_cfg, prompt="d", confirm="n", max_items=1)

    assert review_mod.load_queue(tmp_cfg)[0]["status"] == "skipped"
    # TM 未被清理
    assert any(r["page"] == "page1.html" for r in util.read_jsonl(tm_mod._path(tmp_cfg)))
    # 段译文保留、页面仍 review
    st = tr.State(tmp_cfg)
    assert st.page("page1.html")["segments"][str(sid)]["translation"] is not None


def test_review_skip(tmp_cfg, tmp_path):
    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence"))

    review_mod.interactive_review(tmp_cfg, prompt="s", max_items=1)
    assert review_mod.load_queue(tmp_cfg)[0]["status"] == "skipped"


def test_review_qa_does_not_change_page(tmp_cfg, tmp_path):
    _disable_auto_regenerate(tmp_cfg)
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    # QA 条目：原因以 qa_ 开头
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "qa_high", "占位符不一致"))

    review_mod.interactive_review(tmp_cfg, prompt="a", max_items=1)

    # 队列被标记 accepted
    assert review_mod.load_queue(tmp_cfg)[0]["status"] == "accepted"
    # 页面状态不变（仍为 done，而非因 changed_pages 变 pending 等）
    st = tr.State(tmp_cfg)
    assert st.page("page1.html")["status"] == "done"


# ── _finalize_review 状态流转 ──────────────────────────────────────────


def test_finalize_deleted_keeps_pending(tmp_cfg, tmp_path):
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    # 页面先标记 review，队列含 deleted 条目
    pstate = state.page("page1.html")
    pstate["status"] = "review"
    state.save()
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence",
                                          status="deleted"))

    review_mod._finalize_review(tmp_cfg, "page1.html")

    st = tr.State(tmp_cfg)
    assert st.page("page1.html")["status"] == "pending"


def test_finalize_accepted_goes_done(tmp_cfg, tmp_path):
    state, sid = _make_translated_page(tmp_cfg, tmp_path)
    pstate = state.page("page1.html")
    pstate["status"] = "review"
    state.save()
    review_mod.save_queue(tmp_cfg, _queue(tmp_cfg, "page1.html", sid, "low_confidence",
                                          status="accepted"))

    review_mod._finalize_review(tmp_cfg, "page1.html")

    st = tr.State(tmp_cfg)
    assert st.page("page1.html")["status"] == "done"
    assert "page1.html" in st.data["done_pages"]
