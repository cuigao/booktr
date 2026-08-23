# -*- coding: utf-8 -*-
"""段切分重构测试：Q/P 标签块模型、空白保留、<pre> 换行升格、&nbsp; 规则。"""
from __future__ import annotations

import os
import re

import pytest

from booktr import segments as seg
from conftest import tmp_cfg  # noqa: F401


def _segs(cfg, html):
    return [s for s in seg.split_segments(html, cfg) if s.kind == "text"]


def test_seg_strips_leading_trailing_whitespace(tmp_cfg):
    """段文本首尾无空白；段首缩进由 start/end 收缩保留在骨架。"""
    html = "<p>　　　　本文</p>"
    segs = _segs(tmp_cfg, html)
    assert segs, "应有段"
    assert segs[0].text == "本文"
    assert not segs[0].text.startswith("　")
    assert not segs[0].text.endswith("　")


def test_reassemble_preserves_leading_fullwidth(tmp_cfg):
    """reassemble 保留段首全角缩进（start/end 收缩，空白留骨架）。"""
    html = "<p>　　　　寒い毎日、あなたはよく眠れていますか。</p>"
    segs = _segs(tmp_cfg, html)
    segs[0].translation = "寒冷的每一天，你睡得好吗？"
    out = seg.reassemble(html, segs)
    assert "　　　　寒冷的每一天" in out
    assert out == "<p>　　　　寒冷的每一天，你睡得好吗？</p>"


def test_br_absorbs_adjacent_ws_into_placeholder(tmp_cfg):
    """<br> 及两侧空白并入占位符 raw。"""
    html = "<p>a<br>\n　　　　b</p>"
    segs = _segs(tmp_cfg, html)
    assert len(segs) == 1
    assert segs[0].text == "a[[P0]]b"
    assert segs[0].placeholders["[[P0]]"] == "<br>\n　　　　"


def test_inline_open_close_absorb_ws(tmp_cfg):
    """inline 开/闭标签及邻接空白并入占位符。"""
    html = "<p>　<a href=\"#\">　連結　</a>　本文</p>"
    segs = _segs(tmp_cfg, html)
    assert segs and segs[0].text.endswith("本文")
    # 占位符应含两侧空白
    ph = next(v for v in segs[0].placeholders.values())
    assert ph.startswith("　")
    assert ph.endswith("　")


def test_nbsp_adjacent_to_tag_absorbed(tmp_cfg):
    """紧邻标签的 &nbsp; 被吸收进占位符。"""
    html = "<p>a&nbsp;&nbsp;<br>&nbsp;b</p>"
    segs = _segs(tmp_cfg, html)
    assert segs[0].placeholders["[[P0]]"] == "&nbsp;&nbsp;<br>&nbsp;"
    assert "&nbsp;" not in segs[0].text  # 全部被吸收


def test_nbsp_between_text_preserved(tmp_cfg):
    """纯文本间的 &nbsp; 保留给 LLM（不吸收）。"""
    html = "<p>a&nbsp;b</p>"
    segs = _segs(tmp_cfg, html)
    assert segs[0].text == "a&nbsp;b"
    assert segs[0].placeholders == {}


def test_pre_single_newline_becomes_p(tmp_cfg):
    """<pre> 内单一 \\n → P 占位符。"""
    html = "<pre>text1\ntext2</pre>"
    segs = _segs(tmp_cfg, html)
    assert len(segs) == 1
    assert segs[0].text == "text1[[P0]]text2"
    assert segs[0].placeholders["[[P0]]"] == "\n"


def test_pre_consecutive_newlines_become_q(tmp_cfg):
    """<pre> 内连续换行（中间可有空白）→ Q 段边界，拆段。"""
    html = "<pre>a\n\n\nb</pre>"
    segs = _segs(tmp_cfg, html)
    assert len(segs) == 2
    assert segs[0].text == "a"
    assert segs[1].text == "b"
    # 连续换行留骨架
    for s in segs:
        s.translation = s.text
    assert seg.reassemble(html, segs) == html


def test_pre_newline_with_spaces_still_consecutive(tmp_cfg):
    """<pre> 内多个换行即使中间有空白也视为连续换行 → Q。"""
    html = "<pre>a\n \n \nb</pre>"
    segs = _segs(tmp_cfg, html)
    assert len(segs) == 2
    assert segs[0].text == "a"
    assert segs[1].text == "b"


def test_roundtrip_identity_byte_exact(tmp_cfg):
    """恒等译文 round-trip 字节级一致。"""
    html = "<p>　　　　周りは風邪をひいている人でいっぱいです。<br>\n　　　　私は普段、丈夫です。</p>"
    segs = _segs(tmp_cfg, html)
    for s in segs:
        s.translation = s.text
    assert seg.reassemble(html, segs) == html


def test_reassemble_trims_llm_ws(tmp_cfg):
    """收译文去除首尾空白后再替换（模拟 LLM 丢弃缩进）。"""
    html = "<p>　　　　本文です。</p>"
    segs = _segs(tmp_cfg, html)
    segs[0].translation = "  这是正文。  "
    out = seg.reassemble(html, segs)
    assert out == "<p>　　　　这是正文。</p>"


def test_block_tags_are_q_not_in_seg(tmp_cfg):
    """block 标签（如 <p>/<td>）为段边界，标签本身不进段文本。"""
    html = "<p>一</p><p>二</p>"
    segs = _segs(tmp_cfg, html)
    assert [s.text for s in segs] == ["一", "二"]
    for s in segs:
        assert "p" not in s.text.lower()
