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


def test_check_placeholders_duplicate():
    # 原文 1 个 [[P0]]，译文 2 个 → 检测重复
    assert tr._check_placeholders("[[P0]]a", "[[P0]]a[[P0]]") == ["[[P0]]"]


def test_check_placeholders_duplicate_mixed():
    # 缺失 + 重复同时存在
    assert tr._check_placeholders("[[P0]]a[[P1]]", "[[P0]]a[[P0]]") == ["[[P0]]", "[[P1]]"]


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


# ── 后续消息 TM 命中注入 ─────────────────────────────────────────────


def test_build_translate_user_subsequent_tm_hits(tmp_cfg):
    from booktr import prompts
    usr = prompts.build_translate_user_subsequent(
        tmp_cfg, "こんにちは", tm_hits=[{"src": "こんにちは", "dst": "你好"}]
    )
    assert "翻译记忆命中" in usr
    assert "こんにちは → 你好" in usr
    assert "### 待翻译文本" in usr


def test_build_translate_user_subsequent_no_tm(tmp_cfg):
    from booktr import prompts
    usr = prompts.build_translate_user_subsequent(tmp_cfg, "こんにちは")
    assert "翻译记忆命中" not in usr
    assert "### 待翻译文本" in usr


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


# ── TM 写入守卫（规范化比较）────────────────────────────────────────


def test_tm_add_skips_normalized_identical(tmp_cfg):
    """规范化后 src==dst（仅空白差异）不写入 TM。"""
    from booktr import tm as tm_mod
    # 模拟 Photo 类：src 带前导 \n，dst 无，规范化后相同
    tm_mod.add(tmp_cfg, "\n[[P0]] [Photo / X] [[P1]]", "[[P0]] [Photo / X] [[P1]]",
               "today/today1.html", 2)
    assert tm_mod.size(tmp_cfg) == 0


def test_tm_add_keeps_real_translation(tmp_cfg):
    """真正翻译（规范化后不同）写入 TM。"""
    from booktr import tm as tm_mod
    tm_mod.add(tmp_cfg, "こんにちは", "你好", "today/today1.html", 2)
    assert tm_mod.size(tmp_cfg) == 1


# ── 用户附加规则注入 ────────────────────────────────────────────────


def test_system_prompt_injects_user_rules(tmp_cfg):
    """user_rules 内容注入 system prompt 的『用户附加规则』小节。"""
    from booktr import prompts
    sysp = prompts.build_translate_system(
        tmp_cfg, [], "", "保留全角写法", "", is_retranslation=False
    )
    assert "## 用户附加规则" in sysp
    assert "保留全角写法" in sysp


def test_retranslate_rules_refer_user_rules(tmp_cfg):
    """重译规则表达『用户附加规则仍然适用』，不强调全角/人名。"""
    from booktr import prompts
    sysp = prompts.build_translate_system(
        tmp_cfg, [], "", "", "", is_retranslation=True
    )
    assert "用户附加规则仍然适用" in sysp
    assert "保留全角写法" not in sysp


def test_user_rules_priority_line(tmp_cfg):
    """system 的『用户附加规则』小节含优先级声明。"""
    from booktr import prompts
    sysp = prompts.build_translate_system(
        tmp_cfg, [], "", "全角规则", "", is_retranslation=False
    )
    assert "（如有冲突，以本条用户附加规则为准）" in sysp
    assert "全角规则" in sysp


# ── 重译上下文补齐 ────────────────────────────────────────────────────


def test_retranslate_user_includes_full_context(tmp_cfg):
    """重译 user 消息渲染词条/跨页前导/翻译记忆/风格样例，不弱于初译。"""
    from booktr import prompts
    ctx = {
        "term_hints": "## 推荐翻译译文\n- リッツ → Ritz",
        "prior_ctx": "前页摘要内容",
        "summary": "页面摘要",
        "page_ctx": "标题：测试",
        "tm_hits": [{"src": "foo", "dst": "bar"}],
        "exemplars": [{"src": "src例", "dst": "dst例"}],
        "context_before": "前文已译",
        "context_after": "后文已译",
    }
    usr = prompts.build_retranslate_user(tmp_cfg, "待译", ctx)
    assert "リッツ → Ritz" in usr
    assert "前页摘要内容" in usr
    assert "foo → bar" in usr
    assert "src例" in usr and "dst例" in usr
    assert "前文已译" in usr
    assert "后文已译" in usr


def test_retranslate_user_minimal(tmp_cfg):
    """上下文字段缺失时不渲染对应小节。"""
    from booktr import prompts
    usr = prompts.build_retranslate_user(tmp_cfg, "待译", {})
    assert "## 翻译记忆命中" not in usr
    assert "## 风格参照样例" not in usr
    assert "### 待翻译文本" in usr or "## 待翻译文本" in usr


def test_reset_segment_gets_term_hints(tmp_cfg, tmp_path):
    """段级重译（translation=None）注入词汇表 term hint，非重置段被复制。"""
    from booktr import phrases as phrases_mod
    write_sample_site(tmp_path)
    gl.upsert(tmp_cfg, {"src": "今日", "dst": "Ritz", "category": "term", "status": "confirmed"})

    # 先正常翻译一页（FakeLLM 恒等）
    fake = FakeLLM.default()
    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, fake, "page1.html", state, {}, {}, [])
    # 清空短语记忆，避免重译被机械替换（不走 LLM）
    phrases_mod.save(tmp_cfg, {})
    segs = seg_mod.segments_for_page(tmp_cfg, "page1.html")
    target = next(s for s in segs if s.kind == "text" and "今日" in s.text)
    keep = next(s for s in segs if s.kind == "text" and "今日" not in s.text)
    keep_tr = state.page("page1.html")["segments"][str(keep.id)]["translation"]

    # 重置该段（模拟 reset --segments），再翻译
    state = tr.State(tmp_cfg)
    state.page("page1.html")["segments"][str(target.id)]["translation"] = None
    state.page("page1.html")["status"] = "pending"
    state.save()
    captured = {}

    def resp(user):
        captured["user"] = user
        return _ok_json("Ritz")

    fake2 = FakeLLM(responder=resp)
    tr.translate_page(tmp_cfg, fake2, "page1.html", state, {}, {}, [])

    # 重译 prompt 含词条推荐译法
    assert "今日 → Ritz" in captured.get("user", "")
    # 非重置段译文被原样保留
    assert state.page("page1.html")["segments"][str(keep.id)]["translation"] == keep_tr


# ── 链接/时间前导 ─────────────────────────────────────────────────────

def _mk_site_map():
    """构造合成 site_map：today 系列 + photo 系列 + 链接关系。"""
    pages = {
        "today/today1.html": {"kind": "diary", "date": "1996-10-01", "links_out": []},
        "today/today2.html": {"kind": "diary", "date": "1996-11-01", "links_out": ["today/manbow.html"]},
        "today/today3.html": {"kind": "diary", "date": "1996-12-10", "links_out": []},
        "today/manbow.html": {"kind": "special", "date": None, "links_out": []},
        "photo/photo1.html": {"kind": "photo_diary", "date": "1997-06-17", "links_out": []},
        "index.html": {"kind": "index", "date": "2004-04-28", "links_out": []},
    }
    return {"pages": pages}


def _mk_plan():
    return {"order": ["index.html", "today/today1.html", "today/today2.html",
                      "today/today3.html", "today/manbow.html", "photo/photo1.html"]}


def _ctx_cfg(tmp_cfg, **kw):
    """返回带非零前导数量的 cfg（覆盖 conftest 的 0 默认）。"""
    ctx = {"plan_predecessors": 5, "time_predecessors": 3, "link_predecessors": 3}
    ctx.update(kw)
    tmp_cfg.data.setdefault("planner", {})["context"] = ctx
    return tmp_cfg


def test_link_predecessors_finds_backlink(tmp_cfg):
    """链接前导：manbow 被 today2 链接，应返回 today2。"""
    sm = _mk_site_map()
    plan = _mk_plan()
    rels = tr._link_predecessors(_ctx_cfg(tmp_cfg), sm, plan, "today/manbow.html")
    assert "today/today2.html" in rels


def test_link_predecessors_excludes_index(tmp_cfg):
    """链接前导排除索引/导航页。"""
    sm = _mk_site_map()
    plan = _mk_plan()
    # index 链接到 today1，但 index 是索引页，不应作为前导
    sm["pages"]["index.html"]["links_out"] = ["today/today1.html"]
    rels = tr._link_predecessors(_ctx_cfg(tmp_cfg), sm, plan, "today/today1.html")
    assert "index.html" not in rels


def test_time_predecessors_chronological(tmp_cfg):
    """时间前导：date 早于当前页，按日期降序。"""
    sm = _mk_site_map()
    plan = _mk_plan()
    rels = tr._time_predecessors(_ctx_cfg(tmp_cfg), sm, plan, "photo/photo1.html")
    # photo1 date=1997-06-17，早于它的有 today1/2/3
    assert "today/today3.html" in rels
    assert "today/today2.html" in rels
    assert "today/today1.html" in rels
    # 无日期页（manbow）不作为时间前导
    assert "today/manbow.html" not in rels


def test_time_predecessors_no_date(tmp_cfg):
    """无日期的当前页无时间前导。"""
    sm = _mk_site_map()
    plan = _mk_plan()
    rels = tr._time_predecessors(_ctx_cfg(tmp_cfg), sm, plan, "today/manbow.html")
    assert rels == []


def test_path_hops():
    assert tr._path_hops("today/today2.html", "today/manbow.html") == 2
    assert tr._path_hops("today/today2.html", "photo/photo1.html") == 4


def test_norm_desc():
    assert tr._norm_desc(2, 2, 8) == 1.0
    assert tr._norm_desc(8, 2, 8) == 0.0
    assert tr._norm_desc(5, 2, 8) == 0.5
    assert tr._norm_desc(0, 2, 8) == 1.0  # 钳位
    assert tr._norm_desc(10, 2, 8) == 0.0  # 钳位


def test_build_context_related_sections(tmp_cfg):
    """build_context 注入时间/链接相关页面小节（去重）。"""
    sm = _mk_site_map()
    plan = _mk_plan()
    _ctx_cfg(tmp_cfg)
    # 为关联页写摘要
    sdir = os.path.join(tmp_cfg.data_dir, "work", "summaries")
    os.makedirs(sdir, exist_ok=True)
    for rel, text in [("today/today2.html", "today2摘要"), ("today/today3.html", "today3摘要")]:
        with open(os.path.join(sdir, rel.replace("/", "__") + ".json"), "w", encoding="utf-8") as f:
            json.dump({"summary": text}, f, ensure_ascii=False)
    page_ctx, prior_ctx, _ = tr.build_context(tmp_cfg, sm, plan, "today/manbow.html")
    # manbow 的 plan 前导（N1=5）已含 today2/today3，摘要应注入
    assert "today2摘要" in prior_ctx
    assert "today3摘要" in prior_ctx
