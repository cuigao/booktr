# -*- coding: utf-8 -*-
"""reset 清理 TM/notes 测试。"""
from __future__ import annotations

import argparse
import os

from booktr import notes as notes_mod
from booktr import tm as tm_mod
from booktr import translate as tr
from booktr import util
from booktr.pipeline import cmd_reset


def _args(**kw):
    base = {"pages": None, "segments": None, "all_segments": False,
            "all": False, "yes": True, "keep_tm": False, "keep_notes": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _write_page(tmp_cfg, rel, segs, status="done"):
    seg_path = os.path.join(tmp_cfg.get("segments_dir", default=""),
                            rel.replace("/", "__") + ".json")
    util.write_json(seg_path, {"encoding": "", "segments": segs})
    state = tr.State(tmp_cfg)
    pstate = state.page(rel)
    pstate["status"] = status
    for s in segs:
        pstate.setdefault("segments", {})[str(s["id"])] = {
            "translation": s.get("translation"), "confidence": 0.9,
            "needs_human": False, "untrusted": False,
        }
    state.save()


def _add_tm(tmp_cfg, page, sid, src, dst):
    util.append_jsonl(tm_mod._path(tmp_cfg),
                      {"key": util.normalize_ws(src), "src": src, "dst": dst,
                       "page": page, "segment_id": sid, "usage_count": 1})


def _tm_count(tmp_cfg):
    return len(util.read_jsonl(tm_mod._path(tmp_cfg)))


def test_reset_segment_purges_only_that_segment(tmp_cfg):
    segs = [
        {"id": 4, "kind": "text", "text": "あいうえお順", "translation": "あいうえお顺序"},
        {"id": 5, "kind": "text", "text": "こんにちは", "translation": "你好"},
    ]
    _write_page(tmp_cfg, "today/today4.html", segs)
    _write_page(tmp_cfg, "today/today9.html",
                [{"id": 4, "kind": "text", "text": "そして", "translation": "そして"}])
    _add_tm(tmp_cfg, "today/today4.html", 4, "あいうえお順", "あいうえお顺序")
    _add_tm(tmp_cfg, "today/today4.html", 5, "こんにちは", "你好")
    _add_tm(tmp_cfg, "today/today9.html", 4, "そして", "そして")
    notes_mod.add(tmp_cfg, "today/today4.html", 4, "q", "旧说明A")
    notes_mod.add(tmp_cfg, "today/today4.html", 5, "q", "旧说明B")

    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"], segments=[4]))

    # 仅 seg4 的 TM/notes 被清
    recs = util.read_jsonl(tm_mod._path(tmp_cfg))
    assert not any(r["page"] == "today/today4.html" and r["segment_id"] == 4 for r in recs)
    assert any(r["page"] == "today/today4.html" and r["segment_id"] == 5 for r in recs)
    assert any(r["page"] == "today/today9.html" for r in recs)
    ns = notes_mod.all_notes(tmp_cfg)
    assert not any(n.get("page") == "today/today4.html" and n.get("segment_id") == 4 for n in ns)
    assert any(n.get("page") == "today/today4.html" and n.get("segment_id") == 5 for n in ns)
    # state：seg4 译文清空，页面 pending
    st = tr.State(tmp_cfg)
    pstate = st.page("today/today4.html")
    assert pstate["segments"]["4"]["translation"] is None
    assert pstate["segments"]["5"]["translation"] == "你好"
    assert pstate["status"] == "pending"


def test_reset_page_purges_whole_page(tmp_cfg):
    segs = [{"id": 1, "kind": "text", "text": "a", "translation": "A"}]
    _write_page(tmp_cfg, "today/today4.html", segs)
    _write_page(tmp_cfg, "today/today9.html", [{"id": 1, "kind": "text", "text": "b", "translation": "B"}])
    _add_tm(tmp_cfg, "today/today4.html", 1, "a", "A")
    _add_tm(tmp_cfg, "today/today9.html", 1, "b", "B")
    notes_mod.add(tmp_cfg, "today/today4.html", 1, "q", "说明")

    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"]))  # 整页

    recs = util.read_jsonl(tm_mod._path(tmp_cfg))
    assert not any(r["page"] == "today/today4.html" for r in recs)
    assert any(r["page"] == "today/today9.html" for r in recs)
    assert not any(n.get("page") == "today/today4.html" for n in notes_mod.all_notes(tmp_cfg))


def test_reset_keep_tm_and_notes(tmp_cfg):
    segs = [{"id": 4, "kind": "text", "text": "x", "translation": "X"}]
    _write_page(tmp_cfg, "today/today4.html", segs)
    _add_tm(tmp_cfg, "today/today4.html", 4, "x", "X")
    notes_mod.add(tmp_cfg, "today/today4.html", 4, "q", "说明")

    # 保留 TM → TM 不删，notes 仍清
    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"], segments=[4], keep_tm=True))
    assert _tm_count(tmp_cfg) == 1
    assert not notes_mod.all_notes(tmp_cfg)

    # 保留 notes → notes 不删，TM 仍清
    notes_mod.add(tmp_cfg, "today/today4.html", 4, "q", "说明2")
    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"], segments=[4], keep_notes=True))
    assert notes_mod.all_notes(tmp_cfg)
    assert _tm_count(tmp_cfg) == 0


def test_reset_keeps_user_note(tmp_cfg):
    segs = [{"id": 4, "kind": "text", "text": "x", "translation": "X"}]
    _write_page(tmp_cfg, "today/today4.html", segs)
    notes_mod.add_user(tmp_cfg, "用户注入的重要提示")
    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"], segments=[4]))
    ns = notes_mod.all_notes(tmp_cfg)
    assert any(n.get("created_by") == "user" for n in ns)


def test_reset_preview_before_confirm(tmp_cfg, capsys):
    segs = [{"id": 4, "kind": "text", "text": "x", "translation": "X"}]
    _write_page(tmp_cfg, "today/today4.html", segs)
    _add_tm(tmp_cfg, "today/today4.html", 4, "x", "X")
    notes_mod.add(tmp_cfg, "today/today4.html", 4, "q", "说明")

    cmd_reset(tmp_cfg, _args(pages=["today/today4.html"], segments=[4]))
    out = capsys.readouterr().out
    assert "将重置 1 页" in out
    assert "翻译记忆 1 条、翻译笔记 1 条" in out
