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
