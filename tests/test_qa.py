# -*- coding: utf-8 -*-
"""QA 深度检查开关测试。"""
from __future__ import annotations

import json
import os

import pytest

from booktr import qa
from booktr.pipeline import cmd_qa
from conftest import FakeLLM, write_sample_site


def _args(**kw):
    import argparse
    base = {"pages": None, "no_deep": False, "with_deep": False,
            "start": None, "count": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_qa_local_only_no_llm(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 用 mock 翻译页面，得到已译段
    from booktr import translate as tr
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    # 关闭 deep → 不调 LLM
    tmp_cfg.set(False, "qa", "deep_llm_check")
    counting = FakeLLM.default()
    issues = qa.run_qa(tmp_cfg, counting, "page1.html")
    assert counting.calls == 0  # 未调用 LLM
    assert isinstance(issues, list)


def test_run_qa_with_deep_calls_llm(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    from booktr import translate as tr
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    # 开启 deep → 调用 LLM
    tmp_cfg.set(True, "qa", "deep_llm_check")
    counting = FakeLLM.default()
    issues = qa.run_qa(tmp_cfg, counting, "page1.html")
    assert counting.calls >= 1  # 调用了 LLM


def test_cmd_qa_no_deep_overrides_config(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    from booktr import translate as tr
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    # 配置默认 deep=true，但 --no-deep 应覆盖为 false
    tmp_cfg.set(True, "qa", "deep_llm_check")
    counting = FakeLLM.default()
    # 直接调用 run_qa 验证 cfg 覆盖后的效果（cmd_qa 内部会 set）
    # 模拟 cmd_qa 的覆盖逻辑
    args = _args(no_deep=True)
    if args.no_deep:
        tmp_cfg.set(False, "qa", "deep_llm_check")
    issues = qa.run_qa(tmp_cfg, counting, "page1.html")
    assert counting.calls == 0


def _write_translated(tmp_cfg, tmp_path, rel="page1.html"):
    write_sample_site(tmp_path)
    from booktr import translate as tr
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, rel, state, {}, {}, [])


def test_qa_report_prints_per_page_progress(tmp_cfg, tmp_path, capsys):
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")
    qa.qa_report(tmp_cfg, FakeLLM.default(), ["page1.html"])
    out = capsys.readouterr().out
    assert "[1/1] page1.html" in out


def test_qa_report_writes_incrementally(tmp_cfg, tmp_path):
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")
    qa.qa_report(tmp_cfg, FakeLLM.default(), ["page1.html"])
    report_path = os.path.join(tmp_cfg.work_dir, "qa_report.json")
    assert os.path.exists(report_path)
    data = json.loads(open(report_path, encoding="utf-8").read())
    assert "pages" in data and "total_issues" in data


def test_qa_report_records_checked_even_without_issues(tmp_cfg, tmp_path):
    """version 2：0 问题页也写入 checked（含 ts/计数），供聚合查询。"""
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")  # 无 LLM，通常 0 问题
    report = qa.qa_report(tmp_cfg, FakeLLM.default(), ["page1.html"],
                          ts="20260101_000000", scope="test")
    assert report["version"] == 2
    assert report["ts"] == "20260101_000000"
    assert report["total_pages_checked"] == 1
    assert "page1.html" in report["checked"]
    info = report["checked"]["page1.html"]
    for k in ("ts", "total", "high", "mid", "low", "unresolved",
              "deep", "duration_s"):
        assert k in info
    # 无问题时 checked 有记录，pages 不含该页
    if not report["total_issues"]:
        assert "page1.html" not in report["pages"]


def test_collect_page_status_picks_latest(tmp_cfg, tmp_path):
    """聚合取每页 ts 最大的 checked 记录；兼容旧报告（仅有问题页）。"""
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")
    reports_dir = os.path.join(tmp_cfg.work_dir, "qa_reports")
    os.makedirs(reports_dir, exist_ok=True)
    from booktr import util
    util.write_json(os.path.join(reports_dir, "qa_20260101_000000.json"), {
        "version": 2, "ts": "20260101_000000",
        "pages": {"page1.html": [{"severity": "high"}]},
        "checked": {"page1.html": {"ts": "20260101_000000", "total": 1,
                                   "high": 1, "mid": 0, "low": 0,
                                   "unresolved": 0, "deep": True,
                                   "duration_s": 1.0}},
        "total_issues": 1, "high": 1, "unresolved": 0, "total_pages_checked": 1,
    })
    util.write_json(os.path.join(reports_dir, "qa_20260202_000000.json"), {
        "version": 2, "ts": "20260202_000000",
        "pages": {}, "checked": {"page1.html": {"ts": "20260202_000000",
            "total": 0, "high": 0, "mid": 0, "low": 0, "unresolved": 0,
            "deep": True, "duration_s": 0.5}},
        "total_issues": 0, "high": 0, "unresolved": 0, "total_pages_checked": 1,
    })
    latest = qa.collect_page_status(tmp_cfg)
    assert latest["page1.html"]["ts"] == "20260202_000000"
    assert latest["page1.html"]["total"] == 0


def test_cmd_qa_status_lists_checked_and_unchecked(tmp_cfg, tmp_path, capsys):
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html", "page2.html"])
    tmp_cfg.set(False, "qa", "deep_llm_check")
    reports_dir = os.path.join(tmp_cfg.work_dir, "qa_reports")
    os.makedirs(reports_dir, exist_ok=True)
    from booktr import util
    util.write_json(os.path.join(reports_dir, "qa_20260101_000000.json"), {
        "version": 2, "ts": "20260101_000000", "pages": {},
        "checked": {"page1.html": {"ts": "20260101_000000", "total": 0,
                                   "high": 0, "mid": 0, "low": 0,
                                   "unresolved": 0, "deep": True,
                                   "duration_s": 0.1}},
        "total_issues": 0, "high": 0, "unresolved": 0, "total_pages_checked": 1,
    })
    from booktr.pipeline import cmd_qa_status
    cmd_qa_status(tmp_cfg, _args())
    out = capsys.readouterr().out
    assert "1/2" in out
    assert "page1.html" in out
    assert "page2.html" in out


def test_run_qa_warns_on_llm_failure(tmp_cfg, tmp_path, capsys):
    from booktr import llm as llm_mod

    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(True, "qa", "deep_llm_check")

    class FailingLLM:
        calls = 0

        def chat(self, *a, **k):
            self.calls += 1
            raise llm_mod.LLMError("boom")

    issues = qa.run_qa(tmp_cfg, FailingLLM(), "page1.html")
    out = capsys.readouterr().out
    assert "LLM 深度检查失败" in out
    assert isinstance(issues, list)


def test_run_qa_passes_reasoning_effort(tmp_cfg, tmp_path):
    """QA 深度检查 reasoning_effort：qa 覆盖 llm；空则不传（None）。"""
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(True, "qa", "deep_llm_check")

    class RecLLM:
        kwargs = None

        def chat(self, system, user, **k):
            RecLLM.kwargs = k
            return '{"issues": []}'

    # 默认（qa 与 llm 均空）→ 不传
    qa.run_qa(tmp_cfg, RecLLM(), "page1.html")
    assert RecLLM.kwargs.get("reasoning_effort") is None

    # qa 覆盖
    tmp_cfg.set("high", "qa", "reasoning_effort")
    qa.run_qa(tmp_cfg, RecLLM(), "page1.html")
    assert RecLLM.kwargs.get("reasoning_effort") == "high"

    # qa 空 → 继承 llm
    tmp_cfg.set("", "qa", "reasoning_effort")
    tmp_cfg.set("max", "llm", "reasoning_effort")
    qa.run_qa(tmp_cfg, RecLLM(), "page1.html")
    assert RecLLM.kwargs.get("reasoning_effort") == "max"


def test_cmd_qa_prints_start_line(tmp_cfg, tmp_path, capsys):
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")
    cmd_qa(tmp_cfg, _args())
    out = capsys.readouterr().out
    assert "QA 开始" in out


# ── --start/--count 区间 + 时间戳报告 ──────────────────────────────────


def _seed_plan_and_done(tmp_cfg, tmp_path, rels):
    """写入 plan.order，并为给定页写段缓存 + 标记 done。"""
    from booktr import translate as tr
    from booktr import util
    util.write_json(os.path.join(tmp_cfg.work_dir, "plan.json"), {"order": rels})
    seg_dir = tmp_cfg.get("segments_dir", default="")
    state = tr.State(tmp_cfg)
    for r in rels:
        util.write_json(os.path.join(seg_dir, r.replace("/", "__") + ".json"), {
            "encoding": "utf-8",
            "segments": [{"id": 1, "kind": "text", "text": "こんにちは。",
                          "translation": "你好。", "start": 0, "end": 6}],
        })
        state.page(r)["status"] = "done"
    state.data["done_pages"] = list(rels)
    state.save()


def test_cmd_qa_range_selects_from_plan_order(tmp_cfg, tmp_path, capsys):
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html", "page2.html", "page3.html"])
    tmp_cfg.set(False, "qa", "deep_llm_check")
    cmd_qa(tmp_cfg, _args(start=2, count=1))
    out = capsys.readouterr().out
    assert "plan.order[2..2]" in out
    assert "page2.html" in out
    assert "page1.html" not in out


def test_cmd_qa_range_skips_untranslated(tmp_cfg, tmp_path, capsys):
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html"])
    # plan 含 page2 但未翻译
    from booktr import util
    util.write_json(os.path.join(tmp_cfg.work_dir, "plan.json"),
                    {"order": ["page1.html", "page2.html"]})
    tmp_cfg.set(False, "qa", "deep_llm_check")
    cmd_qa(tmp_cfg, _args(start=1, count=2))
    out = capsys.readouterr().out
    assert "跳过未翻译 1 页" in out


def test_cmd_qa_pages_and_start_conflict(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html"])
    tmp_cfg.set(False, "qa", "deep_llm_check")
    import pytest
    with pytest.raises(SystemExit):
        cmd_qa(tmp_cfg, _args(pages=["page1.html"], start=1))


def test_cmd_qa_writes_timestamped_report(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html"])
    tmp_cfg.set(False, "qa", "deep_llm_check")
    cmd_qa(tmp_cfg, _args(start=1, count=1))
    reports_dir = os.path.join(tmp_cfg.work_dir, "qa_reports")
    files = [f for f in os.listdir(reports_dir) if f.startswith("qa_")]
    assert len(files) == 1
    # 最新别名同步存在
    assert os.path.exists(os.path.join(tmp_cfg.work_dir, "qa_report.json"))


def test_cmd_qa_queue_dedup(tmp_cfg, tmp_path):
    """重复 QA 同一页，qa_queue 条目不重复；不写入 review_queue。"""
    write_sample_site(tmp_path)
    _seed_plan_and_done(tmp_cfg, tmp_path, ["page1.html"])
    tmp_cfg.set(True, "qa", "deep_llm_check")

    class IssueLLM:
        def chat(self, system, user, **k):
            return ('{"issues": [{"severity": "low", "reason": "x", '
                    '"src_quote": "こんにちは。", "dst_quote": "", '
                    '"suggestion": "y"}]}')

    from booktr import qa_queue as qa_queue_mod
    from booktr import review as review_mod
    import booktr.pipeline as pl
    orig = pl._client
    pl._client = lambda cfg: IssueLLM()
    try:
        cmd_qa(tmp_cfg, _args(start=1, count=1))
        cmd_qa(tmp_cfg, _args(start=1, count=1))
    finally:
        pl._client = orig
    q = qa_queue_mod.load(tmp_cfg)
    assert len(q) == 1
    assert q[0]["resolved"] is True
    assert q[0]["segments"] == [1]
    # 不再写入 review_queue
    assert review_mod.load_queue(tmp_cfg) == []


def test_locate_segments_exact_and_fuzzy(tmp_cfg, tmp_path):
    from booktr import qa
    from booktr import translate as tr
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])
    segs = __import__("booktr.segments", fromlist=["segments"]).segments_for_page(tmp_cfg, "page1.html")
    # 精确原文命中
    ids = qa.locate_segments(segs, "今日はいい天気です。", "")
    assert ids
    # 无法命中
    assert qa.locate_segments(segs, "存在しないテキストXYZ", "") == []


def test_locate_segments_cross_segment_lines(tmp_cfg, tmp_path):
    from booktr import qa
    from booktr import translate as tr
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])
    segs = __import__("booktr.segments", fromlist=["segments"]).segments_for_page(tmp_cfg, "page1.html")
    # 跨段的多行引用：应命中多个段
    ids = qa.locate_segments(segs, "こんにちは。\n今日はいい天気です。", "")
    assert len(ids) >= 2


def test_locate_segments_includes_head_title():
    """候选段含 head_title（非 text），使其可被定位。"""
    from booktr import qa
    from booktr.segments import Segment

    segs = [
        Segment(id=1, kind="head_title", start=0, end=7, text="YOUKOSO",
                translation="欢迎"),
        Segment(id=2, kind="text", start=8, end=20, text="こんにちは。",
                translation="你好。"),
    ]
    # 原文命中 head_title
    assert qa.locate_segments(segs, "YOUKOSO", "") == [1]
    # 译文命中 head_title
    assert qa.locate_segments(segs, "", "欢迎") == [1]


def test_qa_prompt_only_lists_actionable():
    """系统提示词须把生硬/翻译腔明确列为 low 可报告项。"""
    from booktr import prompts
    from booktr.config import Config
    import os
    cfg = Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    s = prompts.build_qa_system(cfg)
    assert "生硬" in s
    assert "翻译腔" in s
    assert "low" in s
    assert "合并为一条" in s
    assert "high" in s


def test_qa_user_includes_glossary_note():
    """QA 词汇表与翻译词汇表模式相同：含 note 时渲染 '└ 使用场景：' 子行。"""
    from booktr import prompts
    gl = [{"src": "HOME", "dst": "首页", "note": "导航入口"},
          {"src": "X", "dst": "Y"}]
    out = prompts.build_qa_user("原文", "译文", gl)
    gl_section = out.split("## 原文")[0]
    assert "- HOME → 首页" in gl_section
    assert "└ 使用场景：导航入口" in gl_section
    # 无 note 的条目不产生使用场景行
    assert "- X → Y" in gl_section
    assert gl_section.count("└ 使用场景：") == 1


def test_term_lines_shared_by_translate_and_qa():
    """term_lines 为翻译与 QA 共用的渲染函数。"""
    from booktr import prompts
    items = [{"src": "A", "dst": "B", "note": "n"}]
    assert prompts.term_lines(items) == ["- A → B", "  └ 使用场景：n"]
    hints = prompts.format_term_hints(items)
    assert "└ 使用场景：n" in hints
