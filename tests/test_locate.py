# -*- coding: utf-8 -*-
"""段落定位测试：locate.locate 与 qa.locate_segments 委托一致性。"""
from __future__ import annotations

from booktr import locate as locate_mod
from booktr import qa
from booktr.segments import Segment


def _seg(i, text, translation=None, kind="text"):
    return Segment(id=i, kind=kind, start=0, end=len(text), text=text,
                   translation=translation)


def test_locate_exact_src():
    segs = [_seg(1, "こんにちは", "你好"), _seg(2, "今日はいい天気", "今天天气好")]
    res = locate_mod.locate(segs, src_frag="今日はいい天気")
    assert [r["sid"] for r in res] == [2]
    assert res[0]["method"] == "exact"


def test_locate_exact_dst():
    segs = [_seg(1, "こんにちは", "你好"), _seg(2, "今日はいい天気", "今天天气好")]
    res = locate_mod.locate(segs, dst_frag="你好")
    assert [r["sid"] for r in res] == [1]


def test_locate_partial_substring():
    segs = [_seg(1, "私は毎日散歩します", "我每天散步")]
    res = locate_mod.locate(segs, src_frag="散歩")
    assert [r["sid"] for r in res] == [1]


def test_locate_strip_placeholder():
    segs = [_seg(1, "[[P0]]こんにちは[[P1]]", "[[P0]]你好[[P1]]")]
    res = locate_mod.locate(segs, src_frag="こんにちは")
    assert [r["sid"] for r in res] == [1]
    assert res[0]["method"] == "exact"


def test_locate_multiline():
    segs = [_seg(1, "一行目", "第一行"), _seg(2, "二行目", "第二行")]
    res = locate_mod.locate(segs, src_frag="一行目\n二行目")
    assert {r["sid"] for r in res} == {1, 2}
    assert res[0]["method"] == "line_split"


def test_locate_fuzzy():
    segs = [_seg(1, "今日はとてもいい天気ですね", "今天天气真的非常好")]
    res = locate_mod.locate(segs, src_frag="今日はとても良い天気でしたね")
    assert res and res[0]["sid"] == 1
    assert res[0]["method"] == "fuzzy"
    assert res[0]["score"] >= 0.6


def test_locate_skips_untranslated_by_default():
    segs = [_seg(1, "こんにちは", None)]
    assert locate_mod.locate(segs, src_frag="こんにちは") == []


def test_locate_include_untranslated():
    segs = [_seg(1, "こんにちは", None)]
    res = locate_mod.locate(segs, src_frag="こんにちは", include_untranslated=True)
    assert [r["sid"] for r in res] == [1]


def test_locate_empty_quote():
    segs = [_seg(1, "こんにちは", "你好")]
    assert locate_mod.locate(segs, src_frag="", dst_frag="") == []


def test_qa_locate_segments_delegates_same():
    segs = [_seg(1, "こんにちは", "你好"), _seg(2, "今日はいい天気", "今天天气好")]
    assert qa.locate_segments(segs, "今日はいい天気", "") == [2]
    assert qa.locate_segments(segs, "", "你好") == [1]


def test_qa_locate_segments_fuzzy_top1_only():
    segs = [_seg(1, "今日はとてもいい天気ですね", "今天天气非常好"),
            _seg(2, "今日もいい天気ですね", "今天天气也好")]
    # 模糊匹配应只返回最优一个
    res = qa.locate_segments(segs, "今日はとても良い天気でしたね", "")
    assert len(res) == 1
