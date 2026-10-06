# -*- coding: utf-8 -*-
"""qa_slide_probe 纯函数 + 分析流程测试（不调用 LLM）。"""
from __future__ import annotations

import importlib.util
import os

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "tools", "qa_slide_probe")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOL, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _S:
    def __init__(self, i, t, tr="x"):
        self.id, self.text, self.translation = i, t, tr


def test_build_windows_budget_and_overlap():
    P = _load("probe")
    segs = [_S(i, "x" * 100) for i in range(1, 11)]
    wins = P.build_windows(segs, 250, 2)
    # 每窗含 2 段（200 字符，加第 3 段超 250），相邻窗重叠 2 段 -> 前进 0?
    # 前进 = max(i+1, j-overlap)；首窗 i=0,j=2 -> 下 i=max(1,0)=1
    assert [s.id for s in wins[0]] == [1, 2]
    assert [s.id for s in wins[1]] == [2, 3]
    assert [s.id for s in wins[-1]] == [9, 10]
    # 覆盖完整且无空洞
    ids = set()
    for w in wins:
        ids |= {s.id for s in w}
    assert ids == set(range(1, 11))


def test_build_windows_single_huge_segment():
    P = _load("probe")
    assert [[s.id for s in w] for w in P.build_windows([_S(1, "y" * 5000)], 1500, 2)] == [[1]]


def test_build_windows_empty_and_single():
    P = _load("probe")
    assert P.build_windows([], 1500, 2) == []
    assert [[s.id for s in w] for w in P.build_windows([_S(1, "abc")], 1500, 2)] == [[1]]


def test_dedup_issues_by_segments_and_reason():
    P = _load("probe")
    iss = [
        {"segments": [1, 2], "reason": "a b"},
        {"segments": [2, 1], "reason": "a  b"},   # 同段集合同归一 reason
        {"segments": [3], "reason": "c"},
    ]
    assert len(P.dedup_issues(iss)) == 2


def test_analyze_coverage_and_window_only():
    A = _load("analyze")
    # 构造两个条件：整页漏掉 seg5 的问题，滑窗命中
    whole = {"calls": 1, "llm_issues": [
        {"segments": [1], "reason": "术语错", "suggestion": "", "dst_quote": "甲"}],
        "duration_s": 10, "prompt_tokens": 100, "completion_tokens": 50,
        "reasoning_chars": 200, "loops": 0}
    win = {"calls": 3, "llm_issues": [
        {"segments": [1], "reason": "术语错", "suggestion": "", "dst_quote": "甲"},
        {"segments": [5], "reason": "漏译", "suggestion": "", "dst_quote": "乙"}],
        "duration_s": 30, "prompt_tokens": 300, "completion_tokens": 120,
        "reasoning_chars": 500, "loops": 0}
    result = {
        "meta": {"conditions": ["whole", "win1500"], "model": "m"},
        "pages": {"p.html": {"conditions": {
            "whole": {"runs": [{"run": 1, **whole}]},
            "win1500": {"runs": [{"run": 1, **win}]},
        }}}}
    an = A.analyze(result)
    assert an["total_clusters"] == 2
    assert an["summary"]["whole"]["cluster_coverage"] == 0.5
    assert an["summary"]["win1500"]["cluster_coverage"] == 1.0
    assert an["summary"]["win1500"]["new_vs_whole"] == 1
    assert an["pages"]["p.html"]["win_new_vs_whole"] == 1
    # fmt 不抛
    assert "win1500" in A.fmt(result, an)
