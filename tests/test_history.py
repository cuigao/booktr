# -*- coding: utf-8 -*-
"""段落历史/回滚测试：提交即版本、计划/应用分离、dry-run 无副作用。"""
from __future__ import annotations

import os

import pytest

from booktr import history as hist
from booktr import tm as tm_mod
from booktr import translate as tr
from booktr import util
from conftest import FakeLLM, write_sample_site


def _hashes(cfg, paths):
    out = {}
    for p in paths:
        out[p] = util.read_json(p, None) if p.endswith(".json") else None
    return out


def _file_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _snapshot_cfg_files(cfg):
    """快照 state/segments/tm/notes/out 的字节，用于 dry-run 无副作用断言。"""
    files = [
        os.path.join(cfg.work_dir, "state.json"),
        tm_mod._path(cfg),
        os.path.join(cfg.work_dir, "notes.jsonl"),
        os.path.join(cfg.work_dir, "segments", "page1.html.json"),
        os.path.join(cfg.output_dir, "page1.html"),
    ]
    return {p: _file_bytes(p) for p in files}


def _translate(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", state, {}, {}, [])
    return state


# ── 提交即版本 ──────────────────────────────────────────────────────────

def test_commit_creates_version(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    vs1 = hist.versions(tmp_cfg, "page1.html", "1")
    assert len(vs1) == 1
    assert vs1[0]["op"] == "translate"
    assert vs1[0]["state"]["translation"]
    assert vs1[0]["id"].startswith("ver_")


def test_commit_all_segments(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    segs = tr.seg_mod.segments_for_page(tmp_cfg, "page1.html")
    text_ids = [str(s.id) for s in segs if s.kind == "text"]
    for sid in text_ids:
        assert len(hist.versions(tmp_cfg, "page1.html", sid)) >= 1


def test_commit_disabled(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    tmp_cfg.set(False, "segment_history", "enabled")
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", state, {}, {}, [])
    assert hist.versions(tmp_cfg, "page1.html", "1") == []


# ── 计划：diff / 目标解析 ───────────────────────────────────────────────

def test_inline_diff():
    d = hist.inline_diff("散步", "跑步")
    assert "[-散-]" in d and "{+跑+}" in d and d.endswith("步")


def test_resolve_target_segments(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[1])
    assert targets and targets[0]["sid"] == "1"
    # 仅一个版本（首次翻译），无更早不同版本 → to_version 为 None
    assert targets[0]["to_version"] is None


def test_resolve_target_src_fragment(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    targets = hist.resolve_targets(tmp_cfg, "page1.html", src_frag="こんにちは")
    assert targets and targets[0]["sid"]  # 定位到某段


def test_plan_restore_shape(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    # 再手动追加一个不同版本：模拟 qa-apply 覆盖
    sid = "1"
    st = tr.State(tmp_cfg)
    pseg = st.page("page1.html")["segments"][sid]
    old = pseg["translation"]
    pseg["translation"] = old + "【改】"
    pseg["confidence"] = 0.99
    st.save()
    hist.commit(tmp_cfg, "page1.html", sid, "qa-apply", "op_test", state=st)

    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[1])
    assert targets[0]["to_version"] is not None
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    seg = plan["segments"][0]
    assert seg["from"]["translation"] == old + "【改】"
    assert seg["to"]["translation"] == old
    assert plan["page"] == "page1.html"
    # 渲染不抛异常且包含 diff
    text = hist.format_plan(plan)
    assert "段1" in text and "译文 diff" in text


# ── 应用回滚 ────────────────────────────────────────────────────────────

def test_apply_restore_roundtrip(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    sid = "1"
    st = tr.State(tmp_cfg)
    pseg = st.page("page1.html")["segments"][sid]
    old = pseg["translation"]
    pseg["translation"] = "被覆盖的译文"
    st.save()

    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[1])
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    res = hist.apply_plan(tmp_cfg, plan)
    assert res["restored"] == 1

    st2 = tr.State(tmp_cfg)
    assert st2.page("page1.html")["segments"][sid]["translation"] == old
    # 段缓存同步
    segs = tr.seg_mod.segments_for_page(tmp_cfg, "page1.html")
    cache_seg = next(s for s in segs if str(s.id) == sid)
    assert cache_seg.translation == old
    # 追加了 rollback 版本
    vs = hist.versions(tmp_cfg, "page1.html", sid)
    assert vs[-1]["op"] == "rollback"
    assert vs[-1]["restored_from"]


def test_rollback_restores_page_status_and_done_pages(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    # 模拟 reset 整页：清空 state
    st = tr.State(tmp_cfg)
    for sid in list(st.page("page1.html")["segments"].keys()):
        hist.commit(tmp_cfg, "page1.html", sid, "reset", "op_r", state=st)
        st.page("page1.html")["segments"][sid]["translation"] = None
    st.page("page1.html")["status"] = "pending"
    if "page1.html" in st.data["done_pages"]:
        st.data["done_pages"].remove("page1.html")
    st.save()

    # 对每段恢复到 reset 前版本
    sids = [int(s) for s in hist._load(tmp_cfg, "page1.html")["segments"].keys()]
    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=sids)
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    hist.apply_plan(tmp_cfg, plan)

    st2 = tr.State(tmp_cfg)
    assert st2.page("page1.html")["status"] == "done"
    assert "page1.html" in st2.data["done_pages"]


def test_dry_run_no_side_effects(tmp_cfg, tmp_path):
    """dry-run 计划构建不得修改任何文件（核心保证）。"""
    _translate(tmp_cfg, tmp_path)
    sid = "1"
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"][sid]["translation"] = "覆盖后"
    st.save()

    before = _snapshot_cfg_files(tmp_cfg)
    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[1])
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    hist.format_plan(plan)
    after = _snapshot_cfg_files(tmp_cfg)
    assert before == after


# ── TM 安全恢复 ─────────────────────────────────────────────────────────

def test_tm_collision_skipped(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    # 开启 TM 并造一条属于段1的 TM，再让另一页占用同 key
    tm_mod.add(tmp_cfg, "こんにちは", "你好", "page1.html", 1)
    tm_mod.add(tmp_cfg, "こんにちは", "你好", "other.html", 9)  # 抢占 key（同 key 更新属主）
    # 此时快照里段1的 TM 记录为 key=こんにちは；属主已是 other.html
    st = tr.State(tmp_cfg)
    st.page("page1.html")["segments"]["1"]["translation"] = "改后"
    st.save()
    targets = hist.resolve_targets(tmp_cfg, "page1.html", sids=[1])
    plan = hist.plan_restore(tmp_cfg, "page1.html", targets)
    # 快照无 TM（提交时机在 add 之前）时跳过数可能为 0；这里直接验证 reconcile 逻辑
    from booktr import history as h
    tm_all = util.read_jsonl(tm_mod._path(tmp_cfg))
    rec = {"key": "こんにちは", "src": "こんにちは", "dst": "你好",
           "page": "page1.html", "segment_id": 1, "usage_count": 1}
    r = h._reconcile_tm(tmp_cfg, "page1.html", "1", [rec], tm_all)
    assert r["skipped_collisions"] and r["skipped_collisions"][0]["owner_page"] == "other.html"


# ── purge / backfill ────────────────────────────────────────────────────

def test_purge_keep_last(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    sid = "1"
    st = tr.State(tmp_cfg)
    for i in range(5):
        st.page("page1.html")["segments"][sid]["translation"] = f"v{i}"
        st.save()
        hist.commit(tmp_cfg, "page1.html", sid, "translate", f"op{i}", state=st)
    hist.purge(tmp_cfg, page="page1.html", keep_last=3)
    assert len(hist.versions(tmp_cfg, "page1.html", sid)) == 3


def test_purge_all_files(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    info = hist.purge(tmp_cfg, page=None, keep_last=None)
    assert info["removed_files"] >= 1
    assert hist.versions(tmp_cfg, "page1.html", "1") == []


def test_backfill(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 手工造 state（无历史）
    st = tr.State(tmp_cfg)
    p = st.page("page1.html")
    p["segments"] = {"1": {"translation": "已有译文", "confidence": 1.0}}
    p["status"] = "done"
    st.data["done_pages"].append("page1.html")
    st.save()
    n = hist.backfill(tmp_cfg)
    assert n >= 1
    vs = hist.versions(tmp_cfg, "page1.html", "1")
    assert vs and vs[0]["op"] == "backfill"
    # 再跑一次不重复补录
    assert hist.backfill(tmp_cfg) == 0
