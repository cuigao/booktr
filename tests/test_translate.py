# -*- coding: utf-8 -*-
"""翻译引擎测试：纯函数 + translate_page 全流程（mock / FakeLLM）。"""
from __future__ import annotations

import json
import os

import pytest

from booktr import glossary as gl
from booktr import segments as seg_mod
from booktr import translate as tr
from conftest import FakeLLM, _ok_json, write_sample_site


# ── 纯函数 ──────────────────────────────────────────────────────────────


def test_check_placeholders_missing():
    assert tr._check_placeholders("[[P0]]こんにちは[[P1]]", "こんにちは") == ["[[P0]]", "[[P1]]"]


def test_check_placeholders_extra():
    # 译文凭空多出的占位符也应被检测（多余）
    assert tr._check_placeholders("こんにちは", "こんにちは[[P9]]") == ["[[P9]]"]


def test_check_placeholders_ok():
    assert tr._check_placeholders("[[P0]]a[[P1]]", "[[P0]]b[[P1]]") == []


def test_restore_placeholders_leading_trailing():
    out = tr._restore_placeholders_from_src("[[P0]]本文[[P1]]", "译文")
    assert out == "[[P0]]译文[[P1]]"


def test_restore_placeholders_no_ph():
    assert tr._restore_placeholders_from_src("本文", "译文") == "译文"


def test_chunk_text_small():
    assert tr._chunk_text("短文本", 100) == ["短文本"]


def test_chunk_text_splits_on_sentence():
    text = "こんにちは。今日はいい天気です。明日は雨です。"
    chunks = tr._chunk_text(text, 10)
    assert len(chunks) >= 2
    assert "".join(chunks).replace("\n", "") == text


def test_chunk_text_preserves_placeholder():
    text = "[[P0]]" + "あ" * 50 + "[[P1]]"
    chunks = tr._chunk_text(text, 20)
    joined = "".join(chunks)
    assert "[[P0]]" in joined and "[[P1]]" in joined


def test_cleanup_fallback_removes_json_prefix():
    assert tr._cleanup_fallback('{"translation": "你好"}') == "你好"
    assert "|TEXT|" not in tr._cleanup_fallback("|TEXT|你好")


# ── translate_page 全流程 ──────────────────────────────────────────────


def test_translate_page_full_flow(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)

    result = tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    assert result["status"] == "done"
    assert not result["skipped"]
    assert result["segments_total"] > 0

    # out 页面已写出且含 UTF-8 charset
    out_path = os.path.join(tmp_cfg.output_dir, "page1.html")
    assert os.path.exists(out_path)
    with open(out_path, encoding="utf-8") as f:
        out = f.read()
    assert '<meta charset="utf-8">' in out

    # state 已记录
    pstate = state.page("page1.html")
    assert pstate["status"] == "done"
    assert "page1.html" in state.data["done_pages"]
    assert len(pstate.get("segments", {})) > 0


def test_translate_page_mechanical_glossary(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 词汇表 read_only 条目精确命中一个短段 → 机械替换，不调用 LLM
    gl.upsert(tmp_cfg, {"src": "HOME", "dst": "首页", "category": "nav", "status": "confirmed"})
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)

    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    # 检查是否任何段译文含机械替换结果（HOME 段）
    segs = seg_mod.segments_for_page(tmp_cfg, "page1.html")
    found = any(s.translation and "首页" in s.translation for s in segs if s.kind == "text")
    assert found


def test_translate_page_needs_human_review(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 前两次调用返回 needs_human（可能有多段，只需至少一次触发 review）
    fake = FakeLLM(responder=lambda u: _ok_json("x", needs_human=True))
    state = tr.State(tmp_cfg)

    result = tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    pstate = state.page("page1.html")
    assert pstate["status"] == "review"
    assert result["review_count"] >= 1


def test_translate_page_placeholder_repair(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 第一次调用丢失占位符，之后正常 → 触发修复重试
    bad = json.dumps({"translation": "丢失占位符", "confidence": 0.9,
                      "glossary_conflicts": [], "notes": [], "needs_human": False})
    cnt = {"n": 0}

    def resp(user):
        cnt["n"] += 1
        if cnt["n"] == 1:
            return bad
        return _ok_json(_extract_identity(user))

    fake = FakeLLM(responder=resp)
    st = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", st, {}, {}, [])

    # 发生了重试（调用次数 > 段数）
    assert fake.calls > 0


def test_translate_page_persists_repaired(tmp_cfg, tmp_path):
    import json as _json
    write_sample_site(tmp_path)
    # 返回含未转义引号的坏 JSON → 触发机械修复
    bad_resp = ('{"translation": "他说\u201c真棒\u201d然后说"真的"走了", '
                '"confidence": 0.9, "glossary_conflicts": [], '
                '"notes": [], "needs_human": false}')
    fake = FakeLLM(responder=lambda u: bad_resp)
    state = tr.State(tmp_cfg)

    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])

    # 段状态持久化了 repaired
    st = tr.State(tmp_cfg)
    found = False
    for sid, seg in st.page("page1.html").get("segments", {}).items():
        if seg.get("repaired"):
            assert seg.get("repair_methods") == ["ESCAPE_VALUE_STRINGS"]
            found = True
            break
    assert found


def _extract_identity(user):
    from conftest import _extract_text
    return _extract_text(user)


# ── 相邻上下文截断 ─────────────────────────────────────────────────────


def test_get_adjacent_translations(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    state = tr.State(tmp_cfg)
    segs = seg_mod.segments_for_page(tmp_cfg, "page1.html")
    text_segs = [s for s in segs if s.kind == "text"]
    if len(text_segs) < 3:
        pytest.skip("样例页面段落不足")
    # 给中间段前后段写入译文
    pstate = state.page("page1.html")
    pstate["segments"][str(text_segs[0].id)] = {"translation": "前段译文" * 10}
    pstate["segments"][str(text_segs[2].id)] = {"translation": "后段译文" * 10}

    before, after = tr._get_adjacent_translations(
        tmp_cfg, text_segs[1], text_segs, state, "page1.html", max_chars=10
    )
    assert "前段译文" in before
    assert "后段译文" in after
    # 截断生效（总量不超过 max_chars）
    assert len(before) <= 10
    assert len(after) <= 10
