# -*- coding: utf-8 -*-
"""短语记忆对称清理 / 学习 / qa-apply 回写 测试。"""
from __future__ import annotations

import os
from argparse import Namespace

import pytest

from booktr import phrases as ph
from booktr import pipeline
from booktr import translate as tr
from conftest import FakeLLM, _ok_json, write_sample_site


# ── learn 门槛 ──────────────────────────────────────────────────────────

def test_learn_basic(tmp_cfg):
    assert ph.learn(tmp_cfg, "HOME", "首页") is True
    assert ph.lookup(tmp_cfg, "HOME") == "首页"


def test_learn_rejects_long(tmp_cfg):
    assert ph.learn(tmp_cfg, "あ" * 40, "译文") is False
    assert ph.size(tmp_cfg) == 0


def test_learn_placeholder_edges_ok(tmp_cfg):
    assert ph.learn(tmp_cfg, "[[P0]]HOME[[P1]]", "[[P0]]首页[[P1]]") is True


def test_learn_rejects_middle_placeholder(tmp_cfg):
    assert ph.learn(tmp_cfg, "ああ[[P0]]いい", "译文") is False
    assert ph.size(tmp_cfg) == 0


def test_learn_rejects_leaked_marker(tmp_cfg):
    assert ph.learn(tmp_cfg, "TOP", "|TEXT|") is False
    assert ph.size(tmp_cfg) == 0


# ── purge 条件矩阵 ──────────────────────────────────────────────────────

def test_segment_removals_all_conditions(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    # ① k∈S ② d∈F ③ d∉T → 删
    assert ph.segment_removals(tmp_cfg, "HOME", "首页", "") == ["HOME"]


def test_segment_removals_kept_when_unchanged(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    # d ∈ T（译文未变）→ 保留
    assert ph.segment_removals(tmp_cfg, "HOME", "首页", "首页") == []


def test_segment_removals_kept_when_key_absent(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    # k ∉ S → 保留
    assert ph.segment_removals(tmp_cfg, "OTHER", "首页", "") == []


def test_segment_removals_kept_when_dst_not_in_from(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    # d ∉ F → 保留（该短语未被本段旧译文用到）
    assert ph.segment_removals(tmp_cfg, "HOME", "别的译文", "") == []


def test_segment_removals_plain_with_placeholders(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    assert ph.segment_removals(tmp_cfg, "[[P0]]HOME[[P1]]",
                               "[[P0]]首页[[P1]]", "") == ["HOME"]


def test_purge_for_segment_removes(tmp_cfg):
    ph.add(tmp_cfg, "HOME", "首页")
    assert ph.purge_for_segment(tmp_cfg, "HOME", "首页", "主页") == 1
    assert ph.lookup(tmp_cfg, "HOME") is None


# ── reset 清短语 ────────────────────────────────────────────────────────

def test_reset_purges_phrases(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", tr.State(tmp_cfg), {}, {}, [])
    st = tr.State(tmp_cfg)
    # seg6 源文为 HOME，把当前译文设为「首页」并登记短语
    st.page("page1.html")["segments"]["6"]["translation"] = "首页"
    st.save()
    ph.add(tmp_cfg, "HOME", "首页")

    args = Namespace(all=False, pages=["page1.html"], segments=[6],
                     all_segments=False, keep_tm=False, keep_notes=False, yes=True)
    pipeline.cmd_reset(tmp_cfg, args)
    assert ph.lookup(tmp_cfg, "HOME") is None


# ── qa-apply 清 + 回写 ──────────────────────────────────────────────────

def test_qa_apply_purges_and_relearns(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", tr.State(tmp_cfg), {}, {}, [])
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"]["6"]["translation"] = "首页"
    st.save()
    ph.add(tmp_cfg, "HOME", "首页")

    fake = FakeLLM(sequence=[_ok_json("主页")])
    r = tr.apply_qa_fix(tmp_cfg, fake, "page1.html", "6",
                        [{"severity": "low", "reason": "改", "src_quote": "HOME",
                          "dst_quote": "首页", "suggestion": "主页"}],
                        st, {}, {})
    assert r["ok"]
    # 旧短语被清、新短语被回写（与 TM 对齐）
    assert ph.lookup(tmp_cfg, "HOME") == "主页"


def test_qa_apply_keeps_phrase_when_unchanged(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", tr.State(tmp_cfg), {}, {}, [])
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"]["6"]["translation"] = "首页"
    st.save()
    ph.add(tmp_cfg, "HOME", "首页")

    # 新译文仍含「首页」→ 不被清理（条件③不成立），而是随重译更新为新值
    fake = FakeLLM(sequence=[_ok_json("首页（修正）")])
    tr.apply_qa_fix(tmp_cfg, fake, "page1.html", "6",
                    [{"severity": "low", "reason": "r", "src_quote": "HOME",
                      "dst_quote": "首页", "suggestion": "首页（修正）"}],
                    st, {}, {})
    assert ph.segment_removals(tmp_cfg, "HOME", "首页", "首页（修正）") == []
    assert ph.lookup(tmp_cfg, "HOME") == "首页（修正）"


# ── review [d] 清短语 ───────────────────────────────────────────────────

def test_review_delete_purges_phrases(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", tr.State(tmp_cfg), {}, {}, [])
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"]["6"]["translation"] = "首页"
    st.data["done_pages"] = ["page1.html"]
    st.save()
    ph.add(tmp_cfg, "HOME", "首页")

    from booktr import review as review_mod
    review_mod.save_queue(tmp_cfg, [{
        "page": "page1.html", "segment_id": 6, "src": "HOME",
        "reason": "glossary_conflict", "detail": "x", "status": "open"}])
    review_mod.interactive_review(tmp_cfg, prompt="d", confirm="y")
    assert ph.lookup(tmp_cfg, "HOME") is None


# ── rollback 计划含短语清理 ─────────────────────────────────────────────

def test_rollback_plan_and_apply_phrases(tmp_cfg, tmp_path):
    from booktr import history as hist

    write_sample_site(tmp_path)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", tr.State(tmp_cfg), {}, {}, [])
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"]["6"]["translation"] = "首页"
    st.save()
    ph.add(tmp_cfg, "HOME", "首页")

    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[6])
    assert targets[0]["to_version"] is not None
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    seg = plan["segments"][0]
    assert "HOME" in seg["phrases"]
    assert plan["totals"]["phrases_remove"] >= 1

    hist.apply_plan(tmp_cfg, plan)
    assert ph.lookup(tmp_cfg, "HOME") is None
