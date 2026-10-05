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


# 问题根因分类（关键词启发式，A→G 顺序取首个命中）
CATEGORY_KEYWORDS = [
    ("A", "标点/全半角/特殊符号",
     ["全角", "半角", "省略号", "破折号", "标点", "书名号", "引号", "连字符",
      "中点", "符号", "问号", "句号"]),
    ("B", "术语/专名未译或保留原形",
     ["未译出", "仅保留", "保留原形", "保留原文", "未按词汇表", "未译",
      "保留日文", "片假名原形", "不应译为", "保留英文", "原样保留"]),
    ("C", "用词/译名前后不一致",
     ["不一致", "前后", "同一", "混淆", "撞车", "不统一", "混排"]),
    ("D", "措辞/语气/翻译腔",
     ["生硬", "翻译腔", "不自然", "累赘", "重复", "搭配", "语气", "口吻",
      "书面", "生造", "拗口", "别扭", "习惯", "不贴切", "不妥"]),
    ("E", "漏译/语义偏移/增译",
     ["漏译", "误译", "语义", "偏移", "丢失", "未体现", "增补", "多余",
      "窄化", "不准确", "误解", "增译", "添加", "偏差", "错误"]),
    ("F", "术语/专名选词不当(非未译)", ["词汇表", "术语", "专名"]),
    ("G", "星期/日期/数字", ["星期", "曜日", "周二", "周一", "周三", "周"]),
]


def classify_reason(reason: str) -> str:
    """把问题 reason 归入 A–G 的首个命中的类别字母（未命中返回 "?"）。"""
    r = reason or ""
    for letter, _label, kws in CATEGORY_KEYWORDS:
        if any(k in r for k in kws):
            return letter
    return "?"


def emit_category(data_dir, category, out, qa2_ts):
    """从 QA2 的 open 条目中筛出指定类别，输出 id 白名单 + 可读清单。只读实例。"""
    want_all = not category or category.upper() in ("ALL", "*")
    want = set((category or "").upper())
    q = _load_qa_queue(data_dir)
    reports = _list_qa_reports(data_dir)
    ts, path, rep = _pick_qa2(reports, qa2_ts)
    issues2 = _issues_by_page(rep)

    from collections import defaultdict, Counter
    open_items = [it for it in q if it.get("status") == "open"]
    # open 条目须能在 QA2 报告里找到对应（按 id 去重已保证唯一）
    picked, by_cat = [], Counter()
    for it in open_items:
        letter = classify_reason(it.get("reason", ""))
        if want_all or letter in want:
            picked.append((it, letter))
            by_cat[letter] += 1

    os.makedirs(out, exist_ok=True)
    ids_path = os.path.join(out, "qa2_DE_ids.json")
    with open(ids_path, "w", encoding="utf-8") as f:
        json.dump([it.get("id") for it, _ in picked], f, ensure_ascii=False, indent=2)
    lines = [f"# QA2 open 条目分类筛选（category={'ALL' if want_all else category}）",
             f"QA2 基线: {ts} | open 总数 {len(open_items)} | 命中 {len(picked)}",
             "", "## 类别分布（全部 open）"]
    allcat = Counter(classify_reason(it.get("reason", "")) for it in open_items)
    labels = {l: name for l, name, _ in CATEGORY_KEYWORDS}
    for l in ["A", "B", "C", "D", "E", "F", "G", "?"]:
        if allcat.get(l):
            lines.append(f"    {l} {labels.get(l, '其它')}: {allcat[l]}")
    lines.append(f"\n## 命中条目（{len(picked)}）")
    for it, letter in picked:
        lines.append(f"  [{letter}] {it.get('page')} 段{it.get('segments')} "
                     f"[{it.get('severity')}] {it.get('id')}")
        if it.get("reason"):
            lines.append(f"      原因: {(it.get('reason') or '')[:100]}")
        if it.get("src_quote"):
            lines.append(f"      相关原文: {(it.get('src_quote') or '')[:80]}")
        if it.get("dst_quote"):
            lines.append(f"      现有译文: {(it.get('dst_quote') or '')[:80]}")
        if it.get("suggestion"):
            lines.append(f"      推荐译文: {(it.get('suggestion') or '')[:160]}")
    txt = "\n".join(lines)
    rep_path = os.path.join(out, "qa2_DE_report.txt")
    open(rep_path, "w", encoding="utf-8").write(txt)
    print(txt)
    print(f"\nid 白名单: {ids_path}（{len(picked)} 条）")
    print(f"可读清单: {rep_path}")
    return ids_path


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

    def _iss_line(pg, iss, extra=""):
        line = f"    {pg} 段{iss.get('segments')}{extra} | {(iss.get('reason') or '')[:80]}"
        sug = (iss.get("suggestion") or "").strip()
        if sug:
            line += f"\n        └ 推荐: {sug[:160]}"
        return line

    L.append("\n## 4. 残存 high（未复现，%d 条）" % len(high_norec))
    for pg, iss, on_touched in high_norec:
        L.append(_iss_line(pg, iss, extra=f" touched={on_touched}"))
    L.append("\n## 5. 复现的 high（%d 条）" % len(high_rec))
    for pg, iss in high_rec:
        L.append(_iss_line(pg, iss))

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


def variant_analysis(vdir: str, out_dir: str, data_dir: str | None = None):
    """汇总 probe.py variant 运行结果：问题数/类别、跨次稳定性、成本、判官分布。

    ``data_dir`` 给出时，统计 qa 变体问题中"落在曾被 apply 的段"的比例（振荡敏感度）。
    """
    import glob
    from collections import defaultdict, Counter

    # 曾被 apply 的段（来自实例队列，可选）
    applied_segs = defaultdict(set)
    if data_dir:
        q = _load_qa_queue(data_dir)
        for it in q:
            if it.get("status") == "applied":
                for s in it.get("segments") or []:
                    applied_segs[it.get("page", "")].add(str(s))

    files = sorted(glob.glob(os.path.join(vdir, "variant_runs", "*.json")))
    by: dict[tuple, list] = defaultdict(list)
    for f in files:
        j = _read_json_opt(f, None)
        if not j:
            continue
        by[(j.get("end"), j.get("variant"))].append(j)

    lines = ["# 变体矩阵实验分析", f"\n目录: {vdir}"]
    cost = ["\n## 成本/耗时/过思考", "（单次完成 >100k tok 或 >300s 标 ⚠ 过思考）"]
    for (end, vkey) in sorted(by, key=lambda x: (x[0], x[1])):
        allrecs = sorted(by[(end, vkey)], key=lambda r: r.get("run", 0))
        recs = [r for r in allrecs if (r.get("llm") or {}).get("calls", 0) > 0]
        bad = len(allrecs) - len(recs)

        def _qa_hitset(rec):
            s = set()
            for pg, arr in (rec.get("qa") or {}).items():
                for iss in arr:
                    s.add((pg, tuple(map(str, iss.get("segments") or []))))
            return s

        if end == "qa":
            catc = Counter(); sevc = Counter(); on_applied = 0; tot = 0
            for rec in recs:
                for pg, arr in (rec.get("qa") or {}).items():
                    for iss in arr:
                        tot += 1
                        sevc[iss.get("severity")] += 1
                        catc[classify_reason(iss.get("reason", ""))] += 1
                        if data_dir and set(map(str, iss.get("segments") or [])) \
                                & applied_segs.get(pg, set()):
                            on_applied += 1
            n = max(len(recs), 1)
            sets = [_qa_hitset(r) for r in recs]
            jac = _jaccard(sets)
            union = set().union(*sets) if sets else set()
            inter = set.intersection(*sets) if sets else set()
            lines.append(f"\n## qa {vkey}（有效 {len(recs)} 次"
                         + (f"，坏次 {bad}" if bad else "") + "）"
                         f"\n  平均问题数/次 {tot / n:.1f} | 严重度 {dict(sevc)}"
                         f" | 类别 {dict(catc)}")
            if data_dir:
                lines.append(f"  落在曾 apply 段的比例 {on_applied / max(tot, 1):.0%}"
                             f"（振荡敏感度，越低越好）")
            lines.append(f"  跨次稳定性(段命中 Jaccard) {jac:.2f}"
                         f"（并 {len(union)} / 交 {len(inter)}；越高越稳定）")
        elif end == "judge":
            vd = Counter()
            for rec in recs:
                for pg, d in (rec.get("judge") or {}).items():
                    for v in (d.get("verdicts") or []):
                        vd[v.get("verdict")] += 1
            lines.append(f"\n## judge {vkey}（有效 {len(recs)} 次"
                         + (f"，坏次 {bad}" if bad else "") + "）"
                         f"\n  裁决分布 {dict(vd)}")
        else:  # translate
            sims = _translate_stability(recs)
            lines.append(f"\n## translate {vkey}（有效 {len(recs)} 次"
                         + (f"，坏次 {bad}" if bad else "") + "）"
                         f"\n  自相似(同风格跨次) {sims:.2f}（越高越稳定）")

        # 成本 + 过思考标记
        for r in allrecs:
            llm = r.get("llm", {}) or {}
            flag = ""
            if llm.get("calls", 0) > 0 and (llm.get("completion_tokens", 0) > 100000
                                            or r.get("duration_s", 0) > 300):
                flag = " ⚠过思考"
            cost.append(f"- {end} {vkey} run{r.get('run')}: "
                        f"p={llm.get('prompt_tokens', 0)} c={llm.get('completion_tokens', 0)} "
                        f"calls={llm.get('calls', 0)} {r.get('duration_s', 0):.0f}s"
                        + (" [FAILED]" if llm.get("calls", 0) == 0 else flag))

    txt = "\n".join(lines + cost)
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "variant_analysis.txt")
    open(p, "w", encoding="utf-8").write(txt)
    print(txt)
    print("\n留存:", p)
    return txt


def link_logs(run_dir: str, logs_dir: str, out_dir: str):
    """把 `_out/logs/` 的调用日志关联到每个 (end, variant, run)。

    **优先按日志内的 ``run`` 字段精确关联**（新日志直接携带运行 id，无需猜测）；
    无 ``run`` 字段的旧日志回退到时间窗（`started_at/ended_at` 或文件 mtime ± duration）。
    输出 `log_index.json`：{run 文件 -> [日志文件名]}，供审阅完整多轮对话历史。
    """
    import glob

    logs = []  # (name, mtime, run_tag)
    for f in glob.glob(os.path.join(logs_dir, "*.json")):
        try:
            j = _read_json_opt(f, {}) or {}
            logs.append((os.path.basename(f), os.path.getmtime(f), j.get("run", "")))
        except OSError:
            pass
    logs.sort(key=lambda x: x[1])

    index = {}
    for rf in sorted(glob.glob(os.path.join(run_dir, "*.json"))):
        j = _read_json_opt(rf, None) or {}
        run_tag = j.get("run_tag", "")
        if run_tag:
            hit = [name for name, _, rt in logs if rt == run_tag]
            index[os.path.basename(rf)] = hit
            continue
        # 回退：时间窗（旧数据）
        dur = float(j.get("duration_s", 0) or 0)
        try:
            mtime = os.path.getmtime(rf)
        except OSError:
            continue
        end_t = mtime
        start_t = mtime - max(dur, 1.0)
        hit = [name for name, t, _ in logs if start_t <= t <= end_t + 2]
        index[os.path.basename(rf)] = hit

    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "log_index.json")
    open(p, "w", encoding="utf-8").write(
        json.dumps(index, ensure_ascii=False, indent=2))
    tot = sum(len(v) for v in index.values())
    print(f"关联 {len(index)} 个运行 → {tot} 条日志；留存: {p}")
    return index


def _log_pages_end(name: str) -> str:
    return name.split("_", 1)[0]


_VKEY_ORDER = {"V0": 0, "V1": 1, "V2": 2, "V3": 3, "V4": 4, "V5": 5}


def _vkey_order(vkey: str) -> int:
    return _VKEY_ORDER.get(vkey, 99)


MARK_POLICY = "审核政策（通用）"
MARK_HISTORY = "本页已发生的 QA 决策"
MARK_STYLE_FAITHFUL = "翻译风格：忠实优先"
MARK_STYLE_FLUENT = "翻译风格：流畅优先"


def _run_bucket(j: dict) -> str:
    """run 的"提示词等价桶"：

    - translate：按 style（standard/faithful/fluent）。
    - qa：按 policy/history（V0 / policy / policy+history）。
    - judge：判官提示词**不含**审核政策、且 style 未生效，仅 history 可辨
      （V0/V1/V2/V5 同桶 "plain"；V3/V4 同桶 "policy+history"）。
    """
    end = j.get("end", "")
    vcfg = j.get("vcfg") or {}
    if end == "translate":
        return j.get("style", "standard")
    if end == "judge":
        return "policy+history" if vcfg.get("history", False) else "plain"
    if not vcfg.get("policy", False):
        return "V0"
    return "policy+history" if vcfg.get("history", False) else "policy"


def _log_bucket(lg: dict) -> str:
    sys_ = lg.get("system", "")
    if lg["end"] == "translate":
        if MARK_STYLE_FAITHFUL in sys_:
            return "faithful"
        if MARK_STYLE_FLUENT in sys_:
            return "fluent"
        return "standard"
    if lg["end"] == "judge":
        # 判官 system 含 history 块（V3/V4）；其余不可辨 → 同桶 plain
        return "policy+history" if MARK_HISTORY in sys_ else "plain"
    if MARK_POLICY not in sys_:
        return "V0"
    return "policy+history" if MARK_HISTORY in lg.get("user", "") else "policy"


def _page_src_fingerprints(ref_data_dir: str, pages: list) -> dict:
    import glob as _g
    fp = {}
    seg_dir = os.path.join(ref_data_dir, "work", "segments") if ref_data_dir else ""
    for pg in pages:
        p = os.path.join(seg_dir, pg.replace("/", "__") + ".json")
        if not os.path.exists(p):
            continue
        d = _read_json_opt(p, {}) or {}
        segs = d.get("segments", d) if isinstance(d, dict) else d
        # 与 qa.run_qa 的 src_all 口径一致：所有已翻译段（不限 kind）
        txt = "\n".join(s.get("text", "") for s in segs if s.get("translation"))
        fp[pg] = _norm(txt)
    return fp


def rebind_logs(source_out: str, out_dir: str | None = None,
                ref_data_dir: str | None = None):
    """修复并构建 `log_index.json`：把 `_out/logs/` 的调用日志绑定到各 run。

    **绑定优先级（run 自标识优先，结构分段兜底）**：
    1. **精确**：日志携带 ``run`` 字段且能对应某 run 的 ``run_tag`` → 直接绑定（新日志，
       无需猜测）。
    2. **沿用旧索引**：`log_index.json` 已存在的绑定保留给**尚无精确绑定**的 run
       （旧日志无 ``run`` 字段，其绑定此前已按固定页序修正过）。
    3. **结构分段兜底**：仅对上述都未覆盖的 run + 未被认领的日志，按**固定页序**切分：
       同一 (end, bucket) 的 run 依固定页序执行，日志按 `ts` 排序后在"页回到首页"处切段。

    - 日志 page：judge/translate 由 `tag`；qa 由 `user` 的 |TEXT| 与页原文指纹匹配。
    - bucket：qa/judge 由 system/user 标记；translate 由 system 风格标记。
    输出 `log_index.json` + 审计摘要。**一条日志只绑一个 run**。
    """
    import glob
    from collections import defaultdict

    logs_dir = os.path.join(source_out, "logs")
    runs_dir = os.path.join(source_out, "variant_runs")
    out_dir = out_dir or source_out
    base_index = _read_json_opt(os.path.join(out_dir, "log_index.json"), {}) or {}

    # 1) 读取 run，构造各端页序 + 桶 + run_tag
    runs = []
    for rf in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        j = _read_json_opt(rf, None)
        if not isinstance(j, dict):
            continue
        runs.append({"file": os.path.basename(rf), "end": j.get("end", ""),
                     "variant": j.get("variant", ""),
                     "run": int(j.get("run", 0) or 0),
                     "pages": list(j.get("pages") or []),
                     "run_tag": j.get("run_tag", "") or "",
                     "bucket": _run_bucket(j)})
    runs.sort(key=lambda r: (r["end"], r["bucket"], _vkey_order(r["variant"]), r["run"]))
    qa_pages = sorted({pg for r in runs for pg in r["pages"]})
    fp = _page_src_fingerprints(ref_data_dir, qa_pages) if ref_data_dir else {}

    # 2) 读取日志并打标 end/page/bucket
    logs = []
    for f in glob.glob(os.path.join(logs_dir, "*.json")):
        j = _read_json_opt(f, {}) or {}
        lg = {"name": os.path.basename(f), "tag": j.get("tag", ""),
              "run": j.get("run", "") or "",
              "ts": j.get("ts", ""), "system": j.get("system", ""),
              "user": j.get("user", ""), "mtime": os.path.getmtime(f)}
        lg["end"] = _log_pages_end(lg["name"])
        if lg["tag"].startswith(("judge_", "translate_")):
            # tag 用 _ 表示 /（如 judge_today_today11.html → today/today11.html）
            suffix = lg["tag"].split("_", 1)[1]
            lg["page"] = next((pg for pg in qa_pages
                               if pg.replace("/", "_") == suffix), suffix.replace("_", "/"))
        else:
            lg["page"] = _resolve_qa_page(lg["user"], fp)
        lg["bucket"] = _log_bucket(lg)
        logs.append(lg)
    logs.sort(key=lambda x: x["ts"])

    index = defaultdict(list)
    seen = set()
    notes = []

    # 3a) 精确绑定：日志 run 字段 == run 的 run_tag
    run_by_tag = {r["run_tag"]: r["file"] for r in runs if r["run_tag"]}
    for lg in logs:
        f = run_by_tag.get(lg["run"])
        if f:
            index[f].append(lg["name"])
            seen.add(lg["name"])

    # 3b) 沿用旧索引：仅补给尚无精确绑定的 run，且排除已被认领的日志名
    for f, names in base_index.items():
        if index.get(f):
            continue
        kept = [n for n in names if n not in seen]
        if kept:
            index[f].extend(kept)
            seen.update(kept)

    # 3c) 结构分段兜底：仅处理"仍无绑定"的 run 与"未被认领"的日志
    leftover = [lg for lg in logs if lg["name"] not in seen
                and lg["end"] in ("qa", "judge", "translate")]
    groups = defaultdict(list)
    for lg in leftover:
        groups[(lg["end"], lg["bucket"])].append(lg)
    for (end, bucket), glist in groups.items():
        gruns = [r for r in runs if r["end"] == end and r["bucket"] == bucket
                 and not index.get(r["file"])]
        if not gruns:
            continue
        order = gruns[0]["pages"] or []
        pidx = {pg: i for i, pg in enumerate(order)}
        glist.sort(key=lambda x: x["ts"])
        # 页序"回退"即开新一轮（每轮 = 一个 run 依固定页序的产出；对同页多次调用不误切）
        cycles = [[]]
        prev = -1
        for lg in glist:
            i = pidx.get(lg["page"], -1)
            if i >= 0 and prev >= 0 and i < prev and cycles[-1]:
                cycles.append([])
            cycles[-1].append(lg)
            if i >= 0:
                prev = i
        # 去掉不完整的前导碎片（如冒烟测试的单页）
        cycles = [c for c in cycles if len(c) >= 2]
        if len(cycles) > len(gruns):
            # 重复运行（首批被重跑）：只保留最后 N 轮（对应最终覆盖的 JSON）
            notes.append(f"{end}/{bucket}: 轮次 {len(cycles)} > run 数 {len(gruns)}；"
                         f"取最后 {len(gruns)} 轮，其余 {sum(len(c) for c in cycles[:-len(gruns)])} 条标记未分配")
            cycles = cycles[-len(gruns):]
        elif len(cycles) < len(gruns):
            notes.append(f"{end}/{bucket}: 轮次 {len(cycles)} < run 数 {len(gruns)}")
        # 去重：每条日志只归本轮次组内**最早出现的 run**（确定性，跨轮边界页归前一 run）
        for i, gr in enumerate(gruns):
            if i >= len(cycles):
                continue
            new = []
            for lg in cycles[i]:
                if lg["name"] in seen:
                    continue
                seen.add(lg["name"])
                new.append(lg["name"])
            index[gr["file"]].extend(new)

    # 4) 审计：每个 run 绑定数 vs llm.calls
    audit = []
    for r in runs:
        j = _read_json_opt(os.path.join(runs_dir, r["file"]), {}) or {}
        want = (j.get("llm") or {}).get("calls", 0)
        got = len(index.get(r["file"], []))
        flag = "" if got == want else f"  ⚠ 期望{want} 实得{got}"
        audit.append(f"  {r['file']}: {got}{flag}")

    os.makedirs(out_dir, exist_ok=True)
    idx_path = os.path.join(out_dir, "log_index.json")
    open(idx_path, "w", encoding="utf-8").write(
        json.dumps({k: sorted(v) for k, v in index.items()}, ensure_ascii=False, indent=2))
    lines = ["# 日志重绑定审计",
             f"\n绑定 {len(seen)}/{len(logs)} 条日志到 {len(index)} 个 run"]
    lines += audit
    if notes:
        lines.append(f"\n## 切段异常（{len(notes)}）")
        lines += ["  " + n for n in notes[:60]]
    ap = os.path.join(out_dir, "rebind_audit.txt")
    open(ap, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n留存: {idx_path} / {ap}")
    return index


def _resolve_qa_page(user: str, fp: dict) -> str:
    """qa 日志 page：|TEXT| 与页原文指纹包含匹配（前缀），取最长匹配页。"""
    import re as _re
    if not fp:
        return ""
    m = _re.search(r"\|TEXT\|(.*?)\|DST\|", user or "", _re.S)
    if not m:
        return ""
    t = _norm(m.group(1))
    best, bl = "", 0
    for pg, f in fp.items():
        k = min(len(f), len(t), 80)
        if k and f[:k] == t[:k] and k > bl:
            best, bl = pg, k
    return best


def _rc_excerpt(text: str, head: int = 800, tail: int = 800) -> str:
    """reasoning 摘要：首 head + 省略 + 尾 tail。"""
    t = (text or "").strip()
    if len(t) <= head + tail + 20:
        return t
    return t[:head] + "\n…（中略）…\n" + t[-tail:]


def _fmt_issue(x: dict) -> list[str]:
    return [
        f"    - [{x.get('severity')}] 段{x.get('segments')}",
        f"      原因: {x.get('reason', '')}",
        f"      相关原文: {x.get('src_quote', '')}",
        f"      现有译文: {x.get('dst_quote', '')}",
        f"      建议: {x.get('suggestion', '')}",
    ]


def _block(text: str, indent: str = "    ") -> list[str]:
    return ["  " + indent + "```"] + \
           [indent + ln for ln in (text or "").splitlines()] + \
           ["  " + indent + "```"]


def _render_call(i: int, name: str, lg: dict, thinking: str,
                 include_system: bool = True) -> list[str]:
    """渲染一次调用：元信息 + system + user 全文 + response + thinking。"""
    dur = lg.get("duration_ms", 0) / 1000
    rl = lg.get("reasoning_len", 0) or 0
    out = [f"#### 调用 {i}　`{name}`　{dur:.0f}s  reasoning={rl}  "
           f"completion={lg.get('usage', {}).get('completion_tokens')}  "
           f"finish={lg.get('finish_reason')}"]
    if include_system and lg.get("system"):
        out.append("- **system（全文）**:")
        out.extend(_block(lg["system"]))
    if lg.get("messages"):
        out.append(f"- messages（{len(lg['messages'])} 条，完整多轮）:")
        for m in lg["messages"]:
            out.append(f"  - {m.get('role')}: {m.get('content', '')}")
    elif lg.get("user"):
        out.append("- user（全文）:")
        out.extend(_block(lg["user"]))
    if lg.get("response"):
        out.append("- response:")
        out.extend(_block(lg["response"]))
    if thinking != "none" and lg.get("reasoning"):
        body = lg["reasoning"] if thinking == "full" else _rc_excerpt(lg["reasoning"])
        out.append(f"- thinking（{'full' if thinking == 'full' else 'excerpt'}）:")
        out.extend(_block(body))
    for ab in lg.get("loop_aborts") or []:
        out.append(f"- **循环中止** period={ab.get('period')} repeats={ab.get('repeats')} "
                   f"@ {ab.get('fired_at_chars')}字符  {ab.get('elapsed_s')}s  "
                   f"finish={ab.get('finish_reason')}")
        out.append(f"    片段: {ab.get('fragment', '')[:120]}")
        if thinking == "full" and ab.get("reasoning"):
            out.append("    异常 reasoning 尾部:")
            out.extend(_block(ab["reasoning"][-2000:], indent="      "))
    for ab in lg.get("wall_aborts") or []:
        out.append(f"- **调用超时中止** {ab.get('elapsed_s')}s > {ab.get('limit_s')}s  "
                   f"finish={ab.get('finish_reason')}")
        if thinking == "full" and ab.get("reasoning"):
            out.append("    异常 reasoning 尾部:")
            out.extend(_block(ab["reasoning"][-2000:], indent="      "))
    out.append("")
    return out


def transcripts(source_out: str, end, variants, runs, report_dir: str,
                thinking: str = "full", include_system: bool = True):
    """把变体运行的对话日志渲染成可读 Markdown（translate/qa/judge）。需 log_index.json。

    include_system=True（默认）时，逐调用附**完整 system**（及 user/messages 全文）。
    """
    idx_path = os.path.join(source_out, "log_index.json")
    if not os.path.exists(idx_path):
        link_logs(os.path.join(source_out, "variant_runs"),
                  os.path.join(source_out, "logs"), source_out)
    index = _read_json_opt(idx_path, {}) or {}
    logs = os.path.join(source_out, "logs")

    ends = [end] if end else ["translate", "qa", "judge"]
    md_out_dir = report_dir or source_out
    os.makedirs(md_out_dir, exist_ok=True)
    for e in ends:
        lines = [f"# 变体对话记录（{e}）", f"\nthinking={thinking}"]
        for key in sorted(index):
            parts = key[:-5].split("_")
            if parts[0] != e:
                continue
            vkey, run = parts[1], parts[2].replace("run", "")
            if variants and vkey not in variants:
                continue
            if runs and run not in runs:
                continue
            j = _read_json_opt(os.path.join(source_out, "variant_runs", key), {}) or {}
            lines.append(f"\n## {vkey} run{run}  "
                         f"（{j.get('duration_s')}s, {len(index[key])} 次调用）")

            # 结构化摘要
            if e == "qa":
                for pg, issues in (j.get("qa") or {}).items():
                    lines.append(f"\n### {pg}  ({len(issues)} 问题)")
                    for x in issues:
                        lines.extend(_fmt_issue(x))
            elif e == "judge":
                for pg, d in (j.get("judge") or {}).items():
                    verd = d.get("verdicts") or []
                    lines.append(f"\n### {pg}  ({len(verd)} 条裁决)")
                    for v in verd:
                        lines.append(f"    [{v.get('severity')}] 段{v.get('segments')} "
                                     f"→ **{v.get('verdict')}**")
                        lines.append(f"      理由: {v.get('reason', '')}")
                        if v.get("suggestion"):
                            lines.append(f"      建议: {v.get('suggestion', '')}")
            else:  # translate
                for pg, snap in (j.get("snapshot") or {}).items():
                    lines.append(f"\n### {pg}  ({len(snap)} 段)")
                    for sid in sorted(snap, key=lambda x: (len(x), x)):
                        lines.append(f"    - 段{sid}: {(snap[sid] or '')[:120]}")

            # 逐调用全文
            lines.append("\n### 逐调用记录")
            for i, name in enumerate(index[key], 1):
                lg = _read_json_opt(os.path.join(logs, name), {}) or {}
                lines.extend(_render_call(i, name, lg, thinking,
                                          include_system=include_system))
        p = os.path.join(md_out_dir, f"transcripts_{e}.md")
        open(p, "w", encoding="utf-8").write("\n".join(lines))
        print(f"留存: {p}（{len(lines)} 行）")


def embed_thinking(source_out: str):
    """把 `_out/logs` 的完整调用日志回填进 `variant_runs/*.json` 的 `calls` 字段。

    每条 = 磁盘日志的完整 entry（含 reasoning/response/system/user/messages 全文），
    使运行 JSON 自包含，无需再次调用模型。
    """
    idx_path = os.path.join(source_out, "log_index.json")
    if not os.path.exists(idx_path):
        link_logs(os.path.join(source_out, "variant_runs"),
                  os.path.join(source_out, "logs"), source_out)
    index = _read_json_opt(idx_path, {}) or {}
    logs = os.path.join(source_out, "logs")
    n = 0
    for key, names in index.items():
        rf = os.path.join(source_out, "variant_runs", key)
        j = _read_json_opt(rf, None)
        if not isinstance(j, dict):
            continue
        calls = []
        for nm in names:
            lg = _read_json_opt(os.path.join(logs, nm), {})
            if lg:
                calls.append(lg)
        j["calls"] = calls
        util_write = open(rf, "w", encoding="utf-8")
        util_write.write(json.dumps(j, ensure_ascii=False, indent=2))
        util_write.close()
        n += 1
    print(f"已回填 thinking 到 {n} 个运行 JSON（calls 字段）")


def _jaccard(sets: list) -> float:
    if len(sets) < 2:
        return 1.0
    num = den = 0.0
    for i in range(len(sets)):
        for k in range(i + 1, len(sets)):
            a, b = sets[i], sets[k]
            u = len(a | b)
            num += len(a & b) / u if u else 1.0
            den += 1
    return num / den if den else 1.0


def _translate_stability(recs: list) -> float:
    """同风格跨次译文自相似：各页各次快照两两相似度平均。"""
    if len(recs) < 2:
        return 1.0
    pages = set()
    for r in recs:
        pages |= set((r.get("snapshot") or {}).keys())
    vals = []
    for pg in pages:
        texts = []
        for r in recs:
            snap = (r.get("snapshot") or {}).get(pg, {})
            texts.append(" ".join(snap.get(k, "") for k in sorted(snap, key=lambda x: (len(x), x))))
        for i in range(len(texts)):
            for k in range(i + 1, len(texts)):
                vals.append(_similar(texts[i], texts[k]))
    return round(sum(vals) / len(vals), 3) if vals else 1.0


def main():
    ap = argparse.ArgumentParser(description="qa-auto 结果分析 / 判官漂移审计")
    ap.add_argument("--result", default=None, help="e2e_result.json 路径")
    ap.add_argument("--audit", default=None, help="审计模式：指向实例 data_dir")
    ap.add_argument("--prod", default=None, help="生产评估模式：指向实例 data_dir（只读）")
    ap.add_argument("--variant", default=None,
                    help="变体实验目录（含 variant_runs/），汇总逐变体指标")
    ap.add_argument("--ref-data-dir", default=None,
                    help="变体分析参照的实例 data_dir（用于振荡敏感度）")
    ap.add_argument("--link-logs", default=None,
                    help="把 <out>/logs 的调用日志按时间窗关联到 <out>/variant_runs/*")
    ap.add_argument("--transcripts", default=None,
                    help="把 <out> 的变体对话渲染为可读 Markdown（配合 --end/--tvar/--trun）")
    ap.add_argument("--end", default=None, choices=["qa", "judge", "translate"],
                    help="--transcripts 限定端（默认 qa+judge）")
    ap.add_argument("--tvar", nargs="*", default=None,
                    help="--transcripts 限定变体（如 V0 V4）")
    ap.add_argument("--trun", nargs="*", default=None,
                    help="--transcripts 限定运行号（如 1 2 3）")
    ap.add_argument("--report-dir", default=None,
                    help="--transcripts 输出目录（默认写回 <out>）")
    ap.add_argument("--thinking", default="full", choices=["full", "excerpt", "none"],
                    help="--transcripts 中 thinking 呈现粒度（默认 full）")
    ap.add_argument("--no-system", action="store_true",
                    help="--transcripts 不输出完整 system（默认输出完整拼合提示词）")
    ap.add_argument("--embed-thinking", default=None,
                    help="把 <out>/logs 完整调用日志回填进 <out>/variant_runs/*.json 的 calls 字段")
    ap.add_argument("--rebind-logs", default=None,
                    help="一次性内容签名重绑定：修正 <out>/log_index.json 的 run↔日志对应")
    ap.add_argument("--emit-category", default=None,
                    help="按类别筛选 QA2 open 条目，指向实例 data_dir；配合 --category")
    ap.add_argument("--category", default="DE",
                    help="类别字母（如 DE / A / ALL），默认 DE")
    ap.add_argument("--out", default=None, help="--emit-category 输出目录（默认 _out）")
    ap.add_argument("--qa2", default=None, help="指定 QA2 报告的 ts（默认取 checked 最多者）")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--threshold", type=float, default=0.6, help="问题级复现相似度阈值")
    args = ap.parse_args()

    if args.audit:
        audit_drift(args.audit, args.out_dir)
        return
    if args.emit_category:
        out = args.out or os.path.join(args.out_dir, "_out")
        emit_category(args.emit_category, args.category, out, args.qa2)
        return
    if args.link_logs:
        link_logs(os.path.join(args.link_logs, "variant_runs"),
                  os.path.join(args.link_logs, "logs"), args.out_dir)
        return
    if args.rebind_logs:
        rebind_logs(args.rebind_logs, None, args.ref_data_dir)
        return
    if args.embed_thinking:
        embed_thinking(args.embed_thinking)
        return
    if args.transcripts:
        transcripts(args.transcripts, args.end, set(args.tvar or []),
                    set(args.trun or []), args.report_dir or args.out_dir,
                    thinking=args.thinking, include_system=not args.no_system)
        return
    if args.variant:
        variant_analysis(args.variant, args.out_dir, args.ref_data_dir)
        return
    if args.prod:
        prod_analysis(args.prod, args.qa2, args.threshold, args.out_dir)
        return
    if not args.result:
        ap.error("需要 --result / --audit <data_dir> / --prod <data_dir> / "
                 "--emit-category <data_dir>")

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
