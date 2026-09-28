# -*- coding: utf-8 -*-
"""残留假名检测测试：find_residual 纯函数 + cmd_check_residual 流程。"""
from __future__ import annotations

import argparse
import json
import os

from booktr import residual
from booktr import util
from booktr.pipeline import cmd_check_residual


# ── find_residual 纯函数 ────────────────────────────────────────────────


def test_detects_bare_hiragana():
    src = "そして、頑張っている人をみかけた時には、応援者の気持ちで見ていたいです。"
    dst = "そして、当我看到正在努力拼搏的人时，我想以支持者的心情去看待他们。"
    hits = residual.find_residual(src, dst)
    assert [h["token"] for h in hits] == ["そして"]


def test_skips_annotated_original_title():
    """日文原名（中译）：假名串后紧跟注解括号，视为有意保留。"""
    src = "「まっすぐな心で」は、現在も本人が一番好きな曲。"
    dst = "《まっすぐな心で》（以坦率的心）至今仍是本人最喜欢的一首曲子。"
    assert residual.find_residual(src, dst) == []


def test_skips_inside_quotes():
    """位于引号内的假名不报（有意保留的引用）。"""
    src = "曲名は「おはよう」です。"
    dst = "曲名是「おはよう」。"
    assert residual.find_residual(src, dst) == []


def test_skips_ascii_quote_depth():
    """ASCII 双引号成对切换，引号内的假名不报。"""
    src = '今日の結論は、"思い込み"だ。'
    dst = '今天的结论是"思い込み"。'
    assert residual.find_residual(src, dst) == []


def test_skips_when_not_in_source():
    """译文出现的假名若不在原文，属臆造，不在此列表。"""
    src = "こんにちは。"
    dst = "你好ですか。"
    assert residual.find_residual(src, dst) == []


def test_detects_multiple_tokens():
    src = "めぐとまゆが来た。"
    dst = "めぐ和まゆ来了。"
    toks = [h["token"] for h in residual.find_residual(src, dst)]
    assert toks == ["めぐ", "まゆ"]


def test_excerpt_contains_token():
    src = "。そして、次へ。"
    dst = "。そして、次へ。"
    hits = residual.find_residual(src, dst)
    assert hits and hits[0]["token"] in hits[0]["excerpt"]


# ── cmd_check_residual 流程 ──────────────────────────────────────────────


def _args(**kw):
    base = {"pages": None, "json": None, "no_report": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _write_page(tmp_cfg, rel, segs, status="done"):
    """写入段缓存与 state，模拟已译页面。"""
    seg_path = os.path.join(tmp_cfg.get("segments_dir", default=""),
                            rel.replace("/", "__") + ".json")
    util.write_json(seg_path, {"encoding": "", "segments": segs})
    from booktr import translate as tr
    state = tr.State(tmp_cfg)
    state.page(rel)["status"] = status
    state.save()


def test_cmd_lists_and_writes_report(tmp_cfg, capsys):
    segs = [
        {"id": 1, "kind": "text", "text": "そして、次へ。", "translation": "そして、次へ。"},
        {"id": 2, "kind": "text",
         "text": "「まっすぐな心で」は好き。",
         "translation": "《まっすぐな心で》（以坦率的心）很喜欢。"},
    ]
    _write_page(tmp_cfg, "today/today3.html", segs)
    cmd_check_residual(tmp_cfg, _args())
    out = capsys.readouterr().out
    assert "そして" in out
    assert "reset today/today3.html --segments 1" in out
    # 注解条目不应出现
    assert "まっすぐ" not in out
    report = util.read_json(os.path.join(tmp_cfg.work_dir, "residual_report.json"), {})
    assert report["total_segments"] == 1
    assert report["pages"][0]["page"] == "today/today3.html"


def test_cmd_no_report_flag(tmp_cfg):
    segs = [{"id": 1, "kind": "text", "text": "そして、次へ。", "translation": "そして、次へ。"}]
    _write_page(tmp_cfg, "today/today3.html", segs)
    cmd_check_residual(tmp_cfg, _args(no_report=True))
    assert not os.path.exists(os.path.join(tmp_cfg.work_dir, "residual_report.json"))


def test_cmd_groups_segments_per_page(tmp_cfg, capsys):
    segs = [
        {"id": 4, "kind": "text", "text": "そして、次。", "translation": "そして、次。"},
        {"id": 8, "kind": "text", "text": "まゆが来た。", "translation": "まゆ来了。"},
    ]
    _write_page(tmp_cfg, "today/today9.html", segs)
    cmd_check_residual(tmp_cfg, _args(no_report=True))
    out = capsys.readouterr().out
    assert "reset today/today9.html --segments 4 8" in out


def test_cmd_skips_non_japanese_source(tmp_cfg, capsys):
    tmp_cfg.set("en", "lang", "source")
    cmd_check_residual(tmp_cfg, _args())
    out = capsys.readouterr().out
    assert "暂无残留检测规则" in out
    assert not os.path.exists(os.path.join(tmp_cfg.work_dir, "residual_report.json"))
