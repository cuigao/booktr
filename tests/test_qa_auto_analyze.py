"""qa_auto_probe/analyze.py 的 --prod 路径测试（合成数据，隔离 tmp_path）。"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANALYZE = os.path.join(_HERE, "..", "tools", "qa_auto_probe", "analyze.py")


def _load():
    spec = importlib.util.spec_from_file_location("qa_auto_analyze", _ANALYZE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


@pytest.fixture()
def fake_prod(tmp_path):
    d = tmp_path / "instance"
    work = d / "work"
    _write(str(work / "qa_queue.json"), [
        {"id": "q1", "page": "p/a.html", "segments": [1], "status": "applied",
         "severity": "low", "source": "supervisor", "reason": "措辞生硬",
         "src_quote": "abc", "dst_quote": "生硬译", "suggestion": "改顺"},
        {"id": "q2", "page": "p/a.html", "segments": [2], "status": "rejected",
         "severity": "low", "reason": "误报", "src_quote": "x", "dst_quote": "y",
         "suggestion": "z"},
        {"id": "q3", "page": "p/b.html", "segments": [5], "status": "open",
         "severity": "high", "reason": "专名未译"},
    ])
    _write(str(work / "qa_reports" / "qa_20260101_000000.json"), {
        "version": 2, "ts": "20260101_000000", "scope": "全部已译页",
        "pages": {
            "p/a.html": [
                {"severity": "low", "reason": "措辞生硬", "segments": [1],
                 "dst_quote": "生硬译", "suggestion": "改顺", "src_quote": "abc"},
                {"severity": "low", "reason": "同段另一处问题", "segments": [1],
                 "dst_quote": "其它", "suggestion": "别的", "src_quote": "q"},
            ],
            "p/b.html": [
                {"severity": "high", "reason": "专名未译", "segments": [5],
                 "dst_quote": "原形", "suggestion": "译出", "src_quote": "n"},
            ],
        },
        "checked": {"p/a.html": {}, "p/b.html": {}},
    })
    # segment_history：qa-apply 的 before/after
    _write(str(work / "segment_history" / "p__a.html.json"), {
        "segments": {"1": [
            {"op": "backfill", "cache": {"translation": "因为实在太瞬间了"}},
            {"op": "qa-apply", "cache": {"translation": "因为一切发生得实在太快"}},
        ]},
    })
    _write(str(work / "qa_auto_runs" / "qa_auto_20260101_000000.json"),
           {"ts": "20260101_000000", "scope": "x",
            "total": {"adopt": 3, "reject": 1, "skip": 0, "applied": 2, "pages": 2}})
    _write(str(work / "glossary.json"), [{"src": "HOME", "dst": "首页"}])
    return str(d)


def test_pick_qa2_prefers_v2_most_checked(fake_prod):
    m = _load()
    reps = m._list_qa_reports(fake_prod)
    ts, _, rep = m._pick_qa2(reps)
    assert ts == "20260101_000000" and rep["version"] == 2


def test_prod_analysis_metrics_and_no_side_effects(fake_prod, tmp_path):
    m = _load()
    out = str(tmp_path / "out")
    before = {p: os.path.getmtime(os.path.join(r, f))
              for r, _, fs in os.walk(fake_prod) for f in fs
              for p in [os.path.join(r, f)]}
    txt = m.prod_analysis(fake_prod, None, 0.6, out)
    # 主指标：applied q1 复现（同段+相似），rejected q2 未复现
    assert "applied 1 条 → 复现 1" in txt
    assert "rejected 1 条 → 复现 0" in txt
    # 三分类：q1 复现=1；段1 另一新问题=落在已改段；p/b 未改段=他处
    assert "recur(已裁问题) 1" in txt
    assert "new(落在已改段) 1" in txt
    assert "new(他处) 1" in txt
    # diff 安全：本合成例相似度 ~0.53，非整段重写
    assert "样本 1 段" in txt and "相似度<0.4(疑整段重写) 0" in txt
    # 只读：实例文件未被改动
    after = {p: os.path.getmtime(os.path.join(r, f))
             for r, _, fs in os.walk(fake_prod) for f in fs
             for p in [os.path.join(r, f)]}
    assert before == after
    assert os.path.exists(os.path.join(out, "_out", "prod_analysis.txt"))


def test_classify_reason():
    m = _load()
    assert m.classify_reason("全角标点未保持原样") == "A"
    assert m.classify_reason("专名未译出，仅保留日文原形") == "B"
    assert m.classify_reason("同一词前后不一致") == "C"
    assert m.classify_reason("表达生硬、翻译腔") == "D"
    assert m.classify_reason("漏译，语义偏移") == "E"
    assert m.classify_reason("无关描述") == "?"


def test_emit_category_writes_whitelist(fake_prod, tmp_path):
    m = _load()
    # 追加一条 D 类 open 条目
    qp = os.path.join(fake_prod, "work", "qa_queue.json")
    q = json.load(open(qp, encoding="utf-8"))
    q.append({"id": "d1", "page": "p/a.html", "segments": [1], "status": "open",
              "severity": "low", "reason": "表达生硬、翻译腔",
              "src_quote": "a", "dst_quote": "b", "suggestion": "c"})
    json.dump(q, open(qp, "w", encoding="utf-8"), ensure_ascii=False)
    out = str(tmp_path / "emit")
    ids_path = m.emit_category(fake_prod, "DE", out, None)
    ids = json.load(open(ids_path, encoding="utf-8"))
    # 原 fake 队列中 q3(reason="专名未译"→B) 不算；仅新增 d1 属 D
    assert ids == ["d1"]
    report = open(os.path.join(out, "qa2_DE_report.txt"), encoding="utf-8").read()
    assert "推荐译文: c" in report  # 报告含 QA 推荐译文
    assert "原因: 表达生硬" in report


def test_item_issue_match_requires_same_segment():
    m = _load()
    a = {"segments": [1], "reason": "措辞生硬", "suggestion": "改顺", "dst_quote": "生硬"}
    b = {"segments": [2], "reason": "措辞生硬", "suggestion": "改顺", "dst_quote": "生硬"}
    assert m.item_issue_match(a, b)[0] is False
    b["segments"] = [1]
    assert m.item_issue_match(a, b)[0] is True
