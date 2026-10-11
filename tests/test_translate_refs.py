# -*- coding: utf-8 -*-
"""参考译文参考层：translate_page 集成测试（tmp_cfg + FakeLLM）。"""
from __future__ import annotations

import json
import os

from booktr import glossary as gl
from booktr import prompts
from booktr import refs
from booktr import translate as tr
from conftest import FakeLLM, _extract_text, write_sample_site

REF_MAP = {
    "こんにちは。": "你好。",
    "今日はいい天気です。": "今天天气很好。",
    "明日は雨が降るでしょう。": "明天大概会下雨吧。",
    "それではまた。": "那么再见。",
    "HOME": "首页",
}
REF_TEXT = "\n".join(REF_MAP.values())


def _write_ref(cfg, rel, text):
    store = cfg.get("refs", "store_dir")
    path = os.path.join(store, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _ok(translation, corrections=None, **kw):
    d = {"translation": translation, "confidence": 1.0,
         "glossary_conflicts": [], "notes": [], "needs_human": False}
    if corrections is not None:
        d["corrections"] = corrections
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def _src_of(user):
    return refs.plain(_extract_text(user))


def test_ref_first_message_contains_reference_and_no_glossary(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)
    # 放一个词汇表条目，参考模式应不注入
    gl.save(tmp_cfg, [{"src": "こんにちは", "dst": "哈喽", "category": "term",
                       "status": "confirmed", "read_only": True}])
    seen = []

    def responder(user):
        seen.append(user)
        return _ok(REF_MAP.get(_src_of(user), _src_of(user)))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    first = seen[0]
    assert "本页完整原文" in first
    assert "本页参考译文" in first
    assert "推荐翻译译文" not in first        # 词表未注入
    assert "哈喽" not in first


def test_ref_pass_no_qa_and_clean(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)

    def responder(user):
        return _ok(REF_MAP.get(_src_of(user), _src_of(user)))

    state = tr.State(tmp_cfg)
    review = []
    result = tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                               state, {}, {}, review)
    assert result["status"] == "done"
    assert refs.active_corrections(tmp_cfg, "page1.html") == []
    from booktr import qa_queue as qq
    assert qq.load(tmp_cfg) == []


def test_ref_mismatch_retry_then_enqueue(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)

    def responder(user):
        s = _src_of(user)
        if s == "今日はいい天気です。":
            return _ok("今天天气糟透了")  # 始终不一致
        return _ok(REF_MAP.get(s, s))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    from booktr import qa_queue as qq
    items = qq.load(tmp_cfg)
    assert any(i.get("reason") == "参考译文不一致" for i in items)


def test_ref_rules_priority_and_notes(tmp_cfg):
    sysp = prompts.build_translate_system(tmp_cfg, [], "", "", "", ref_mode=True)
    assert "最高优先级" in sysp and "优先于" in sysp
    assert "彩蛋" in sysp and "额外板块" in sysp


def test_ref_tail_preserved(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", "\n".join(REF_MAP.values()) + "\n【彩蛋】\n额外地址说明。")

    def responder(user):
        return _ok(REF_MAP.get(_src_of(user), _src_of(user)))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    out = (tmp_path / "out" / "page1.html").read_text(encoding="utf-8")
    assert "额外地址说明" in out  # 参考末尾彩蛋被兜底保留


def test_ref_mode_readonly_nav_mechanical(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)
    gl.save(tmp_cfg, [{"src": "HOME", "dst": "页首", "category": "nav",
                       "status": "confirmed", "read_only": True}])
    seen = []

    def responder(user):
        seen.append(user)
        return _ok(REF_MAP.get(_src_of(user), _src_of(user)))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    out = (tmp_path / "out" / "page1.html").read_text(encoding="utf-8")
    assert "页首" in out                       # read_only 机械替换在参考模式生效
    assert not any(_src_of(u) == "HOME" for u in seen)  # 导航段未送 LLM


def test_ref_correction_not_retried(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)

    def responder(user):
        s = _src_of(user)
        if s == "今日はいい天気です。":
            return _ok("今天天气糟透了。", corrections=[
                {"ref_err": "今天天气很好。", "corrected": "今天天气糟透了。", "reason": "x"}])
        return _ok(REF_MAP.get(s, s))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    from booktr import qa_queue as qq
    assert not any(i.get("reason") == "参考译文不一致" for i in qq.load(tmp_cfg))


def test_ref_corrections_recorded(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    _write_ref(tmp_cfg, "page1.html", REF_TEXT)

    def responder(user):
        s = _src_of(user)
        if s == "こんにちは。":
            return _ok("你好。", corrections=[
                {"ref_err": "你好", "corrected": "您好", "reason": "更礼貌"}])
        return _ok(REF_MAP.get(s, s))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    cors = refs.active_corrections(tmp_cfg, "page1.html")
    assert any(c["ref_err"] == "你好" and c["corrected"] == "您好" for c in cors)


def test_no_ref_falls_back(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    seen = []

    def responder(user):
        seen.append(user)
        return _ok("译：" + _src_of(user))

    state = tr.State(tmp_cfg)
    tr.translate_page(tmp_cfg, FakeLLM(responder=responder), "page1.html",
                      state, {}, {}, [])
    assert "本页参考译文" not in seen[0]  # 无参考 → 常规 prompt


def test_export_annotated_renders_corrections(tmp_cfg, tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "page1.html").write_text(
        "<html><head><title>t</title></head><body><p>你好世界。</p></body></html>",
        encoding="utf-8")
    refs.record_corrections(tmp_cfg, "page1.html", "1",
                            [{"ref_err": "大自然", "corrected": "你好世界", "reason": "专名"}])
    from booktr import annotator
    res = annotator.export_annotated(tmp_cfg, str(tmp_path / "out_annotated"))
    annotated = (tmp_path / "out_annotated" / "page1.html").read_text(encoding="utf-8")
    assert "参考勘误" in annotated
    assert res["ok"] >= 1

