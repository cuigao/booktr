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


def _seed_queue(tmp_cfg):
    items = [
        qq.make_item("a.html", _issue(reason="a1")),
        qq.make_item("a.html", _issue(reason="a2")),
        qq.make_item("b.html", _issue(reason="b1")),
    ]
    # 标记一条 applied，用于状态过滤
    items[0]["status"] = qq.STATUS_APPLIED
    qq.save(tmp_cfg, items)
    return items


def test_remove_default_open_only_and_backup(tmp_cfg):
    _seed_queue(tmp_cfg)
    gone, kept, bak = qq.remove(tmp_cfg)  # 默认 status=open
    assert gone == 2 and kept == 1
    assert bak and os.path.exists(bak)  # 同目录备份
    left = qq.load(tmp_cfg)
    assert len(left) == 1 and left[0]["status"] == qq.STATUS_APPLIED


def test_remove_dry_run_no_side_effects(tmp_cfg):
    _seed_queue(tmp_cfg)
    before = open(qq._path(tmp_cfg), encoding="utf-8").read()
    gone, kept, bak = qq.remove(tmp_cfg, dry_run=True)
    assert gone == 2 and kept == 1 and bak == ""
    assert open(qq._path(tmp_cfg), encoding="utf-8").read() == before
    # 未产生备份文件
    import glob
    assert not glob.glob(qq._path(tmp_cfg) + ".*.bak")


def test_remove_by_page_and_all(tmp_cfg):
    _seed_queue(tmp_cfg)
    gone, kept, _ = qq.remove(tmp_cfg, status="all", page="a.html")
    assert gone == 2 and kept == 1  # 仅删 a.html 的两条
    gone, kept, _ = qq.remove(tmp_cfg, status="all")
    assert gone == 1 and kept == 0  # 清空
    assert qq.load(tmp_cfg) == []


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


def _feed(monkeypatch, lines):
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it, ""))


def test_interactive_custom_override(tmp_cfg, monkeypatch):
    """[e] 输入建议+理由 → 覆盖有效字段、归档 llm_*、source=human、adopted。"""
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue(reason="r", suggestion="s"))])
    _feed(monkeypatch, ["e", "我撞见了坏心眼", "拟人化，改动词更自然"])
    qq.interactive_qa_review(tmp_cfg)
    it = qq.load(tmp_cfg)[0]
    assert it["status"] == qq.STATUS_ADOPTED
    assert it["source"] == "human"
    assert it["suggestion"] == "我撞见了坏心眼"
    assert it["reason"] == "拟人化，改动词更自然"
    assert it["llm_suggestion"] == "s"
    assert it["llm_reason"] == "r"


def test_interactive_custom_reuse_llm_field(tmp_cfg, monkeypatch):
    """[e] 某字段回车 → 沿用 LLM 原值；另一字段覆盖。"""
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue(reason="rr", suggestion="ss"))])
    _feed(monkeypatch, ["e", "", "只改说明"])
    qq.interactive_qa_review(tmp_cfg)
    it = qq.load(tmp_cfg)[0]
    assert it["suggestion"] == "ss"  # 沿用
    assert it["reason"] == "只改说明"
    assert it["source"] == "human"


def test_interactive_custom_clear_field(tmp_cfg, monkeypatch):
    """[e] 某字段输入 '-' → 该字段清空。"""
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue(reason="rr", suggestion="ss"))])
    _feed(monkeypatch, ["e", "-", "改法不同"])
    qq.interactive_qa_review(tmp_cfg)
    it = qq.load(tmp_cfg)[0]
    assert it["suggestion"] == ""
    assert it["reason"] == "改法不同"
    assert it["llm_suggestion"] == "ss"


def test_interactive_custom_no_change_cancels(tmp_cfg, monkeypatch, capsys):
    """[e] 两个字段均回车 → 无变化，取消、不采纳。"""
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue(reason="rr", suggestion="ss"))])
    _feed(monkeypatch, ["e", "", ""])
    qq.interactive_qa_review(tmp_cfg)
    it = qq.load(tmp_cfg)[0]
    assert it["status"] == qq.STATUS_OPEN
    assert "source" not in it
    assert "未做修改" in capsys.readouterr().out


def test_interactive_custom_unresolved_blocked(tmp_cfg, monkeypatch):
    """[e] 未定位条目 → 被拦截、状态不变。"""
    it0 = qq.make_item("p.html", _issue(segments=[], resolved=False))
    qq.append_items(tmp_cfg, [it0])
    _feed(monkeypatch, ["e"])
    qq.interactive_qa_review(tmp_cfg)
    assert qq.load(tmp_cfg)[0]["status"] == qq.STATUS_OPEN


def test_interactive_custom_not_in_automation(tmp_cfg, capsys):
    """自动化模式 prompt='e' → 不支持、无副作用。"""
    qq.append_items(tmp_cfg, [qq.make_item("p.html", _issue())])
    qq.interactive_qa_review(tmp_cfg, prompt="e")
    it = qq.load(tmp_cfg)[0]
    assert it["status"] == qq.STATUS_OPEN
    assert "自动化模式不支持" in capsys.readouterr().out
