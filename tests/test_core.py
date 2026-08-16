"""核心机制测试：编码探测、段切分、拼接、占位符、规划。"""
import os
import sys
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from booktr import segments, util
from booktr.config import Config


@pytest.fixture
def cfg():
    return Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), data={})


SAMPLE = """<HTML><HEAD><TITLE>岡崎Today</TITLE></HEAD>
<BODY BGCOLOR="FFFFFF">
<CENTER><img src="icon/today.gif"><br></CENTER>
<blockquote>
<font color="#000080">こんにちは。<br>今日はいい天気です。</font>
</blockquote>
<CENTER><A HREF="../index.html">HOME</A></CENTER>
</BODY></HTML>
"""


def test_split_basic(cfg):
    segs = segments.split_segments(SAMPLE, cfg)
    kinds = [s.kind for s in segs]
    assert "head_title" in kinds
    texts = [s.text for s in segs]
    assert any("こんにちは。" in t for t in texts)
    assert any("今日はいい天気です。" in t for t in texts)


def test_split_offsets_valid(cfg):
    segs = segments.split_segments(SAMPLE, cfg)
    for s in segs:
        assert 0 <= s.start < s.end <= len(SAMPLE)
    # 至少一个 text 段与其偏移处的原文一致（占位符还原后）
    matched = False
    for s in segs:
        if s.kind == "text":
            raw = SAMPLE[s.start : s.end]
            restored = segments._restore_placeholders(s.text, s.placeholders)
            if raw == restored:
                matched = True
                break
    assert matched


def test_placeholder_preserved(cfg):
    segs = segments.split_segments(SAMPLE, cfg)
    for s in segs:
        if s.kind == "text":
            for tok in s.placeholders:
                assert tok in s.text


def test_reassemble_roundtrip(cfg):
    segs = segments.split_segments(SAMPLE, cfg)
    for s in segs:
        s.translation = "【" + s.text + "】"
    out = segments.reassemble(SAMPLE, segs)
    tags1 = re.findall(r"<[^>]+>", SAMPLE)
    tags2 = [t for t in re.findall(r"<[^>]+>", out) if t.lower() != '<meta charset="utf-8">']
    assert tags1 == tags2


def test_charset_injected(cfg):
    out = segments._ensure_charset(SAMPLE)
    assert '<meta charset="utf-8">' in out


def test_no_cjk_segment_skipped(cfg):
    html = "<HTML><HEAD><TITLE>x</TITLE></HEAD><BODY>\n\n  \n</BODY></HTML>"
    segs = segments.split_segments(html, cfg)
    assert len(segs) == 0 or not any("text" == s.kind for s in segs)


def test_decode_html_cp932(tmp_path):
    p = tmp_path / "a.html"
    p.write_bytes("<TITLE>岡崎律子</TITLE>".encode("cp932"))
    raw = p.read_bytes()
    text, enc = util.decode_html(raw)
    assert enc == "cp932"
    assert "岡崎律子" in text


def test_decode_html_utf8(tmp_path):
    p = tmp_path / "b.html"
    p.write_bytes("<meta charset=\"utf-8\"><TITLE>岡崎律子</TITLE>".encode("utf-8"))
    raw = p.read_bytes()
    text, enc = util.decode_html(raw)
    assert enc == "utf-8"
    assert "岡崎律子" in text


def test_ngram_dice():
    assert util.dice_coefficient("こんにちは世界", "こんにちは世界") > 0.99
    assert util.dice_coefficient("hello world", "goodbye world") < 1.0
    assert util.dice_coefficient("", "") == 0.0


def test_json_parse_fenced():
    from booktr import llm
    resp = '```json\n{"translation": "你好", "confidence": 0.9}\n```'
    data = llm.parse_json_response(resp)
    assert data["translation"] == "你好"
