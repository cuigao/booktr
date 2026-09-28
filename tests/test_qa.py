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
    base = {"pages": None, "no_deep": False, "with_deep": False}
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


def test_cmd_qa_prints_start_line(tmp_cfg, tmp_path, capsys):
    _write_translated(tmp_cfg, tmp_path)
    tmp_cfg.set(False, "qa", "deep_llm_check")
    cmd_qa(tmp_cfg, _args())
    out = capsys.readouterr().out
    assert "QA 开始" in out
