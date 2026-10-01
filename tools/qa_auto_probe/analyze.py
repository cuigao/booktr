"""qa-auto e2e 结果分析：问题级复现率 / 裁决分布 / diff 范围。

读取 ``probe.py e2e`` 产出的 e2e_result.json，按"同段 + 问题文本相似度"
判定 QA2 是否复现 QA1 的已裁问题（主指标），并汇总裁决分布与 diff 段数。

复现判定用**问题级**（而非仅"同段"）：仅同段会因"同段出现不同新问题"而误报
（见 `instance/report/qa_auto_probe_report.md` §5.4）。

用法：
    python tools/qa_auto_probe/analyze.py \\
        --result tools/qa_auto_probe/_out/e2e_result.json \\
        [--out-dir tools/qa_auto_probe/_out]
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def item_issue_match(it: dict, iss: dict, threshold: float = 0.6) -> tuple[bool, float]:
    """判定 QA2 的 iss 是否复现 QA1 的 it（问题级：同段 + 语义相似）。"""
    seg_it = set(str(x) for x in it.get("segments", []) or [])
    seg_is = set(str(x) for x in iss.get("segments", []) or [])
    if not (seg_it & seg_is):
        return False, 0.0
    r = _similar(it.get("reason", ""), iss.get("reason", ""))
    s = _similar(it.get("suggestion", ""), iss.get("suggestion", ""))
    q = _similar(it.get("dst_quote", ""), iss.get("dst_quote", ""))
    score = max(r, s, q)
    return score >= threshold, score


def main():
    ap = argparse.ArgumentParser(description="qa-auto e2e 结果分析")
    ap.add_argument("--result", required=True, help="e2e_result.json 路径")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--threshold", type=float, default=0.6, help="问题级复现相似度阈值")
    args = ap.parse_args()

    res = json.load(open(args.result, encoding="utf-8"))
    agg = {"adopted": 0, "adopted_recur": 0, "rejected": 0, "rejected_recur": 0,
           "open": 0, "pages": 0, "diff_segs": 0, "qa1": 0, "qa2": 0}
    lines = []
    for rel, d in res.items():
        items = d.get("items", [])
        verd = d.get("verdicts", [])
        iss2 = d.get("issues2", [])
        adopted = [it for it in items if it.get("status") == "applied"]
        rejected = [it for it in items if it.get("status") == "rejected"]
        opened = [it for it in items if it.get("status") == "open"]

        def recur(its):
            hits = []
            for it in its:
                best, bi = 0.0, None
                for iss in iss2:
                    ok, sc = item_issue_match(it, iss, args.threshold)
                    if ok and sc > best:
                        best, bi = sc, iss
                hits.append((it, bi, best))
            return hits

        ar, rr = recur(adopted), recur(rejected)
        agg["pages"] += 1
        agg["qa1"] += len(d.get("issues1", []))
        agg["qa2"] += len(iss2)
        agg["adopted"] += len(adopted)
        agg["rejected"] += len(rejected)
        agg["open"] += len(opened)
        agg["adopted_recur"] += sum(1 for _, bi, _ in ar if bi)
        agg["rejected_recur"] += sum(1 for _, bi, _ in rr if bi)
        agg["diff_segs"] += len(d.get("diff", {}))
        lines.append(f"\n{'=' * 72}\n{rel}  QA1={len(d.get('issues1', []))} "
                     f"QA2={len(iss2)} adopt={len(adopted)} reject={len(rejected)} "
                     f"open={len(opened)} applied_segs={d.get('applied')}")
        lines.append("  裁决:")
        for it, v in zip(items, verd):
            lines.append(f"    [{it.get('severity')}] 段{it.get('segments')} "
                         f"{it.get('status'):8} v={v.get('verdict')} | "
                         f"{v.get('reason', '')[:46]}")
        if ar:
            lines.append("  adopt 复现:")
            for it, bi, sc in ar:
                mark = f"REPEAT({sc:.2f})" if bi else "resolved"
                lines.append(f"    段{it.get('segments')} {mark}: {it.get('reason', '')[:50]}")
        if rr:
            lines.append("  reject 复现:")
            for it, bi, sc in rr:
                mark = f"REPEAT({sc:.2f})" if bi else "gone"
                lines.append(f"    段{it.get('segments')} {mark}: {it.get('reason', '')[:50]}")
        lines.append("  QA2 新问题:")
        for iss in iss2:
            lines.append(f"    段{iss.get('segments')} [{iss.get('severity')}] "
                         f"{iss.get('reason', '')[:56]}")
    out = [
        "# e2e 汇总",
        f"\n页数 {agg['pages']} | QA1 {agg['qa1']} -> QA2 {agg['qa2']}",
        f"采纳 {agg['adopted']} 条，其中复现 {agg['adopted_recur']} "
        f"(复现率 {agg['adopted_recur'] / max(agg['adopted'], 1):.0%})",
        f"拒绝 {agg['rejected']} 条，其中复现 {agg['rejected_recur']}",
        f"跳过 {agg['open']} 条 | apply 段数 {agg['diff_segs']}",
    ] + lines
    txt = "\n".join(out)
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "analysis.txt")
    open(path, "w", encoding="utf-8").write(txt)
    print(txt)
    print("\n留存:", path)


if __name__ == "__main__":
    main()
