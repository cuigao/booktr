"""terms-scan 测试：机械初筛算法、上下文、去重、LLM 辨析。"""
from __future__ import annotations

import json
import os

from booktr import terms
from booktr import util


def _texts():
    return {
        "a.html": "HOME\nNEXT PAGE\n岡崎律子のプライベートCDを作りました。Ritzberry Fields",
        "b.html": "HOME\nNEXT PAGE\n岡崎律子のプライベートCD、メロキュアのアルバム。",
        "c.html": "HOME\nNEXT PAGE\nプライベートCD。メロキュア。",
    }


def test_runs_counts_and_pages():
    res = terms.algo_runs(_texts())
    assert res["HOME"]["count"] == 3
    assert sorted(res["HOME"]["pages"]) == ["a.html", "b.html", "c.html"]
    # 混排专名（片假名+拉丁）应被捕获
    assert any("プライベートCD" == k for k in res)


def test_extract_dedup_whitespace_variant():
    texts = {"a.html": "NEXT PAGE\nNEXT PAGE", "b.html": "NEXTPAGE\nNEXT PAGE"}
    cands = terms.extract(None, algos=("runs",), min_count=1, min_pages=1,
                          texts=texts)
    srcs = [c["src"] for c in cands]
    # NEXT PAGE / NEXTPAGE 归一合并为一条（保留含空格原形）
    assert srcs.count("NEXT PAGE") == 1
    assert "NEXTPAGE" not in srcs


def test_extract_contexts_and_script():
    cands = terms.extract(None, algos=("runs",), min_count=1, min_pages=1,
                          context_chars=6, texts=_texts())
    home = next(c for c in cands if c["src"] == "HOME")
    assert home["script"] == "latin"
    assert home["contexts"]  # 有源文窗口
    assert all(len(x) <= 40 for x in home["contexts"])


def test_extract_thresholds():
    texts = {"a.html": "AAA", "b.html": "BBB"}
    # min_count=2：AAA/BBB 各 1 次 → 过滤
    assert terms.extract(None, algos=("runs",), min_count=2, min_pages=2,
                         texts=texts) == []


def test_repeat_lines(tmp_cfg):
    texts = {
        "a.html": "●価格：1,700円\nHOME",
        "b.html": "●価格：1,700円\nHOME",
        "c.html": "●価格：1,700円",
    }
    res = terms.algo_repeat_lines(texts)
    assert res["HOME"]["count"] == 2
    assert "●価格：1,700円" in res


def test_review_uses_fake_llm(tmp_cfg):
    from conftest import FakeLLM

    cands = [
        {"src": "HOME", "script": "latin", "count": 9, "pages": ["x"], "contexts": ["HOME"]},
        {"src": "メール", "script": "kana", "count": 5, "pages": ["x"], "contexts": ["メール"]},
    ]

    def responder(user):
        return json.dumps({"terms": [
            {"src": "HOME", "keep": True, "dst": "首页", "category": "nav", "note": "导航"},
            {"src": "メール", "keep": False, "dst": "", "category": "term", "note": ""},
        ]}, ensure_ascii=False)

    kept = terms.review(tmp_cfg, FakeLLM(responder=responder), cands, batch_size=10)
    assert len(kept) == 1
    assert kept[0]["src"] == "HOME" and kept[0]["dst"] == "首页"


def test_review_injects_existing_glossary(tmp_cfg, tmp_path):
    """辨析请求应含现行词汇表，避免与既有译法冲突。"""
    from conftest import FakeLLM
    from booktr import glossary as gl

    gl.add_term(tmp_cfg, "岡崎", "岡崎", note="保留原形")
    seen = {}

    class Rec(FakeLLM):
        def chat(self, system, user, **k):
            seen["user"] = user
            return json.dumps({"terms": []}, ensure_ascii=False)

    terms.review(tmp_cfg, Rec(), [{"src": "X", "count": 2, "pages": ["a"],
                                   "contexts": [], "script": "latin"}])
    assert "岡崎" in seen["user"]
    assert "现行词汇表" in seen["user"]


def test_extract_count_band_and_scripts():
    texts = {"a.html": "AAA\nBBB\nCCC", "b.html": "AAA\nBBB", "c.html": "AAA"}
    # count: AAA=3, BBB=2, CCC=1；pages: AAA=3, BBB=2, CCC=1
    band = terms.extract(None, algos=("runs",), min_count=2, count_max=3,
                         scripts=("latin",), min_pages=2, texts=texts)
    srcs = {c["src"] for c in band}
    assert "BBB" in srcs          # count2 在 band [2,3) 且 pages2>=2
    assert "CCC" not in srcs      # count1 < min_count
    assert "AAA" not in srcs      # count3 >= count_max


def test_extract_maximality_drop_substrings():
    texts = {"a.html": "プライベートCD プライベートCD", "b.html": "プライベートCD"}
    # 丢弃「プライベート」（是 プライベートCD 的子串）
    out = terms.extract(None, algos=("runs",), min_count=1, scripts=("kana",),
                        drop_substrings={"プライベートCD"}, min_pages=1, texts=texts)
    srcs = {c["src"] for c in out}
    assert "プライベート" not in srcs
    # 非子串的保留（此处 プライベートCD 自身由 drop 集合=自身，也应在集合内但
    # 规则要求严格子串才丢，故不应丢自身）——直接验证严格性：
    out2 = terms.extract(None, algos=("runs",), min_count=1, scripts=("kana",),
                         drop_substrings={"XXX"}, min_pages=1, texts=texts)
    assert any("プライベート" == c["src"] for c in out2)


def test_scan_two_pass_and_no_pass2(tmp_cfg):
    from conftest import FakeLLM

    def responder(user):
        return json.dumps({"terms": []}, ensure_ascii=False)

    fake = FakeLLM(responder=responder)
    texts = {"a.html": "HOME\nプライベートCD\nメロキュア", "b.html": "HOME\nプライベートCD"}
    r1 = terms.scan(tmp_cfg, fake, band_split=2, min_count=1, pass2=True,
                    pass2_scripts=("kana",), texts=texts)
    assert "pass1_candidates" in r1 and "pass2_candidates" in r1
    assert "reviewed" in r1
    # --no-pass2
    r2 = terms.scan(tmp_cfg, fake, band_split=2, min_count=1, pass2=False,
                    texts=texts)
    assert r2["pass2_candidates"] == []
    assert r2["reviewed"] == r2["pass1"]


def test_scan_no_client_only_candidates(tmp_cfg):
    texts = {"a.html": "HOME", "b.html": "HOME"}
    r = terms.scan(tmp_cfg, None, band_split=2, texts=texts)
    assert r["pass1"] == [] and r["reviewed"] if "reviewed" in r else True
    assert r["pass1_candidates"]


def test_manual_lines_format_and_order():
    items = [{"src": "HOME", "dst": "首页", "category": "nav", "note": "导航",
              "status": "x", "confidence": 0.5}]
    txt = terms.to_manual_lines(items)
    lines = txt.splitlines()
    assert lines[0] == "[" and lines[-1] == "]"
    assert len(lines) == 3
    rec = __import__("json").loads(lines[1].rstrip(",").strip())
    assert list(rec.keys()) == ["src", "dst", "category", "note"]  # 顺序固定
    assert "status" not in rec and "confidence" not in rec


def test_parse_manual_tolerant():
    text = ('[\n'
            '  {"src": "A", "dst": "a", "category": "term", "note": ""},\n'
            '  {"src": "B", "dst": "b", "category": "nav", "note": "n"},\n'
            '  broken line\n'
            '  {"dst": "no-src", "category": "term"},\n'
            ']\n')
    entries, skipped = terms.parse_manual(text)
    assert {e["src"] for e in entries} == {"A", "B"}
    assert len(skipped) == 2  # broken + no-src


def test_import_confirmed_add_update_conflict(tmp_cfg):
    from booktr import glossary as gl
    gl.add_term(tmp_cfg, "HOME", "首页", category="nav", note="old")
    res = gl.import_confirmed(tmp_cfg, [
        {"src": "HOME", "dst": "主页", "category": "nav", "note": "new"},   # update+conflict
        {"src": "PHOTO DIARY", "dst": "PHOTO DIARY", "category": "nav"},     # add
        {"src": "", "dst": "x"},                                             # skip
    ])
    assert res["added"] == 1 and res["updated"] == 1
    assert res["conflicts"] == [{"src": "HOME", "from": "首页", "to": "主页"}]
    items = {e["src"]: e for e in gl.load(tmp_cfg)}
    assert items["HOME"]["dst"] == "主页" and items["HOME"]["status"] == "confirmed"
    assert items["HOME"]["read_only"] is True
    assert items["PHOTO DIARY"]["status"] == "confirmed"


def test_glossary_backup(tmp_cfg):
    from booktr import glossary as gl
    gl.add_term(tmp_cfg, "X", "Y")
    bak = gl.backup(tmp_cfg)
    assert bak and os.path.exists(bak)
    assert open(bak, encoding="utf-8").read() == \
        open(tmp_cfg.get("glossary", "path", default=""), encoding="utf-8").read()


def test_cmd_terms_review_and_apply(tmp_cfg, tmp_path):
    from booktr import pipeline, glossary as gl, util
    # 造一份 reviewed 终稿
    util.write_json(os.path.join(tmp_cfg.work_dir, "term_reviewed.json"), [
        {"src": "HOME", "dst": "首页", "category": "nav", "note": ""},
        {"src": "メール", "dst": "邮件", "category": "term", "note": ""},
    ])
    pipeline.cmd_terms_review(tmp_cfg, type("A", (), {"from": None, "out": None})())
    manual = os.path.join(tmp_cfg.work_dir, "term_review_manual.json")
    assert os.path.exists(manual)
    # 人工：删除 メール 行
    lines = [ln for ln in open(manual, encoding="utf-8").read().splitlines()
             if "メール" not in ln]
    open(manual, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    pipeline.cmd_terms_apply(tmp_cfg, type("A", (), {"path": None, "dry_run": False})())
    items = {e["src"] for e in gl.load(tmp_cfg)}
    assert "HOME" in items and "メール" not in items


def test_prompts_term_review_strict():
    from booktr import prompts
    from booktr.config import Config
    import os
    cfg = Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    normal = prompts.build_term_review_system(cfg, strict=False)
    strict = prompts.build_term_review_system(cfg, strict=True)
    assert "从严" in strict and "低频" in strict
    assert "从严" not in normal


def test_prompts_term_review_system_has_principles():
    from booktr import prompts
    from booktr.config import Config
    import os
    cfg = Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    s = prompts.build_term_review_system(cfg)
    assert "专名" in s and "专业" in s
    assert "ディレクター" in s  # 领域词示例
    assert "keep" in s and "drop" in s
