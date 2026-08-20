# -*- coding: utf-8 -*-
"""规划排序测试：归一化/方向纯函数 + build_plan 确定性排序。"""
from __future__ import annotations

import json
import os

import pytest

from booktr import planner
from booktr import util


# ── 纯函数 ──────────────────────────────────────────────────────────────


def test_norm_minmax_normal():
    assert planner._norm_minmax({"a": 1, "b": 3, "c": 5}) == {"a": 0.0, "b": 0.5, "c": 1.0}


def test_norm_minmax_reverse():
    assert planner._norm_minmax({"a": 1, "b": 5}, positive=False) == {"a": 1.0, "b": 0.0}


def test_norm_minmax_flat():
    # hi==lo → 全 0.5
    assert planner._norm_minmax({"a": 7, "b": 7}) == {"a": 0.5, "b": 0.5}


def test_apply_direction_forward_reverse():
    scores = {"a": 0.2, "b": 0.8}
    assert planner._apply_direction(scores, "forward") == scores
    assert planner._apply_direction(scores, "reverse") == pytest.approx({"a": 0.8, "b": 0.2})


def test_date_to_float():
    assert planner._date_to_float("2000-01-01") == 2000.0
    assert planner._date_to_float("2000-01-01") < planner._date_to_float("2000-06-15")
    assert planner._date_to_float("bad") == 0.0


# ── build_plan 排序 ────────────────────────────────────────────────────


def _make_site_map(tmp_cfg, pages):
    sm = {"pages": pages}
    util.write_json(os.path.join(tmp_cfg.work_dir, "site_map.json"), sm)


def test_build_plan_len_sort(tmp_cfg):
    # 用 len 权重：text_len 越大排越前
    pages = {
        "a.html": {"title": "A", "links": [], "text_len": 100, "in_degree": 0, "depth": 0},
        "b.html": {"title": "B", "links": [], "text_len": 300, "in_degree": 0, "depth": 0},
        "c.html": {"title": "C", "links": [], "text_len": 200, "in_degree": 0, "depth": 0},
    }
    _make_site_map(tmp_cfg, pages)

    plan = planner.build_plan(tmp_cfg, weights={"len": 1.0}, reset_order=True)
    assert plan["order"] == ["b.html", "c.html", "a.html"]


def test_build_plan_hotness(tmp_cfg):
    # hotness 权重：in_degree 越大越前
    pages = {
        "a.html": {"title": "A", "links": [], "in_degree": 1, "depth": 0},
        "b.html": {"title": "B", "links": [], "in_degree": 5, "depth": 0},
    }
    _make_site_map(tmp_cfg, pages)
    plan = planner.build_plan(tmp_cfg, weights={"hotness": 1.0}, reset_order=True)
    assert plan["order"] == ["b.html", "a.html"]


def test_build_plan_reverse_direction(tmp_cfg):
    # len 权重 + reverse：text_len 越小越前
    pages = {
        "a.html": {"title": "A", "links": [], "text_len": 100, "in_degree": 0, "depth": 0},
        "b.html": {"title": "B", "links": [], "text_len": 300, "in_degree": 0, "depth": 0},
    }
    _make_site_map(tmp_cfg, pages)
    plan = planner.build_plan(
        tmp_cfg, weights={"len": 1.0}, directions={"len": "reverse"}, reset_order=True
    )
    assert plan["order"] == ["a.html", "b.html"]
