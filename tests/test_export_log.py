# -*- coding: utf-8 -*-
"""export-log 对话序列附录测试。"""
from __future__ import annotations

import json
import os

import pytest

from booktr.pipeline import _format_appendix, _format_assistant_content, export_page_log


def _log(tag, ts, task_id, context_id, messages, response="", system="", user=""):
    return {
        "ts": ts, "tag": tag, "ok": True, "error": "", "usage": {},
        "system": system, "user": user, "response": response,
        "task_id": task_id, "context_id": context_id, "messages": messages,
    }


def _msg(role, content):
    return {"role": role, "content": content}


# ── _format_assistant_content ───────────────────────────────────────────


def test_assistant_json_parsed_then_raw():
    resp = json.dumps({"translation": "你好", "confidence": 0.9, "needs_human": False,
                       "glossary_conflicts": [], "notes": []}, ensure_ascii=False)
    lines = _format_assistant_content(resp)
    text = "\n".join(lines)
    assert "译文：" in text
    assert "你好" in text
    assert "confidence=0.9" in text
    assert "```json" in text
    assert '"translation": "你好"' in text


def test_assistant_non_json_raw():
    lines = _format_assistant_content("纯文本响应")
    assert "原始响应：" in lines
    assert "纯文本响应" in lines


def test_assistant_content_repair_shows_fixed_and_raw():
    # 未转义引号的坏 JSON：显示修复译文 + 原始未修复响应
    bad = '{"translation": "他说\u201c真棒\u201d然后说"真的"走了", "confidence": 0.9, "glossary_conflicts": [], "notes": [], "needs_human": false}'
    lines = _format_assistant_content(bad)
    text = "\n".join(lines)
    assert "译文（⚠ 修复 ESCAPE_VALUE_STRINGS）：" in text
    assert "然后说\"真的\"走了" in text  # 修复后译文保留引号（未删除）
    assert "原始响应：" in text
    # 原始非法 JSON 以纯文本原样展示
    assert '说"真的"走了' in text


# ── _format_appendix ───────────────────────────────────────────────────


def test_appendix_single_context_linear():
    # 单 context，含 repair 插入（线性累积）
    ctx = "ctx_1"
    tid = "tsk_1"
    logs = [
        _log("translate_p.html", "2026-01-01T00:00:00", tid, ctx,
             [_msg("system", "SYS"), _msg("user", "U1")],
             response=json.dumps({"translation": "T1", "confidence": 0.9})),
        # repair：messages 累积（含 repair user + assistant 响应）
        _log("repair_p.html_seg1", "2026-01-01T00:00:01", tid, ctx,
             [_msg("system", "SYS"), _msg("user", "U1"),
              _msg("assistant", "BAD"), _msg("user", "REPAIR_HINT"),
              _msg("assistant", json.dumps({"translation": "T1fix", "confidence": 0.8}))],
             response=json.dumps({"translation": "T1fix", "confidence": 0.8})),
    ]
    lines = _format_appendix([logs])
    text = "\n".join(lines)
    # 完整序列含 repair 的 user 提示
    assert "### system" in text
    assert "### user" in text
    assert "REPAIR_HINT" in text
    assert "T1fix" in text
    # 单 context 无横线分隔（无第二个对话段）
    assert "对话段 2" not in text


def test_appendix_multi_context_separated():
    # 两个 context（摘要接力）→ 横线分隔 + 原因标注
    tid = "tsk_1"
    ctx1 = "ctx_1"
    ctx2 = "ctx_2"
    logs = [
        _log("translate_p.html", "2026-01-01T00:00:00", tid, ctx1,
             [_msg("system", "SYS1"), _msg("user", "U1")],
             response=json.dumps({"translation": "T1", "confidence": 0.9})),
        _log("summarize_conv_p.html", "2026-01-01T00:00:02", tid, ctx1,
             [_msg("system", "SYS1"), _msg("user", "U1"), _msg("assistant", "A1")],
             response="摘要"),
        _log("translate_p.html", "2026-01-01T00:00:03", tid, ctx2,
             [_msg("system", "SYS2"), _msg("user", "U2")],
             response=json.dumps({"translation": "T2", "confidence": 0.9})),
    ]
    lines = _format_appendix([logs])
    text = "\n".join(lines)
    assert "对话段 1" in text
    assert "对话段 2" in text
    assert "摘要接力后新对话" in text
    assert "SYS2" in text


def test_appendix_old_log_fallback():
    # 无 messages 的旧日志 → 回退 system/user/response
    tid = "tsk_1"
    logs = [_log("translate_p.html", "2026-01-01T00:00:00", tid, "",
                 [], response=json.dumps({"translation": "T", "confidence": 0.9}),
                 system="SYS", user="USER")]
    lines = _format_appendix([logs])
    text = "\n".join(lines)
    assert "### system" in text
    assert "SYS" in text
    assert "### user" in text
    assert "USER" in text
    assert "T" in text


# ── export_page_log 附录开关 ───────────────────────────────────────────


def _write_logs(tmp_cfg, page):
    log_dir = tmp_cfg.get("llm_logs", "dir", default="work/llm_logs")
    os.makedirs(log_dir, exist_ok=True)
    tid = "tsk_1"
    ctx = "ctx_1"
    logs = [
        _log("translate_p.html", "2026-01-01T00:00:00", tid, ctx,
             [_msg("system", "SYS"), _msg("user", "U1")],
             response=json.dumps({"translation": "T1", "confidence": 0.9})),
    ]
    for i, l in enumerate(logs):
        with open(os.path.join(log_dir, f"log{i}.json"), "w", encoding="utf-8") as f:
            json.dump(l, f, ensure_ascii=False)


def test_export_log_appendix_default(tmp_cfg, tmp_path):
    _write_logs(tmp_cfg, "p.html")
    out = export_page_log(tmp_cfg, "p.html", output_path=str(tmp_path / "out.md"))
    assert out
    with open(out, encoding="utf-8") as f:
        text = f.read()
    assert "## 附录：完整对话序列" in text
    assert "### system" in text


def test_export_log_no_messages(tmp_cfg, tmp_path):
    _write_logs(tmp_cfg, "p.html")
    out = export_page_log(tmp_cfg, "p.html", output_path=str(tmp_path / "out.md"),
                          no_messages=True)
    with open(out, encoding="utf-8") as f:
        text = f.read()
    assert "## 附录：完整对话序列" not in text
    # 主日志仍在
    assert "# LLM 对话日志" in text
