# -*- coding: utf-8 -*-
"""QA 修正定点重译测试：build_qa_fix_user + apply_qa_fix + qa-apply 分组。"""
from __future__ import annotations

import argparse
import json
import os

from booktr import prompts
from booktr import qa_queue as qq
from booktr import translate as tr
from booktr import util
from booktr.pipeline import cmd_qa_apply
from conftest import FakeLLM, write_sample_site


def _prep(tmp_cfg, tmp_path, translation="旧译文XYZ"):
    """准备一个已译页面，段 2 译文可控。"""
    write_sample_site(tmp_path)
    seg_dir = tmp_cfg.get("segments_dir", default="")
    util.write_json(os.path.join(seg_dir, "page1.html.json"), {
        "encoding": "utf-8",
        "segments": [
            {"id": 2, "kind": "text", "text": "こんにちは。",
             "translation": translation, "start": 0, "end": 6},
        ],
    })
    state = tr.State(tmp_cfg)
    ps = state.page("page1.html")
    ps["status"] = "done"
    ps.setdefault("segments", {})["2"] = {
        "translation": translation, "confidence": 0.9,
        "needs_human": False, "untrusted": False}
    state.data["done_pages"] = ["page1.html"]
    state.save()


def test_build_qa_fix_user_contains_opinion_and_current(tmp_cfg):
    ops = [{"severity": "mid", "reason": "误译", "src_quote": "こんにちは",
            "dst_quote": "旧译", "suggestion": "改为你好"}]
    usr = prompts.build_qa_fix_user(tmp_cfg, "こんにちは。", "旧译XYZ", ops, {})
    assert "QA 审核意见" in usr
    assert "误译" in usr and "改为你好" in usr
    assert "现有译文" in usr and "旧译XYZ" in usr
    assert "こんにちは。" in usr


def test_apply_qa_fix_updates_translation_and_state(tmp_cfg, tmp_path):
    _prep(tmp_cfg, tmp_path)
    # FakeLLM 恒等：把待翻译文本返回为译文
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    ops = [{"severity": "mid", "reason": "r", "src_quote": "こんにちは。",
            "dst_quote": "旧译文XYZ", "suggestion": "s"}]
    r = tr.apply_qa_fix(tmp_cfg, fake, "page1.html", "2", ops, state, {}, {})
    assert r["ok"] is True
    # apply_qa_fix 只改内存 state（由调用方保存）；读同一个 state 对象
    new_t = state.page("page1.html")["segments"]["2"]["translation"]
    assert new_t and new_t != "旧译文XYZ"
    # 段缓存同步
    cache = util.read_json(os.path.join(
        tmp_cfg.get("segments_dir", default=""), "page1.html.json"), {})
    seg2 = [s for s in cache["segments"] if s["id"] == 2][0]
    assert seg2["translation"] == new_t


def _adopt_all(tmp_cfg):
    items = qq.load(tmp_cfg)
    for it in items:
        it["status"] = qq.STATUS_ADOPTED
    qq.save(tmp_cfg, items)


def test_cmd_qa_apply_groups_and_marks(tmp_cfg, tmp_path):
    _prep(tmp_cfg, tmp_path)
    # 两条意见指向同一段
    qq.append_items(tmp_cfg, [
        qq.make_item("page1.html", {"severity": "mid", "reason": "r1",
                                    "src_quote": "a", "dst_quote": "b",
                                    "suggestion": "s", "segments": [2],
                                    "resolved": True}),
        qq.make_item("page1.html", {"severity": "low", "reason": "r2",
                                    "src_quote": "c", "dst_quote": "d",
                                    "suggestion": "t", "segments": [2],
                                    "resolved": True}),
    ])
    _adopt_all(tmp_cfg)

    import booktr.pipeline as pl
    orig = pl._client
    pl._client = lambda cfg: FakeLLM.default()
    try:
        cmd_qa_apply(tmp_cfg, argparse.Namespace(page=None, dry_run=False))
    finally:
        pl._client = orig
    items = qq.load(tmp_cfg)
    assert all(it["status"] == qq.STATUS_APPLIED for it in items)


def test_cmd_qa_apply_dry_run(tmp_cfg, tmp_path, capsys):
    _prep(tmp_cfg, tmp_path)
    qq.append_items(tmp_cfg, [qq.make_item("page1.html", {
        "severity": "low", "reason": "r", "src_quote": "", "dst_quote": "",
        "suggestion": "s", "segments": [2], "resolved": True})])
    _adopt_all(tmp_cfg)
    cmd_qa_apply(tmp_cfg, argparse.Namespace(page=None, dry_run=True))
    out = capsys.readouterr().out
    assert "段2" in out
    # 未应用
    assert qq.load(tmp_cfg)[0]["status"] == qq.STATUS_ADOPTED
