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


def _lcs(a: str, b: str) -> int:
    a, b = a or "", b or ""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def audit_drift(data_dir: str, out_dir: str) -> list[str]:
    """审计某实例的判官日志，检测三类漂移信号（只读）。

    1. **重置态调用**：某页非首次调用却形态为 `[system, user]`（历史中断，
       即"重试复用 seed"bug 的特征）。
    2. **索引回显不符**：判官返回的 `index` ≠ 本次所问序号。
    3. **响应错位**：响应文本与同页其它条目 profile 的 LCS 明显高于被问条目。

    返回人类可读报告行（同时写 `out_dir/drift_audit.txt`）。
    """
    import glob
    import datetime
    from collections import defaultdict

    ld = os.path.join(data_dir, "work", "llm_logs")
    q = _read_json_opt(os.path.join(data_dir, "work", "qa_queue.json"), [])
    bypage: dict[str, list] = defaultdict(list)
    for it in q:
        bypage[it.get("page", "")].append(it)

    per_page: dict[str, list] = defaultdict(list)
    for f in glob.glob(os.path.join(ld, "judge_*.json")):
        base = os.path.basename(f)
        key = base[6:base.find("_2")] if "_2" in base else base
        try:
            e = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        per_page[key].append((os.path.getmtime(f), e, base))

    out = ["# 判官漂移审计", f"\n数据目录: {data_dir}"]
    n_calls = n_reset = n_idx = n_mis = 0
    for key, cs in sorted(per_page.items()):
        cs.sort(key=lambda x: x[0])
        for idx, (ts, e, base) in enumerate(cs):
            msgs = e.get("messages") or []
            seed = next((m["content"] for m in msgs if m["role"] == "user"
                         and "\u5f85\u88c1\u5b9a QA \u95ee\u9898" in m["content"]), "")
            items = _parse_seed_items(seed) if seed else []
            n_calls += 1
            roles = [m["role"] for m in msgs]
            t = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            if idx > 0 and roles[:2] == ["system", "user"] and len(roles) == 2:
                n_reset += 1
                out.append(f"[重置态] {key} {t} call#{idx + 1} roles={roles}")
            p = e.get("parsed") or {}
            lu = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
            m = re.search(r"\u73b0\u5728\u88c1\u51b3\u7b2c (\d+)/", lu)
            asked = int(m.group(1)) if m else (1 if roles[:2] == ["system", "user"] else None)
            ri = p.get("index")
            if asked and ri and ri != asked:
                n_idx += 1
                out.append(f"[索引不符] {key} {t} asked={asked} index={ri}")
            if asked and items and 0 < asked <= len(items):
                resp = (p.get("reason", "") + " " + p.get("suggestion", "")).strip()
                if resp:
                    prof = lambda it: " ".join(  # noqa: E731
                        [it.get("reason", ""), it.get("suggestion", ""),
                         it.get("src_quote", ""), it.get("dst_quote", "")])
                    scores = [_lcs(resp, prof(it)) for it in items]
                    best = max(range(len(scores)), key=lambda k: scores[k])
                    # 仅当"响应几乎完全不匹配被问条目、却强烈匹配其它条目"才判
                    # 错位；同页多条共享词汇（如人名「岡崎」）会造成误报，故要求
                    # 被问条目得分极低（<3）且与最佳条目差距显著（≥12）。
                    if best != asked - 1 and scores[asked - 1] < 3 \
                            and scores[best] >= 12 \
                            and scores[best] - scores[asked - 1] >= 12:
                        n_mis += 1
                        out.append(f"[响应错位] {key} {t} asked={asked} "
                                   f"best_match=item{best + 1} "
                                   f"({scores[asked - 1]}->{scores[best]})")
    out.insert(2, f"\n调用 {n_calls} | 重置态 {n_reset} | 索引不符 {n_idx} | 响应错位 {n_mis}")
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "drift_audit.txt")
    open(p, "w", encoding="utf-8").write("\n".join(out))
    print("\n".join(out))
    print("\n留存:", p)
    return out


def _read_json_opt(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def _load_qa_queue(data_dir):
    return _read_json_opt(os.path.join(data_dir, "work", "qa_queue.json"), []) or []


def _list_qa_reports(data_dir):
    """列出实例的全部 QA 报告：[(ts, path, obj)]。"""
    import glob
    d = os.path.join(data_dir, "work", "qa_reports")
    out = []
    for f in glob.glob(os.path.join(d, "*.json")):
        j = _read_json_opt(f, None)
        if j is None:
            continue
        ts = j.get("ts") or os.path.splitext(os.path.basename(f))[0][3:]
        out.append((ts, f, j))
    return out


def _pick_qa2(reports, ts=None):
    """选定用作 QA2 基线的一份报告（默认 version≥2 且 checked 最多者）。"""
    if ts:
        for rts, f, j in reports:
            if rts == ts:
                return rts, f, j
        raise SystemExit(f"未找到 qa2 ts={ts}")
    if not reports:
        raise SystemExit("该实例没有任何 QA 报告")
    cands = [r for r in reports if (r[2].get("version") or 1) >= 2]
    if not cands:
        cands = reports
    cands.sort(key=lambda r: (len(r[2].get("checked") or {}), os.path.getmtime(r[1])))
    return cands[-1]


def _issues_by_page(report):
    from collections import defaultdict
    by = defaultdict(list)
    for pg, items in (report.get("pages") or {}).items():
        by[pg].extend(items)
    return by


def _recur(its, issues2, threshold):
    """逐条在**同页 QA2** 中找"问题级"匹配；返回 [(item, matched_issue|None, score)]。"""
    hits = []
    for it in its:
        best, bi = 0.0, None
        for iss in issues2.get(it.get("page", ""), []):
            ok, sc = item_issue_match(it, iss, threshold)
            if ok and sc > best:
                best, bi = sc, iss
        hits.append((it, bi, best))
    return hits


def _diff_safety(data_dir):
    """apply 前/后相似度（取每段最后一次 qa-apply 相对其前驱版本）。"""
    import glob
    hd = os.path.join(data_dir, "work", "segment_history")
    rows, rewrites = [], []
    for f in glob.glob(os.path.join(hd, "*.json")):
        h = _read_json_opt(f, {})
        for sid, vers in (h.get("segments") or {}).items():
            idxs = [i for i, v in enumerate(vers) if v.get("op") == "qa-apply"]
            if not idxs or idxs[-1] == 0:
                continue
            i = idxs[-1]
            before = (vers[i - 1].get("cache") or {}).get("translation") or ""
            after = (vers[i].get("cache") or {}).get("translation") or ""
            if not before or not after:
                continue
            sc = round(_similar(before, after), 3)
            rows.append(sc)
            if sc < 0.4:
                rewrites.append((sc, os.path.basename(f), sid, before, after))
    rows.sort()
    n = len(rows)
    stats = {
        "n": n,
        "mean": round(sum(rows) / n, 3) if n else 0.0,
        "median": rows[n // 2] if n else 0.0,
        "min": rows[0] if n else 0.0,
        "p10": rows[n // 10] if n else 0.0,
        "rewrite_lt0.4": len(rewrites),
    }
    return stats, rewrites


def _load_runs(data_dir):
    import glob
    from collections import Counter
    agg = Counter()
    runs = []
    for f in sorted(glob.glob(os.path.join(data_dir, "work", "qa_auto_runs", "*.json"))):
        j = _read_json_opt(f, None)
        if not j:
            continue
        t = j.get("total", {})
        runs.append((os.path.basename(f), j.get("scope", ""), t))
        for k, v in t.items():
            agg[k] += v
    return runs, agg


def prod_analysis(data_dir: str, qa2_ts, threshold: float, out_dir: str):
    """生产实例评估：问题级复现率 / 新问题构成 / diff 安全 / 严重度分布。只读。"""
    from collections import Counter
    q = _load_qa_queue(data_dir)
    reports = _list_qa_reports(data_dir)
    ts, path, rep = _pick_qa2(reports, qa2_ts)
    issues2 = _issues_by_page(rep)
    glossary = _read_json_opt(os.path.join(data_dir, "work", "glossary.json"), [])
    runs, run_agg = _load_runs(data_dir)

    qa1 = [it for it in q if it.get("status") in ("applied", "rejected")]
    applied = [it for it in qa1 if it.get("status") == "applied"]
    rejected = [it for it in qa1 if it.get("status") == "rejected"]
    opened = [it for it in q if it.get("status") == "open"]

    def _src(it):
        s = it.get("source")
        return s if s else "qa(默认建议)"

    a_hits = _recur(applied, issues2, threshold)
    r_hits = _recur(rejected, issues2, threshold)
    a_rec = sum(1 for _, bi, _ in a_hits if bi)
    r_rec = sum(1 for _, bi, _ in r_hits if bi)

    by_src = Counter()
    by_src_rec = Counter()
    for it, bi, _ in a_hits:
        by_src[_src(it)] += 1
        if bi:
            by_src_rec[_src(it)] += 1

    # 阈值敏感性
    sens = {}
    for th in (0.5, 0.6, 0.7):
        sens[th] = (sum(1 for _, bi, _ in _recur(applied, issues2, th) if bi),
                    sum(1 for _, bi, _ in _recur(rejected, issues2, th) if bi))

    # QA2 问题三分类
    touched = {}
    for it in applied:
        touched.setdefault(it.get("page", ""), set()).update(
            str(x) for x in it.get("segments") or [])
    cat = Counter()
    rec_sev = Counter()
    high_norec, high_rec = [], []
    for pg, arr in issues2.items():
        for iss in arr:
            segs = set(str(x) for x in iss.get("segments") or [])
            hit = any(item_issue_match(it, iss, threshold)[0]
                      for it in qa1 if it.get("page") == pg)
            if hit:
                cat["recur(已裁问题)"] += 1
                rec_sev[iss.get("severity")] += 1
                if iss.get("severity") == "high":
                    high_rec.append((pg, iss))
            elif segs & touched.get(pg, set()):
                cat["new(落在已改段)"] += 1
            else:
                cat["new(他处)"] += 1
            if iss.get("severity") == "high" and not hit:
                high_norec.append((pg, iss, bool(segs & touched.get(pg, set()))))

    qa2_sev = Counter(i.get("severity") for arr in issues2.values() for i in arr)
    qa1_sev = Counter(it.get("severity") for it in qa1)

    diff_stats, rewrites = _diff_safety(data_dir)

    L = ["# qa-auto 生产效果评估（--prod 只读）", f"\n数据目录: {data_dir}"]
    L.append(f"QA2 基线: {ts}  ({os.path.basename(path)})  "
             f"version={rep.get('version')} scope={rep.get('scope')} "
             f"checked={len(rep.get('checked') or {})} pages={len(issues2)}")
    L.append(f"词表: {len(glossary)} 条（旧口径） | 队列: 总 {len(q)} "
             f"(applied {len(applied)} / rejected {len(rejected)} / open {len(opened)})")
    L.append("\n## 1. 主指标｜问题级复现率（同段 ∩ 相似度 ≥ %.2f）（阈值敏感性见表）" % threshold)
    L.append(f"applied {len(applied)} 条 → 复现 {a_rec} ({a_rec / max(len(applied), 1):.0%}) "
             f"⇒ **修复 {len(applied) - a_rec} ({(len(applied) - a_rec) / max(len(applied), 1):.0%})**")
    L.append(f"rejected {len(rejected)} 条 → 复现 {r_rec} ({r_rec / max(len(rejected), 1):.0%})")
    L.append("  按来源：")
    for s in sorted(by_src, key=lambda x: -by_src[x]):
        L.append(f"    {s}: {by_src[s]} 条，复现 {by_src_rec[s]} "
                 f"({by_src_rec[s] / max(by_src[s], 1):.0%})")
    L.append("  阈值敏感性（阈值: applied复现 / rejected复现）：")
    for th in (0.5, 0.6, 0.7):
        L.append(f"    {th:.2f}: {sens[th][0]} / {sens[th][1]}")

    L.append("\n## 2. QA2 问题构成")
    L.append(f"总 {sum(cat.values())} | " + " | ".join(f"{k} {v}" for k, v in cat.items()))
    L.append(f"复现问题严重度: {dict(rec_sev)} | QA2 全量严重度: {dict(qa2_sev)} "
             f"| QA1 已裁严重度: {dict(qa1_sev)}")

    L.append("\n## 3. 客观指标｜diff 安全（apply 前/后相似度）")
    L.append(f"样本 {diff_stats['n']} 段 | 均值 {diff_stats['mean']} | 中位 {diff_stats['median']} "
             f"| p10 {diff_stats['p10']} | 最小 {diff_stats['min']} "
             f"| 相似度<0.4(疑整段重写) {diff_stats['rewrite_lt0.4']}")
    for sc, f, sid, b, a in rewrites[:12]:
        L.append(f"    [{sc}] {f} 段{sid}")
        L.append(f"        before: {b[:70]}")
        L.append(f"        after : {a[:70]}")

    L.append("\n## 4. 残存 high（未复现，%d 条）" % len(high_norec))
    for pg, iss, on_touched in high_norec:
        L.append(f"    {pg} 段{iss.get('segments')} touched={on_touched} | "
                 f"{(iss.get('reason') or '')[:80]}")
    L.append("\n## 5. 复现的 high（%d 条）" % len(high_rec))
    for pg, iss in high_rec:
        L.append(f"    {pg} 段{iss.get('segments')} | {(iss.get('reason') or '')[:80]}")

    L.append("\n## 6. qa-auto 运行汇总")
    for name, scope, t in runs:
        L.append(f"    {name} [{scope}] {t}")

    txt = "\n".join(L)
    o = os.path.join(out_dir, "_out")
    os.makedirs(o, exist_ok=True)
    p = os.path.join(o, "prod_analysis.txt")
    open(p, "w", encoding="utf-8").write(txt)
    print(txt)
    print("\n留存:", p)
    return txt


def _items_from_page(bypage, key):
    for pg, its in bypage.items():
        if pg.replace("/", "_").split(".")[0] == key.split(".")[0]:
            return its
    return []


def _parse_seed_items(seed: str) -> list[dict]:
    """从 seed user 文本解析被判定的条目列表（含 reason/src/dst/suggestion）。

    以 `[i] 严重度=... 位置=...` 分块，逐块抓取字段，供相关性比对使用
    （避免用整页队列顺序导致索引用错位）。
    """
    items = []
    blocks = re.split(r"\n(?=\[\d+\] \u4e25\u91cd\u5ea6=)", seed or "")
    for b in blocks:
        if not re.match(r"\s*\[\d+\] \u4e25\u91cd\u5ea6=", b):
            continue
        def grab(label):
            m = re.search(label + r"[:\uff1a]\s*(.*)", b)
            return m.group(1).strip() if m else ""
        items.append({
            "reason": grab("\u539f\u56e0"),
            "src_quote": grab("\u76f8\u5173\u539f\u6587"),
            "dst_quote": grab("\u73b0\u6709\u8bd1\u6587\u95ee\u9898"),
            "suggestion": grab("\u5efa\u8bae"),
        })
    return items


def main():
    ap = argparse.ArgumentParser(description="qa-auto 结果分析 / 判官漂移审计")
    ap.add_argument("--result", default=None, help="e2e_result.json 路径")
    ap.add_argument("--audit", default=None, help="审计模式：指向实例 data_dir")
    ap.add_argument("--prod", default=None, help="生产评估模式：指向实例 data_dir（只读）")
    ap.add_argument("--qa2", default=None, help="指定 QA2 报告的 ts（默认取 checked 最多者）")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--threshold", type=float, default=0.6, help="问题级复现相似度阈值")
    args = ap.parse_args()

    if args.audit:
        audit_drift(args.audit, args.out_dir)
        return
    if args.prod:
        prod_analysis(args.prod, args.qa2, args.threshold, args.out_dir)
        return
    if not args.result:
        ap.error("需要 --result <e2e_result.json> / --audit <data_dir> / --prod <data_dir>")

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
