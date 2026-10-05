"""译者注生成：提示词契约、锚点定位、数量上限与字段规范。"""
from __future__ import annotations

import json
import os

from conftest import FakeLLM

from booktr import annotator as ann
from booktr import prompts
from booktr import segments as seg_mod
from booktr import util
from booktr.config import Config


SAMPLE_HTML = """<HTML><HEAD><TITLE>テストページ</TITLE></HEAD>
<BODY>
<p>こんにちは。リブレットは小さい。</p>
<p>今日はいい天気です。</p>
</BODY></HTML>
"""


def _write_site(tmp_path) -> Config:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "page1.html").write_bytes(SAMPLE_HTML.encode("utf-8"))
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
    return cfg


def _note(src, dst, **kw):
    d = {"src_quote": src, "dst_quote": dst, "type": "历史考据",
         "content": "背景说明。", "related_pages": []}
    d.update(kw)
    return d


def test_system_requires_quote_fields():
    cfg = Config(root=".", data={}, data_dir=".")
    sysp = prompts.build_translator_note_system(cfg)
    assert "src_quote" in sysp and "dst_quote" in sysp
    assert "逐字" in sysp


def test_user_prompt_includes_segments_and_summaries():
    cfg = Config(root=".", data={}, data_dir=".")
    segs = [{"id": 1, "src": "リブレットは小さい。", "dst": "Libretto很小。"}]
    usr = prompts.build_translator_note_user("a.html", segs, {"b.html": "摘要"})
    assert "原文分段" in usr and "译文分段" in usr
    assert "Libretto很小。" in usr
    assert "b.html" in usr


def test_generate_stores_anchor_and_locates(tmp_path):
    cfg = _write_site(tmp_path)
    # 先写入段缓存（含译文），供 generate 读取
    html = (tmp_path / "site" / "page1.html").read_text(encoding="utf-8")
    segs = seg_mod.split_segments(html, cfg)
    for s in segs:
        if s.kind == "text":
            s.translation = s.text
    seg_path = os.path.join(cfg.get("segments_dir", default=""),
                            "page1.html.json")
    util.write_json(seg_path, {"encoding": "utf-8",
                               "segments": [s.to_dict() for s in segs]})

    note_sid = next(s.id for s in segs if s.kind == "text")
    resp = json.dumps({"notes": [_note("リブレット", "リブレット")]},
                      ensure_ascii=False)
    fake = FakeLLM(sequence=[resp])

    n = ann.generate_for_page(cfg, fake, "page1.html")
    assert n == 1
    items = ann.load(cfg)
    e = items[0]
    assert e["page"] == "page1.html"
    assert e["src_quote"] == "リブレット"
    assert e["dst_quote"] == "リブレット"
    assert e["segment_id"] == note_sid
    assert e["created_by"] == "llm"
    assert e["content"].startswith("背景")
    assert "anchor" not in e  # 新 schema 不再写旧 anchor


def test_empty_content_skipped(tmp_path):
    cfg = _write_site(tmp_path)
    html = (tmp_path / "site" / "page1.html").read_text(encoding="utf-8")
    segs = seg_mod.split_segments(html, cfg)
    for s in segs:
        if s.kind == "text":
            s.translation = s.text
    util.write_json(os.path.join(cfg.get("segments_dir", default=""),
                                 "page1.html.json"),
                    {"encoding": "utf-8",
                     "segments": [s.to_dict() for s in segs]})
    resp = json.dumps({"notes": [
        {"src_quote": "x", "dst_quote": "x", "content": "  "}]},
        ensure_ascii=False)
    assert ann.generate_for_page(cfg, FakeLLM(sequence=[resp]), "page1.html") == 0


def test_max_notes_cap(tmp_path):
    cfg = _write_site(tmp_path)
    html = (tmp_path / "site" / "page1.html").read_text(encoding="utf-8")
    segs = seg_mod.split_segments(html, cfg)
    for s in segs:
        if s.kind == "text":
            s.translation = s.text
    util.write_json(os.path.join(cfg.get("segments_dir", default=""),
                                 "page1.html.json"),
                    {"encoding": "utf-8",
                     "segments": [s.to_dict() for s in segs]})
    cfg.set(2, "translators_notes", "max_notes_per_page")
    resp = json.dumps({"notes": [_note("a", "a"), _note("b", "b"),
                                 _note("c", "c")]}, ensure_ascii=False)
    ann.generate_for_page(cfg, FakeLLM(sequence=[resp]), "page1.html")
    assert len(ann.load(cfg)) == 2


def test_related_pages_filtered(tmp_path):
    cfg = _write_site(tmp_path)
    (tmp_path / "site" / "real.html").write_bytes(b"<html></html>")
    html = (tmp_path / "site" / "page1.html").read_text(encoding="utf-8")
    segs = seg_mod.split_segments(html, cfg)
    for s in segs:
        if s.kind == "text":
            s.translation = s.text
    util.write_json(os.path.join(cfg.get("segments_dir", default=""),
                                 "page1.html.json"),
                    {"encoding": "utf-8",
                     "segments": [s.to_dict() for s in segs]})
    resp = json.dumps({"notes": [_note("a", "a", related_pages=[
        "real.html", "nope.html", ""])]}, ensure_ascii=False)
    ann.generate_for_page(cfg, FakeLLM(sequence=[resp]), "page1.html")
    assert ann.load(cfg)[0]["related_pages"] == ["real.html"]
