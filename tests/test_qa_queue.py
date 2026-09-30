# -*- coding: utf-8 -*-
"""QA 队列与交互裁定测试。"""
from __future__ import annotations

import os

from booktr import qa_queue as qq
from booktr import util


def _issue(**kw):
    base = {"severity": "low", "reason": "r", "src_quote": "原文",
            "dst_quote": "译文", "suggestion": "s", "segments": [1],
            "resolved": True}
    base.update(kw)
    return base


def test_make_and_dedup(tmp_cfg):
    it = qq.make_item("page1.html", _issue())
    assert it["id"].startswith("qa_")
    assert it["status"] == "open"
    n = qq.append_items(tmp_cfg, [it, it])
    assert n == 1
    # 再次并入同一条 → 不新增
    assert qq.append_items(tmp_cfg, [qq.make_item("page1.html", _issue())]) == 0
    assert len(qq.load(tmp_cfg)) == 1


def test_stats(tmp_cfg):
    qq.append_items(tmp_cfg, [
        qq.make_item("p.html", _issue(severity="high")),
        qq.make_item("p.html", _issue(reason="r2", severity="mid")),
    ])
    s = qq.stats(tmp_cfg)
    assert s["total"] == 2 and s["open"] == 2
    assert s["by_severity"] == {"high": 1, "mid": 1}


def test_interactive_adopt(tmp_cfg, capsys):
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue())])
    qq.interactive_qa_review(tmp_cfg, prompt="a")
    items = qq.load(tmp_cfg)
    assert items[0]["status"] == qq.STATUS_ADOPTED


def test_interactive_reject(tmp_cfg):
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue())])
    qq.interactive_qa_review(tmp_cfg, prompt="r")
    assert qq.load(tmp_cfg)[0]["status"] == qq.STATUS_REJECTED


def test_interactive_drop(tmp_cfg):
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue())])
    qq.interactive_qa_review(tmp_cfg, prompt="d")
    assert qq.load(tmp_cfg) == []


def test_interactive_unresolved_cannot_adopt(tmp_cfg, capsys):
    it = qq.make_item("p.html", _issue(segments=[], resolved=False))
    qq.append_items(tmp_cfg, [it])
    qq.interactive_qa_review(tmp_cfg, prompt="a")
    # 未定位 → 采纳被拒，仍为 open
    assert qq.load(tmp_cfg)[0]["status"] == qq.STATUS_OPEN
    out = capsys.readouterr().out
    assert "未定位" in out
