"""terms-scan 测试：机械初筛算法、上下文、去重、LLM 辨析。"""
from __future__ import annotations

import json

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


def test_prompts_term_review_system_has_principles():
    from booktr import prompts
    from booktr.config import Config
    import os
    cfg = Config(root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    s = prompts.build_term_review_system(cfg)
    assert "专名" in s and "专业" in s
    assert "ディレクター" in s  # 领域词示例
    assert "keep" in s and "drop" in s
