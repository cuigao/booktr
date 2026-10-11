# -*- coding: utf-8 -*-
"""qa-add 命令测试：人工新增 QA 条目、定位、强制段号、adopt、去重、dry-run。"""
from __future__ import annotations

import types

from booktr import pipeline
from booktr import qa_queue
from booktr import translate as tr
from conftest import FakeLLM, write_sample_site


def _translate(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    st = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM.default(), "page1.html", st, {}, {}, [])
    return st


def _args(**kw):
    d = dict(page="page1.html", src="", dst="", reason="", suggestion="",
             severity=None, segment=None, adopt=False, dry_run=False, yes=True)
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_qa_add_locate(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    pipeline.cmd_qa_add(tmp_cfg, _args(src="こんにちは。", reason="漏译", suggestion="你好"))
    items = qa_queue.load(tmp_cfg)
    assert len(items) == 1
    it = items[0]
    assert it["source"] == "human" and it["resolved"] and it["segments"]
    assert it["status"] == "open" and it["severity"] == "high"


def test_qa_add_force_segment(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    pipeline.cmd_qa_add(tmp_cfg, _args(segment=[2], reason="r", suggestion="s"))
    it = qa_queue.load(tmp_cfg)[0]
    assert it["segments"] == [2] and it["resolved"] is True


def test_qa_add_adopt(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    pipeline.cmd_qa_add(tmp_cfg, _args(segment=[2], adopt=True))
    assert qa_queue.load(tmp_cfg)[0]["status"] == "adopted"


def test_qa_add_dedup(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    a = _args(segment=[2], reason="r", suggestion="s")
    pipeline.cmd_qa_add(tmp_cfg, a)
    pipeline.cmd_qa_add(tmp_cfg, a)
    assert len(qa_queue.load(tmp_cfg)) == 1


def test_qa_add_unresolved(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    pipeline.cmd_qa_add(tmp_cfg, _args(src="不存在的片段xyz", reason="r"))
    it = qa_queue.load(tmp_cfg)[0]
    assert it["resolved"] is False and it["segments"] == []


def test_qa_add_dry_run(tmp_cfg, tmp_path):
    _translate(tmp_cfg, tmp_path)
    pipeline.cmd_qa_add(tmp_cfg, _args(segment=[2], dry_run=True))
    assert qa_queue.load(tmp_cfg) == []
