"""QA 滑窗覆盖实验分析：覆盖率 / 去重 / 成本。

读取 ``probe.py coverage`` 产出的 ``coverage_result.json``，按"同段 + 问题文本
相似度"把所有条件×多次运行的问题**聚类**（同一真实问题在不同条件/轮次视为同一
簇），再比较各条件的**覆盖簇数 / 单轮覆盖 / 独有问题 / Jaccard / 成本**。

用法：
    python tools/qa_slide_probe/analyze.py \
        --result tools/qa_slide_probe/_out/coverage_result.json
输出 stdout 与 ``_out/coverage_stats.txt``。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(TOOL_DIR, "_out")


# ── 匹配 / 聚类 ─────────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def same_issue(a: dict, b: dict, threshold: float = 0.6) -> bool:
    """两问题是否同一：段号有交集（任一为空则跳过该条件）+ 文本相似。"""
    A = set(str(x) for x in (a.get("segments") or []))
    B = set(str(x) for x in (b.get("segments") or []))
    if A and B and not (A & B):
        return False
    r = _sim(a.get("reason", ""), b.get("reason", ""))
    s = _sim(a.get("suggestion", ""), b.get("suggestion", ""))
    q = _sim(a.get("dst_quote", ""), b.get("dst_quote", ""))
    return max(r, s, q) >= threshold


def cluster_issues(items: list[dict], threshold: float = 0.6) -> list[int]:
    """并查集聚类；返回每项所属簇 id（0..k-1）。items 为 [{issue, ...}]。"""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if same_issue(items[i], items[j], threshold):
                union(i, j)
    roots: dict[int, int] = {}
    out = []
    for i in range(n):
        r = find(i)
        if r not in roots:
            roots[r] = len(roots)
        out.append(roots[r])
    return out


# ── 主分析 ─────────────────────────────────────────────────────────────────


def analyze(result: dict, threshold: float = 0.6) -> dict:
    pages = result.get("pages") or {}
    conditions = (result.get("meta") or {}).get("conditions") or []
    page_stats = {}
    agg = {c: {"runs": 0, "issues_sum": 0, "calls_sum": 0, "duration_sum": 0.0,
               "ptok_sum": 0, "ctok_sum": 0, "reason_sum": 0, "loops_sum": 0,
               "cluster_hits": set(), "perrun_clusters": [], "new_vs_whole": 0}
           for c in conditions}
    for rel, prec in pages.items():
        # 收集所有条件×轮次的问题，带来源标签
        tagged = []  # (cond, run, issue)
        for cond in conditions:
            for run in prec["conditions"].get(cond, {}).get("runs", []):
                for iss in run.get("llm_issues", []):
                    tagged.append((cond, run.get("run"), iss))
                # 成本
                a = agg[cond]
                a["runs"] += 1
                a["issues_sum"] += len(run.get("llm_issues", []))
                a["calls_sum"] += run.get("calls", 0)
                a["duration_sum"] += run.get("duration_s", 0) or 0
                a["ptok_sum"] += run.get("prompt_tokens", 0) or 0
                a["ctok_sum"] += run.get("completion_tokens", 0) or 0
                a["reason_sum"] += run.get("reasoning_chars", 0) or 0
                a["loops_sum"] += run.get("loops", 0) or 0
        if not tagged:
            page_stats[rel] = {"clusters": 0}
            continue
        cids = cluster_issues([t[2] for t in tagged], threshold)
        # 每簇被哪些条件/轮次命中
        total_clusters = max(cids) + 1
        cluster_conds: dict[int, set] = {}
        # 各条件的"整页有无命中"（用于 new vs whole）
        whole_touched = set()
        cond_touched = {}
        perrun = {c: {} for c in conditions}
        for (cond, run, iss), cid in zip(tagged, cids):
            cluster_conds.setdefault(cid, set()).add(cond)
            cond_touched.setdefault(cond, set()).add(cid)
            perrun[cond].setdefault(run, set()).add(cid)
            if cond == "whole":
                whole_touched.add(cid)
        for cond in conditions:
            agg[cond]["cluster_hits"] |= {(rel, c) for c in cond_touched.get(cond, set())}
            for run, s in perrun[cond].items():
                agg[cond]["perrun_clusters"].append(len(s))
        # window-only 簇（被任一 win* 命中，whole 从未命中）
        winc = [c for c in conditions if c.startswith("win")]
        win_touched = set()
        for c in winc:
            win_touched |= cond_touched.get(c, set())
        new_vs_whole = len(win_touched - whole_touched)
        for c in winc:
            agg[c]["new_vs_whole"] += len(cond_touched.get(c, set()) - whole_touched)
        page_stats[rel] = {
            "clusters": total_clusters,
            "whole_clusters": len(whole_touched),
            "win_new_vs_whole": new_vs_whole,
            "cluster_conds": {cid: sorted(v) for cid, v in cluster_conds.items()},
        }
    # 汇总各条件覆盖率（cluster 键为 (rel, cid)，跨页唯一）
    total_clusters = sum(ps.get("clusters", 0) for ps in page_stats.values())
    summary = {}
    for cond in conditions:
        a = agg[cond]
        runs = max(1, a["runs"])
        summary[cond] = {
            "runs": a["runs"],
            "calls_per_run": round(a["calls_sum"] / runs, 2),
            "issues_per_run": round(a["issues_sum"] / runs, 2),
            "duration_per_run_s": round(a["duration_sum"] / runs, 1),
            "prompt_tok_per_run": round(a["ptok_sum"] / runs, 0),
            "completion_tok_per_run": round(a["ctok_sum"] / runs, 0),
            "reasoning_per_run": round(a["reason_sum"] / runs, 0),
            "loops": a["loops_sum"],
            "perrun_clusters_median": (statistics.median(a["perrun_clusters"])
                                       if a["perrun_clusters"] else 0),
            "new_vs_whole": a["new_vs_whole"],
        }
    # 覆盖簇总数（所有条件合并）
    for cond in conditions:
        summary[cond]["cluster_coverage"] = (
            round(len(agg[cond]["cluster_hits"]) / total_clusters, 3)
            if total_clusters else 0.0)
    return {"total_clusters": total_clusters, "summary": summary,
            "pages": page_stats}


def fmt(result: dict, analysis: dict) -> str:
    m = result.get("meta") or {}
    lines = ["# QA 滑窗覆盖实验", ""]
    lines.append(f"模型: {m.get('model','')}  条件: {m.get('conditions')}  "
                 f"窗口: {m.get('windows')}  重叠: {m.get('overlap')}  "
                 f"每条件轮次: {m.get('runs')}")
    lines.append(f"总簇数: {analysis['total_clusters']}")
    lines.append("")
    hdr = ("条件", "轮/页", "调用/轮", "问题/轮", "簇覆盖", "独有(vs整页)",
           "整页单轮簇中位", "耗时/轮s", "prompt/轮", "compl/轮", "reason/轮", "loop")
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    for cond, s in analysis["summary"].items():
        lines.append("| " + " | ".join(str(x) for x in [
            cond, s["runs"], s["calls_per_run"], s["issues_per_run"],
            f"{s['cluster_coverage']:.0%}", s["new_vs_whole"],
            s["perrun_clusters_median"], s["duration_per_run_s"],
            int(s["prompt_tok_per_run"]), int(s["completion_tok_per_run"]),
            int(s["reasoning_per_run"]), s["loops"]]) + " |")
    lines.append("")
    lines.append("## 每页")
    for rel, ps in analysis["pages"].items():
        lines.append(f"- {rel}: 簇={ps.get('clusters')}  "
                     f"整页命中={ps.get('whole_clusters', 0)}  "
                     f"滑窗独有={ps.get('win_new_vs_whole', 0)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="QA 滑窗覆盖实验分析")
    ap.add_argument("--result", default=os.path.join(OUT_DIR, "coverage_result.json"))
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--threshold", type=float, default=0.6)
    args = ap.parse_args()
    result = json.load(open(args.result, encoding="utf-8"))
    analysis = analyze(result, args.threshold)
    text = fmt(result, analysis)
    print(text)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "coverage_stats.txt"), "w",
              encoding="utf-8") as f:
        f.write(text + "\n")
    # 附 JSON
    with open(os.path.join(args.out_dir, "coverage_stats.json"), "w",
              encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
