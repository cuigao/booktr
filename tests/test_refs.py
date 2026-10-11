"""refs 模块单测：参考读取、相似度/定位/diff、勘误 sidecar。"""
from __future__ import annotations

import os

from booktr import refs
from booktr.segments import Segment


def _write_ref(cfg, rel, text):
    store = cfg.get("refs", "store_dir")
    path = os.path.join(store, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_plain_and_strip():
    assert refs.strip_placeholders("a[[P0]]b[[P1]]c") == "abc"
    assert refs.plain(" 你 好\n[[P0]]世界 ") == "你好世界"


def test_similarity_metrics():
    assert refs.similarity("abc", "abc") == 1.0
    assert refs.similarity("", "") == 1.0
    assert refs.similarity("abc", "") == 0.0
    assert refs.similarity("abc", "abd", "ratio") > 0.5
    assert refs.similarity("abc", "abd", "edit") > 0.5
    assert refs.similarity("abcd", "abce", "dice") > 0.0


def test_canon():
    # NFKC 全半角 + 标点统一 + 折叠空白
    assert refs.canon("ＡＢＣ　１２３　\u201cx\u201d\u2026\u2014\u2014\uff5e") == 'ABC123"x"...-~'
    assert refs.canon("[[P0]]你 好[[P1]]") == "你好"


def test_locate_exact_and_fuzzy():
    ref = "今天天气很好。明天可能会下雨。后天放晴。"
    loc = refs.locate_fragment(ref, "明天可能会下雨")
    assert loc["score"] == 1.0
    loc2 = refs.locate_fragment(ref, "明天可能会下大雨哦")
    assert 0.5 < loc2["score"] < 1.0
    assert refs.locate_fragment(ref, "")["score"] == 0.0


def test_diff_text():
    d = refs.diff_text("今天很好", "今天不好")
    assert "今天" in d and ("-" in d and "+" in d or "?" in d)


def test_load_page_normalizes(tmp_cfg):
    _write_ref(tmp_cfg, "today/today1.html", "第一行\r\n\r\n\r\n\r\n第二行  ")
    t = refs.load_page(tmp_cfg, "today/today1.html")
    assert t is not None
    assert t.startswith("第一行")
    assert "\r" not in t
    assert t.splitlines()[-1].strip() == "第二行"
    assert refs.load_page(tmp_cfg, "today/nope.html") is None


def test_kind_weight(tmp_cfg):
    assert refs.kind_weight(tmp_cfg, "text") == 1.0
    assert refs.kind_weight(tmp_cfg, "head_title") == 0.0
    tmp_cfg.set({"head_title": 0.5, "text": 0.2}, "refs", "weights")
    assert refs.kind_weight(tmp_cfg, "head_title") == 0.5
    assert refs.kind_weight(tmp_cfg, "text") == 0.2


def test_check_page_restores_corrections(tmp_cfg):
    _write_ref(tmp_cfg, "p2.html", "今天天气很好。再见。")
    seg = Segment(1, "text", 0, 0, text="今日はいい天気です。", translation="今天天气糟透了。")
    assert len(refs.check_page(tmp_cfg, "p2.html", [seg])) == 1
    corr = {"1": [{"status": "active", "ref_err": "今天天气很好。",
                   "corrected": "今天天气糟透了。"}]}
    # 归属本段的纠错 → 还原后与参考一致 → 不判不一致
    assert refs.check_page(tmp_cfg, "p2.html", [seg], corrections=corr) == []


def test_trailing_orphan(tmp_cfg):
    _write_ref(tmp_cfg, "p4.html", "正文第一句。\n正文第二句。\n【彩蛋】\n额外地址说明。")
    segs = [
        Segment(1, "text", 0, 0, text="a", translation="正文第一句。"),
        Segment(2, "text", 0, 0, text="b", translation="正文第二句。"),
    ]
    tail = refs.trailing_orphan(tmp_cfg, "p4.html", segs)
    assert tail.startswith("【彩蛋】") and "额外地址说明" in tail
    assert "正文第二句" not in tail


def test_check_page_uncovered(tmp_cfg):
    # 参考未覆盖该段（无有意义公共片段）→ covered=False
    _write_ref(tmp_cfg, "p3.html", "只有这一段与价格无关的内容。")
    seg = Segment(1, "text", 0, 0, text="価格についてのお知らせです",
                  translation="关于价格的说明")
    issues = refs.check_page(tmp_cfg, "p3.html", [seg])
    assert len(issues) == 1 and issues[0]["covered"] is False


def test_check_page(tmp_cfg):
    _write_ref(tmp_cfg, "p.html", "你好世界。今天很好。再见。")
    segs = [
        Segment(1, "text", 0, 0, text="你好世界。", translation="你好世界。"),
        Segment(2, "text", 0, 0, text="今天很好。", translation="今天糟糕透了。"),
        Segment(3, "text", 0, 0, text="短。", translation="短。"),  # < min_seg_chars
        Segment(4, "head_title", 0, 0, text="标题", translation="标题"),
    ]
    issues = refs.check_page(tmp_cfg, "p.html", segs)
    sids = {i["sid"] for i in issues}
    assert "2" in sids           # 明显不一致
    assert "1" not in sids       # 一致
    assert "3" not in sids       # 过短跳过
    assert "4" not in sids       # 标题权重 0
    assert all(i["fragment"] for i in issues)


def test_corrections_sidecar(tmp_cfg):
    path = refs.corrections_path(tmp_cfg)
    assert path.endswith(os.path.join("work", "ref_corrections.jsonl"))
    n = refs.record_corrections(
        tmp_cfg, "p.html", "2",
        [{"ref_err": "大自然", "corrected": "榉木大厅", "reason": "专名"},
         {"ref_err": "", "corrected": "x"}],  # 无效，跳过
        op_id="op1",
    )
    assert n == 1
    act = refs.active_corrections(tmp_cfg, "p.html")
    assert len(act) == 1 and act[0]["corrected"] == "榉木大厅"
    # supersede 后不再 active，但保留在文件中
    assert refs.supersede_segment(tmp_cfg, "p.html", "2") == 1
    assert refs.active_corrections(tmp_cfg, "p.html") == []
    from booktr import util
    all_rec = util.read_jsonl(path)
    assert len(all_rec) == 1 and all_rec[0]["status"] == "superseded"


def test_inline_annotation():
    assert refs.inline_annotation("榉木大厅", "大自然") == "榉木大厅（原译文有误：大自然）"


def test_extract_ref_text():
    from booktr.pipeline import _extract_ref_text
    raw = ("●第1号\n"
           "冬休みの晴れた午後、とりとめのない話。\n"
           "寒假晴朗的午后。\n"
           "------\n"
           "你好。\n")
    out = _extract_ref_text(raw)
    assert "寒假晴朗的午后。" in out
    assert "你好。" in out
    assert "冬休み" not in out
    assert "●第1号" not in out
