"""译者注提示词探针（annotate prompt probe）。

在真实实例（只读）或沙箱副本上，对若干候选译者注提示词做实机 LLM 测试，
自动统计"结构可锚定性"指标（引文是否逐字命中、是否唯一、长度、密度等），
并输出逐页并排结果供人工审核语义质量。

设计约束（与展示层一致）：
  - 正文只加极小角标 `<sup>`，故每条注必须携带一个**逐字连续**的
    `dst_quote`（译文片段）作为锚点，`src_quote` 为回退锚点。
  - 段缓存 `work/segments/*.json` 的 `text` 为**源**文本、`translation` 为译文；
    本探针直接用这套分段（展示层对 out 重切，段号一致）。

用法（仓库根 `src/` 或任意 cwd，脚本自定位）：
    python tools/annotate_probe/probe.py --data-dir <data_dir> \\
        --pages today/today11.html today/today12.html \\
        --variants V1 V2 V3 --runs 3

结果写入 <tool_dir>/_out/（已 gitignore）：
  - 每条：<variant>__<page-key>__run<r>.json（含 system/user/response/parsed/metrics）
  - 汇总：analysis.json / analysis.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, SRC)

from booktr.config import load_config  # noqa: E402
from booktr import glossary as gl_mod  # noqa: E402
from booktr import llm as llm_mod  # noqa: E402
from booktr import prompts  # noqa: E402
from booktr import segments as seg_mod  # noqa: E402
from booktr import util  # noqa: E402

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(TOOL_DIR, "_out")

_PH = re.compile(r"\[\[P\d+\]\]")


def _strip_ph(text: str) -> str:
    return util.normalize_ws(_PH.sub("", text or ""))


def _page_segments(cfg, rel: str) -> list[dict]:
    """取页面的 text 段（源 + 译文），保留 [[Px]]。"""
    out = []
    for s in seg_mod.segments_for_page(cfg, rel):
        if getattr(s, "kind", "") != "text":
            continue
        out.append({"id": s.id, "src": s.text or "",
                    "dst": s.translation or ""})
    return out


def _summaries(cfg) -> dict:
    result = {}
    sdir = cfg.get("summaries", "dir", default="")
    if not os.path.isdir(sdir):
        return result
    for fn in os.listdir(sdir):
        if not fn.endswith(".json"):
            continue
        rel_key = fn[:-5].replace("__", "/")
        data = util.read_json(os.path.join(sdir, fn), {})
        if data.get("summary"):
            result[rel_key] = data["summary"]
    return result


# --------------------------------------------------------------------------
# Variant system prompts (V1/V3 live here; V2 is the frozen library prompt)
# --------------------------------------------------------------------------
def _v1_minimal(cfg, glossary, max_notes) -> str:
    tgt = prompts.lang_name(cfg.get("lang", "target", default="zh-Hans"))
    return (
        f"你是网站研究的译者注作者。基于给定页面的译文与全站摘要，发现值得向读者"
        f"交代的前后文关联、创作背景、历史考据或趣味细节。\n"
        "输出 JSON：{\"notes\": [{\"src_quote\": \"原文片段\", "
        "\"dst_quote\": \"译文片段\", \"content\": "
        f"\"{tgt}的注释内容\", \"related_pages\": [\"关联页面路径\"], "
        "\"type\": \"前文呼应|创作背景|历史考据|趣味细节|其他\"}]}\n"
        "src_quote / dst_quote 要是原文/译文中真实存在的片段。只输出 JSON。"
        "没有可写的就返回空数组。"
    )


_V3_FEWSHOT = """
## 锚点示例（务必模仿「好」、避免「坏」）
好：
- src_quote="リッツベリーフィールズ", dst_quote="Ritzberry Fields"（专名，逐字、唯一）
- src_quote="ライブドア", dst_quote="Livedoor"（专名）
坏：
- dst_quote="这次的采访……"（带省略号，非原文连续子串）
- dst_quote="的采访"（无意义碎词，非最小有意义单元）
- dst_quote="嗯，那个"（在本页多次出现，锚点会落错位置）
"""


def _v3_structured_fewshot(cfg, glossary, max_notes) -> str:
    return prompts.build_translator_note_system(
        cfg, glossary=glossary, max_notes=max_notes) + "\n" + _V3_FEWSHOT


VARIANTS = {
    "V1": _v1_minimal,
    "V2": prompts.build_translator_note_system,
    "V3": _v3_structured_fewshot,
}


# --------------------------------------------------------------------------
def _metrics(parsed: dict | None, segs: list[dict], resp: str) -> dict:
    m = {"parse_ok": parsed is not None, "n_notes": 0}
    if not isinstance(parsed, dict):
        return m
    notes = parsed.get("notes") or []
    if not isinstance(notes, list):
        return m
    m["n_notes"] = len(notes)
    src_text = "\n".join(s["src"] for s in segs)
    dst_text = "\n".join(s["dst"] for s in segs)
    src_n = _strip_ph(src_text)
    dst_n = _strip_ph(dst_text)
    rows = []
    for nt in notes:
        if not isinstance(nt, dict):
            continue
        sq = nt.get("src_quote", "") or ""
        dq = nt.get("dst_quote", "") or ""
        row = {
            "src_quote": sq, "dst_quote": dq,
            "type": nt.get("type", ""), "content": nt.get("content", ""),
            "related_pages": nt.get("related_pages", []) or [],
            "src_len": len(sq), "dst_len": len(dq),
            "has_ph": bool(_PH.search(sq) or _PH.search(dq)),
            "dst_hit": bool(dq) and _strip_ph(dq) in dst_n,
            "src_hit": bool(sq) and _strip_ph(sq) in src_n,
        }
        # unique segment resolution by dst_quote
        if row["dst_hit"]:
            dq_n = _strip_ph(dq)
            hits = [s["id"] for s in segs
                    if dq_n and dq_n in _strip_ph(s["dst"])]
            row["hit_sids"] = hits
            row["unique"] = len(hits) == 1
        else:
            row["hit_sids"] = []
            row["unique"] = False
        rows.append(row)
    m["notes"] = rows
    valid = [r for r in rows if r["dst_quote"]]
    m["dst_hit_rate"] = round(
        sum(1 for r in valid if r["dst_hit"]) / len(valid), 3) if valid else None
    m["unique_rate"] = round(
        sum(1 for r in valid if r["unique"]) / len(valid), 3) if valid else None
    m["src_hit_rate"] = round(
        sum(1 for r in valid if r["src_hit"]) / len(valid), 3) if valid else None
    m["content_prefix"] = sum(
        1 for r in rows if str(r["content"]).strip().startswith("译者注"))
    return m


def _run_one(cfg, client, variant: str, rel: str, segs: list[dict],
             summaries: dict, glossary: list[dict], max_notes: int,
             run: int) -> dict:
    system = VARIANTS[variant](cfg, glossary, max_notes)
    user = prompts.build_translator_note_user(rel, segs, summaries,
                                              max_notes=max_notes)
    t0 = time.monotonic()
    resp, err = "", ""
    try:
        resp = client.chat(system, user, temperature=0.6,
                           tag=f"annprobe_{variant}_{rel.replace('/', '_')}")
    except llm_mod.LLMError as e:
        err = str(e)
    parsed = None
    if resp:
        try:
            parsed = llm_mod.parse_json_response(resp)
        except llm_mod.LLMError:
            parsed = None
    metrics = _metrics(parsed, segs, resp)
    return {
        "variant": variant, "page": rel, "run": run,
        "duration_s": round(time.monotonic() - t0, 1),
        "error": err, "system": system, "user": user, "response": resp,
        "parsed": parsed, "metrics": metrics,
    }


def _page_key(rel: str) -> str:
    return rel.replace("/", "__").replace(".html", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="实例数据根（相对 src/ 或绝对）")
    ap.add_argument("--pages", nargs="*", required=True)
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS.keys()))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-notes", type=int, default=0,
                    help="本页注数上限；0=由 LLM 决定")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    data_dir = args.data_dir
    if not os.path.isabs(data_dir):
        data_dir = os.path.normpath(os.path.join(SRC, data_dir))
    cfg = load_config(data_dir=data_dir)

    # 日志隔离到探针输出目录，避免污染实例工作区
    out_dir = os.path.abspath(args.out_dir)
    log_dir = os.path.join(out_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg.set(os.path.join(log_dir, ts), "llm_logs", "dir")

    client = llm_mod.LLMClient(cfg)
    glossary = gl_mod.load(cfg)
    summaries = _summaries(cfg)

    print(f"data_dir   : {data_dir}")
    print(f"model      : {client.model}  provider={client.provider}")
    print(f"pages      : {len(args.pages)}  variants={args.variants}  runs={args.runs}")
    print(f"glossary   : {len(glossary)}  summaries: {len(summaries)}")
    print(f"out_dir    : {out_dir}\n")

    results = []
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for variant in args.variants:
        if variant not in VARIANTS:
            print(f"⚠ 未知变体 {variant}，跳过")
            continue
        for rel in args.pages:
            segs = _page_segments(cfg, rel)
            if not segs:
                print(f"⚠ {rel}: 无 text 段（页面不存在或未译），跳过")
                continue
            for r in range(1, args.runs + 1):
                res = _run_one(cfg, client, variant, rel, segs, summaries,
                               glossary, args.max_notes, r)
                m = res["metrics"]
                fn = f"{variant}__{_page_key(rel)}__run{r}.json"
                util.write_json(os.path.join(out_dir, fn), res)
                results.append(res)
                flag = "OK" if m.get("parse_ok") else "PARSE-FAIL"
                print(f"  [{variant} {rel} run{r}] {flag} "
                      f"n={m.get('n_notes')} "
                      f"hit={m.get('dst_hit_rate')} uniq={m.get('unique_rate')} "
                      f"src={m.get('src_hit_rate')} {res['duration_s']}s")
    _summarize(results, out_dir, run_ts)


def _summarize(results: list[dict], out_dir: str, ts: str) -> None:
    by = {}
    for res in results:
        by.setdefault(res["variant"], []).append(res)

    lines = [f"# 译者注提示词探针汇总 {ts}", ""]
    agg = {}
    for variant, rows in by.items():
        ok = sum(1 for r in rows if r["metrics"].get("parse_ok"))
        n = sum(r["metrics"].get("n_notes", 0) for r in rows)
        valid = [r["metrics"] for r in rows
                 if r["metrics"].get("parse_ok")]
        def _avg(key):
            vals = [x.get(key) for x in valid if x.get(key) is not None]
            return round(sum(vals) / len(vals), 3) if vals else None
        agg[variant] = {
            "runs": len(rows), "parse_ok": ok, "notes_total": n,
            "avg_notes_per_page": round(n / len(rows), 2) if rows else 0,
            "avg_dst_hit_rate": _avg("dst_hit_rate"),
            "avg_unique_rate": _avg("unique_rate"),
            "avg_src_hit_rate": _avg("src_hit_rate"),
            "content_prefix_total": sum(
                r["metrics"].get("content_prefix", 0) for r in rows),
        }
        lines.append(f"## {variant}")
        lines.append(json.dumps(agg[variant], ensure_ascii=False))
        lines.append("")

    util.write_json(os.path.join(out_dir, f"summary_{ts}.json"),
                    {"aggregate": agg,
                     "results": [{"variant": r["variant"], "page": r["page"],
                                  "run": r["run"], "metrics": r["metrics"],
                                  "error": r["error"]} for r in results]})
    with open(os.path.join(out_dir, "analysis.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n=== 汇总 ===")
    print("\n".join(lines))
    print(f"\n结果目录 -> {out_dir}")


if __name__ == "__main__":
    main()
