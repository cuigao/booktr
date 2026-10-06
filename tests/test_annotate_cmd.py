# -*- coding: utf-8 -*-
"""cmd_annotate：续跑跳过已生成页、--force 重生成、单页容错。"""
from __future__ import annotations

import json
import os

from booktr import annotator as ann
from booktr import llm as llm_mod
from booktr import pipeline
from booktr import segments as seg_mod
from booktr import util
from booktr.config import Config

SAMPLE_HTML = """<HTML><HEAD><TITLE>テストページ</TITLE></HEAD>
<BODY>
<p>こんにちは。リブレットは小さい。</p>
<p>今日はいい天気です。</p>
</BODY></HTML>
"""


class _FakeArgs:
    def __init__(self, pages=None, force=False, export=None, dry_run=False):
        self.pages = pages
        self.force = force
        self.export = export
        self.dry_run = dry_run


class _CountingLLM:
    """chat 每次返回一条有效注 JSON，记录调用次数。"""

    def __init__(self):
        self.calls = 0

    def chat(self, system, user, temperature=None, tag="chat", **kwargs):
        self.calls += 1
        return json.dumps({"notes": [
            {"src_quote": "a", "dst_quote": "a", "type": "其他",
             "content": "说明。"}]}, ensure_ascii=False)


class _FailOnceLLM:
    """首次调用抛 LLMError，之后正常；用于验证单页容错。"""

    def __init__(self, fail_tag_substr=""):
        self.calls = 0
        self.fail_tag_substr = fail_tag_substr
        self.failed = False

    def chat(self, system, user, temperature=None, tag="chat", **kwargs):
        self.calls += 1
        if not self.failed and self.fail_tag_substr in tag:
            self.failed = True
            raise llm_mod.LLMError("LLM 返回空内容（finish_reason=None）")
        return json.dumps({"notes": [
            {"src_quote": "a", "dst_quote": "a", "type": "其他",
             "content": "说明。"}]}, ensure_ascii=False)


def _setup(tmp_path, monkeypatch):
    """构造已翻译的两页站点 + done_pages + out 文件。"""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    html = SAMPLE_HTML
    (site / "page1.html").write_bytes(html.encode("utf-8"))
    data = {
        "source_dir": "site",
        "output_dir": "out",
        "work_dir": "work",
        "lang": {"source": "ja", "target": "zh-Hans"},
        "llm": {"provider": "mock"},
        "llm_logs": {"dir": "work/llm_logs", "auto_export": False},
        "translators_notes": {"path": "work/translators_notes.json",
                              "max_notes_per_page": 0},
    }
    cfg = Config(root=tmp_path, data=data, data_dir=str(tmp_path))
    # 段缓存（带译文），供 generate_for_page 读取
    segs = seg_mod.split_segments(html, cfg)
    for s in segs:
        if s.kind == "text":
            s.translation = s.text
    util.write_json(os.path.join(cfg.get("segments_dir", default=""),
                                 "page1.html.json"),
                    {"encoding": "utf-8", "segments": [s.to_dict() for s in segs]})
    # out 文件存在（cmd_annotate 要求）
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "page1.html").write_text(html, encoding="utf-8")
    # state.done_pages
    util.write_json(os.path.join(cfg.work_dir, "state.json"),
                    {"pages": {"page1.html": {"status": "done", "segments": {}}},
                     "done_pages": ["page1.html"]})
    return cfg


def test_cmd_annotate_generates_then_skips(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    fake = _CountingLLM()
    monkeypatch.setattr(pipeline, "_client", lambda c: fake)

    pipeline.cmd_annotate(cfg, _FakeArgs())
    assert fake.calls == 1
    assert len(ann.load(cfg)) == 1

    # 再次运行：已有注 → 跳过，不再调用 LLM
    pipeline.cmd_annotate(cfg, _FakeArgs())
    assert fake.calls == 1  # 未新增调用
    assert len(ann.load(cfg)) == 1  # 未重复追加


def test_cmd_annotate_force_regenerates(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    fake = _CountingLLM()
    monkeypatch.setattr(pipeline, "_client", lambda c: fake)

    pipeline.cmd_annotate(cfg, _FakeArgs())
    pipeline.cmd_annotate(cfg, _FakeArgs(force=True))
    assert fake.calls == 2  # 强制重生成 → 再次调用
    assert len(ann.load(cfg)) == 2  # 追加（--force 语义为重新生成，不去重旧条）


def test_cmd_annotate_continues_on_error(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    fake = _FailOnceLLM(fail_tag_substr="page1.html")
    monkeypatch.setattr(pipeline, "_client", lambda c: fake)

    # 单页失败不抛异常、整批不中止
    pipeline.cmd_annotate(cfg, _FakeArgs())
    assert fake.failed is True
    assert ann.load(cfg) == []  # 该页失败，无注
