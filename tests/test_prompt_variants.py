"""提示词变体实验相关：风格预设、QA 审核政策、历史注入、变体表/分析。"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from booktr import prompts
from booktr import pipeline
from booktr.config import Config


def _analyze():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "tools", "qa_auto_probe", "analyze.py")
    spec = importlib.util.spec_from_file_location("qa_var_analyze", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg():
    return Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_style_presets_present_and_idempotent():
    keys = {k for k, _, _ in pipeline.TRANSLATION_STYLES}
    assert {"standard", "faithful", "fluent"} <= keys
    r = pipeline.apply_style_preset("基础", "faithful")
    assert "翻译风格：忠实优先" in r
    # 幂等
    assert r == pipeline.apply_style_preset(r, "faithful")
    assert "翻译风格：流畅优先" in pipeline.apply_style_preset("基础", "fluent")


def test_qa_system_audit_policy_toggle():
    cfg = _cfg()
    on = prompts.build_qa_system(cfg, "", audit_policy=True)
    off = prompts.build_qa_system(cfg, "", audit_policy=False)
    assert "审核政策" in on
    assert "可接受损失" in on
    assert "审核政策" not in off
    # 无 user_rules 时不出现该节标题（政策本身含"用户规则"字样，但非标题）
    assert "## 用户附加规则" not in prompts.build_qa_system(cfg, "")


def test_qa_history_block_render():
    h = [
        {"segments": [3], "status": "applied", "reason": "生硬",
         "old_translation": "因为实在太瞬间了", "new_translation": "因为一切发生得太快"},
        {"segments": [7], "status": "rejected", "reason": "可接受意译",
         "suggestion": "改回直译", "llm_suggestion": "旧建议"},
    ]
    blk = prompts.build_qa_history_block(h)
    assert "已发生的 QA 决策" in blk
    assert "曾**采纳**" in blk and "因为一切发生得太快" in blk
    assert "曾**拒绝**" in blk and "另存 LLM 原始建议" in blk
    assert prompts.build_qa_history_block([]) == ""


def test_build_qa_user_includes_history():
    u = prompts.build_qa_user("原文", "译文", [], history_block="## 历史\nX")
    assert "## 历史" in u


def test_qa_build_history_from_queue(tmp_cfg):
    from booktr import qa, qa_queue as qq
    from booktr import history as hist, util
    os.makedirs(tmp_cfg.work_dir, exist_ok=True)
    # 造一条 applied + 一条 rejected
    it1 = qq.make_item("page1.html", {"severity": "low", "reason": "生硬",
                                      "src_quote": "a", "dst_quote": "b",
                                      "suggestion": "c", "segments": [1]})
    it1["status"] = "applied"
    it2 = qq.make_item("page1.html", {"severity": "low", "reason": "误报",
                                      "src_quote": "x", "dst_quote": "y",
                                      "suggestion": "z", "segments": [2]})
    it2["status"] = "rejected"
    qq.save(tmp_cfg, [it1, it2])
    hist_out = qa.build_history(tmp_cfg, "page1.html")
    assert len(hist_out) == 2
    assert {h["status"] for h in hist_out} == {"applied", "rejected"}


def test_variant_table_has_v0_to_v5():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "tools", "qa_auto_probe", "probe.py")
    spec = importlib.util.spec_from_file_location("qa_var_probe", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert set(m.VARIANT_TABLE) == {"V0", "V1", "V2", "V3", "V4", "V5"}
    assert m.VARIANT_TABLE["V5"]["style"] == "fluent"
    assert m.VARIANT_TABLE["V0"]["policy"] is False


def test_variant_analysis_smoke(tmp_path):
    m = _analyze()
    vdir = str(tmp_path / "v")
    runs = os.path.join(vdir, "variant_runs")
    os.makedirs(runs, exist_ok=True)
    for i in (1, 2):
        json.dump({"variant": "V1", "run": i, "end": "qa",
                   "qa": {"p/a.html": [
                       {"severity": "low", "reason": "生硬", "segments": [1]}]},
                   "llm": {"prompt_tokens": 10, "completion_tokens": 5, "calls": 1},
                   "duration_s": 2.0},
                  open(os.path.join(runs, f"qa_V1_run{i}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    txt = m.variant_analysis(vdir, str(tmp_path / "o"))
    assert "qa V1" in txt and "跨次稳定性" in txt
    assert os.path.exists(os.path.join(str(tmp_path / "o"), "variant_analysis.txt"))
