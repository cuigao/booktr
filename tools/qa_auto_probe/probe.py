"""监督式自动 QA（qa-auto）探针 / 评估工具。

对真实实例（只读）或沙箱副本运行监督判官的提示词与多轮对话方案实验：
- ``judge``：对已有人工裁决的页重放判官（只读），与人工历史对照（诊断用；
  注意判官以"当前译文"为依据，已 applied 条目会（正确地）判 reject）。
- ``e2e``：端到端 ``QA1 → 判官 → apply → QA2``，产出 before/after diff 与
  QA2，供"问题级复现率"评估（需可写的沙箱 data-dir）。

用法（在仓库根 `src/` 或任意 cwd 均可，脚本自定位）：
    python tools/qa_auto_probe/probe.py judge --data-dir <data_dir> \\
        --pages profile/profile.html today/today13.html
    python tools/qa_auto_probe/probe.py e2e --data-dir <sandbox_dir> \\
        --pages today/today14.html today/today15.html
    # 断点续跑：加 --resume（跳过结果文件中已完成的页）

结果（`judge_result.json` / `e2e_result.json` / `logs/`）写入 <out_dir>（默认
本工具目录下的 `_out/`，已 gitignore）。评估报告见
`instance/report/qa_auto_probe_report.md`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 本文件位于 <repo>/tools/qa_auto_probe/probe.py，故仓库根（src/）为向上两级。
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, SRC)

from booktr.config import load_config  # noqa: E402
from booktr import glossary as gl  # noqa: E402
from booktr import llm as llm_mod  # noqa: E402
from booktr import prompts  # noqa: E402
from booktr import qa as qa_mod  # noqa: E402
from booktr import qa_queue as qq  # noqa: E402
from booktr import segments as seg_mod  # noqa: E402
from booktr import styles as styles_mod  # noqa: E402
from booktr import supervisor as sup_mod  # noqa: E402
from booktr import translate as tr  # noqa: E402
from booktr import util  # noqa: E402

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(TOOL_DIR, "_out")
ALL_SUMMARIES_MAX = 65536


def _cfg(data_dir: str, out_dir: str) -> object:
    cfg = load_config(data_dir=data_dir)
    # 探针日志隔离到工具输出目录，避免污染实例工作区
    log_dir = os.path.join(out_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    cfg.set(log_dir, "llm_logs", "dir")
    return cfg


def _client(cfg):
    """判官专用 client：较短超时、少重试，快速暴露网络抖动。"""
    cli = llm_mod.LLMClient(cfg)
    cli.timeout = 120
    cli.connect_timeout = 20
    cli.max_retries = 1
    return cli


# --------------------------------------------------------------------------
# 语境构建复用 supervisor（保持与正式实现同口径）
# --------------------------------------------------------------------------
def build_full_context(cfg, rel: str) -> dict:
    return sup_mod.build_context(cfg, rel)


# 判官 prompt 复用 supervisor 正式实现（保证实验与实现一致）
judge_system = lambda cfg, ctx, n_items=0: prompts.build_supervisor_system(cfg, ctx)
seed_user = prompts.build_supervisor_seed_user
item_user = prompts.build_supervisor_item_user
_fmt_item = prompts.format_supervisor_item
parse_verdict = sup_mod.parse_verdict


# --------------------------------------------------------------------------
# 多轮裁决（带首 token 计时诊断）
# --------------------------------------------------------------------------
def _call_with_timing(client, messages, tag):
    first = {"t": None}
    t0 = time.monotonic()

    def on_delta(kind, text):
        if first["t"] is None:
            first["t"] = round(time.monotonic() - t0, 1)

    resp = client.chat_multi(messages, temperature=0.1, tag=tag, on_delta=on_delta)
    return resp, first["t"], round(time.monotonic() - t0, 1)


def adjudicate(cfg, client, rel: str, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (verdicts, turns)。逐条容错：网络/解析失败记 skip 并继续。"""
    ctx = build_full_context(cfg, rel)
    system = judge_system(cfg, ctx, len(items))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": seed_user(items)}]
    verdicts, turns = [], []
    for i, it in enumerate(items, 1):
        tag = f"judge_{rel.replace('/', '_')}"
        try:
            resp, first_t, dur = _call_with_timing(client, messages, tag)
        except llm_mod.LLMError as e:
            v = {"verdict": "skip", "reason": f"网络错误: {str(e)[:120]}", "suggestion": ""}
            verdicts.append({**v, "item_id": it.get("id"), "segments": it.get("segments"),
                             "severity": it.get("severity"), "human": it.get("status")})
            turns.append({"turn": i, "error": str(e)[:200]})
            print(f"    ! 第{i}条网络错误，跳过: {str(e)[:80]}", flush=True)
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": seed_user(items)}]
            continue
        v = parse_verdict(resp)
        verdicts.append({**v, "item_id": it.get("id"), "segments": it.get("segments"),
                         "severity": it.get("severity"), "human": it.get("status")})
        turns.append({"turn": i, "assistant": resp, "parsed": v,
                      "first_token_s": first_t, "duration_s": dur})
        messages.append({"role": "assistant", "content": resp})
        if i < len(items):
            messages.append({"role": "user", "content": item_user(i + 1, len(items), items[i])})
    return verdicts, turns


# --------------------------------------------------------------------------
# 模式
# --------------------------------------------------------------------------
def cmd_judge(cfg, args) -> None:
    queue = qq.load(cfg)
    client = _client(cfg)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "judge_result.json")
    out = util.read_json(path, {}) if (args.resume and os.path.exists(path)) else {}
    for rel in args.pages:
        if args.resume and rel in out:
            print(f"{rel}: 已完成，跳过（resume）")
            continue
        items = [it for it in queue if it.get("page") == rel
                 and it.get("resolved") and it.get("segments")]
        if not items:
            print(f"{rel}: 无已定位 QA 条目，跳过")
            continue
        print(f"\n=== {rel}: {len(items)} 条 ===", flush=True)
        verdicts, turns = adjudicate(cfg, client, rel, items)
        agree, denom = 0, 0
        for v, it in zip(verdicts, items):
            human_v = {"applied": "adopt", "rejected": "reject"}.get(it.get("status"), "?")
            if human_v != "?":
                denom += 1
                if v["verdict"] == human_v:
                    agree += 1
            mark = "✓" if v["verdict"] == human_v else "✗"
            print(f"  {mark} 段{v.get('segments')} [{v.get('severity')}] "
                  f"judge={v['verdict']:6} human={human_v:6} | {v.get('reason','')[:50]}",
                  flush=True)
        print(f"  一致率(全部) {agree}/{denom}", flush=True)
        out[rel] = {"verdicts": verdicts, "turns": turns,
                    "human_ref": [it.get("status") for it in items], "items": items}
        util.write_json(path, out)
    print(f"\n结果留存: {path}")


def cmd_e2e(cfg, args) -> None:
    client = _client(cfg)
    sm = util.read_json(os.path.join(cfg.work_dir, "site_map.json"), {})
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "e2e_result.json")
    result = util.read_json(path, {}) if (args.resume and os.path.exists(path)) else {}
    for rel in args.pages:
        if args.resume and rel in result:
            print(f"{rel}: 已完成，跳过（resume）")
            continue
        print(f"\n=== e2e {rel} ===", flush=True)
        segs = seg_mod.segments_for_page(cfg, rel)
        before = {str(s.id): (s.translation or "") for s in segs}
        issues1 = qa_mod.run_qa(cfg, client, rel)
        items = [qq.make_item(rel, i) for i in issues1
                 if i.get("resolved") and i.get("segments")]
        print(f"  QA1: {len(issues1)} 问题（{len(items)} 条已定位）", flush=True)
        if not items:
            result[rel] = {"before": before, "issues1": issues1, "items": [],
                           "verdicts": [], "diff": {}, "applied": 0, "issues2": []}
            util.write_json(path, result)
            continue
        verdicts, turns = adjudicate(cfg, client, rel, items)
        for v, it in zip(verdicts, items):
            it["status"] = ("adopted" if v["verdict"] == "adopt"
                            else "rejected" if v["verdict"] == "reject" else "open")
            if v["verdict"] == "adopt" and v.get("suggestion") \
                    and v["suggestion"].strip() != (it.get("suggestion") or "").strip():
                it["llm_suggestion"] = it.get("suggestion", "")
                it["suggestion"] = v["suggestion"]
        applied = 0 if args.no_apply else _apply(cfg, client, sm, plan, rel, items)
        segs2 = seg_mod.segments_for_page(cfg, rel)
        after = {str(s.id): (s.translation or "") for s in segs2}
        diff = {sid: (before.get(sid, ""), after.get(sid, ""))
                for sid in set(before) | set(after) if before.get(sid, "") != after.get(sid, "")}
        issues2 = qa_mod.run_qa(cfg, client, rel) if not args.no_qa2 else []
        result[rel] = {
            "before": before, "after": after, "diff": diff,
            "issues1": issues1, "items": items, "verdicts": verdicts,
            "issues2": issues2, "applied": applied, "turns": turns,
        }
        util.write_json(path, result)
        print(f"  应用 {applied} 段；diff 段 {list(diff)}；QA2 {len(issues2)} 问题", flush=True)
    print(f"\n结果留存: {path}")


# --------------------------------------------------------------------------
# 变体矩阵实验：translate / qa / judge 三端 × V0..V5 × N 次
# --------------------------------------------------------------------------
VARIANT_TABLE = {
    "V0": {"policy": False, "style": "standard", "history": False},
    "V1": {"policy": True, "style": "standard", "history": False},
    "V2": {"policy": True, "style": "faithful", "history": False},
    "V3": {"policy": True, "style": "standard", "history": True},
    "V4": {"policy": True, "style": "faithful", "history": True},
    "V5": {"policy": True, "style": "fluent", "history": False},
}


def _style_rules_text(style: str) -> str:
    from booktr import pipeline as pl
    return next((r for k, _, r in pl.TRANSLATION_STYLES if k == style), "")


def _apply_variant_cfg(cfg, v: dict) -> None:
    """把变体开关写入 cfg（影响提示词构建）。"""
    cfg.set(bool(v["policy"]), "qa", "reduce_style_reports")
    cfg.set(bool(v["history"]), "qa", "inject_history")


def _translate_run(cfg, client, pages: list[str], style: str) -> dict:
    """重置目标页（清 TM/notes/短语）后整页翻译，返回每页最终译文快照。

    style 通过临时追加到 user_rules 实现（不动实例配置）。
    """
    from booktr import styles as styles_mod
    from booktr.config import Config
    # 临时把风格规则并入 user_rules（内存内）
    base_rules = cfg.get("user_rules", default="") or ""
    sr = _style_rules_text(style)
    if sr:
        cfg.set((base_rules + "\n\n" + sr).strip(), "user_rules")

    sm = util.read_json(os.path.join(cfg.work_dir, "site_map.json"), {})
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    state = tr.State(cfg)
    # 重置（清 TM/notes/短语 + 状态 pending），确保从零开始
    for rel in pages:
        _reset_page_silent(cfg, state, rel)

    snapshot = {}
    for rel in pages:
        segs = seg_mod.segments_for_page(cfg, rel)
        rq: list = []
        tr.translate_page(cfg, client, rel, state, sm, plan, rq)
        segs = seg_mod.segments_for_page(cfg, rel)
        snapshot[rel] = {str(s.id): (s.translation or "") for s in segs}
    state.save()
    cfg.set(base_rules, "user_rules")  # 还原
    return snapshot


def _reset_page_silent(cfg, state, rel: str) -> None:
    """静默整页重置：清 TM/notes/短语 + 删段缓存 + status=pending（不入队列）。"""
    from booktr import tm as tm_mod
    from booktr import notes as notes_mod
    from booktr import phrases as phrases_mod
    pstate = state.page(rel)
    segs = pstate.get("segments", {})
    tm_mod.purge_page(cfg, rel)
    notes_mod.purge_page(cfg, rel)
    for sid, st in segs.items():
        try:
            phrases_mod.purge_for_segment(cfg, st.get("text", ""),
                                          st.get("translation") or "", "")
        except Exception:
            pass
    seg_path = os.path.join(cfg.get("segments_dir", default=""),
                            rel.replace("/", "__") + ".json")
    if os.path.exists(seg_path):
        os.remove(seg_path)
    pstate["status"] = "pending"
    pstate["segments"] = {}


def _qa_run(cfg, client, pages: list[str]) -> dict:
    out = {}
    for rel in pages:
        issues = qa_mod.run_qa(cfg, client, rel)
        out[rel] = issues
    return out


def _judge_run(cfg, client, pages: list[str]) -> dict:
    queue = qq.load(cfg)
    out = {}
    for rel in pages:
        items = [it for it in queue if it.get("page") == rel
                 and it.get("resolved") and it.get("segments")]
        if not items:
            out[rel] = {"verdicts": [], "n_items": 0}
            continue
        verdicts, turns = adjudicate(cfg, client, rel, items)
        out[rel] = {"verdicts": verdicts, "turns": turns, "n_items": len(items)}
    return out


def _run_valid(path: str, end: str, n_pages: int) -> bool:
    """已有结果文件是否**有效**（用于 --skip-existing）：calls >= 页数 即视为有效。"""
    j = util.read_json(path, None)
    if not isinstance(j, dict):
        return False
    calls = (j.get("llm") or {}).get("calls", 0)
    return calls >= n_pages


def cmd_variant(cfg, args) -> None:
    """变体矩阵实验：对指定页按 --variant 集逐变体逐次运行指定端，详细记录成本。

    **逐次独立容错**：单次 run 失败（LLM 网络错误等）写 calls=0+error 记录并继续
    下一变体，不中断整批。`--skip-existing`：已有**有效**结果则跳过（坏次重跑）。
    """
    os.makedirs(args.out, exist_ok=True)
    runs_dir = os.path.join(args.out, "variant_runs")
    os.makedirs(runs_dir, exist_ok=True)
    variants = args.variant or list(VARIANT_TABLE)
    pages = args.pages
    n_pages = len(pages)
    summary = []

    for vkey in variants:
        v = VARIANT_TABLE.get(vkey)
        if v is None:
            print(f"未知变体 {vkey}，跳过"); continue
        for run_i in range(1, args.runs + 1):
            fname = f"{args.end}_{vkey}_run{run_i}.json"
            fpath = os.path.join(runs_dir, fname)
            if getattr(args, "skip_existing", False) and _run_valid(fpath, args.end, n_pages):
                print(f"  {args.end} {vkey} run{run_i}: 有效，跳过（skip-existing）", flush=True)
                continue
            style = v["style"]
            if args.end == "translate" and args.style != "auto":
                style = args.style
            t0 = time.time()
            started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            client = None
            try:
                cfg2 = _cfg(args.data_dir, args.out)
                _apply_variant_cfg(cfg2, v)
                client = _client(cfg2)
                if args.end == "translate":
                    snap = _translate_run(cfg2, client, pages, style)
                    payload = {"style": style, "snapshot": snap}
                elif args.end == "qa":
                    payload = {"vcfg": v, "qa": _qa_run(cfg2, client, pages)}
                else:
                    payload = {"vcfg": v, "judge": _judge_run(cfg2, client, pages)}
                err = ""
            except Exception as e:  # 单次失败不中断整批
                payload = {}
                err = f"{type(e).__name__}: {e}"
                print(f"  ! {args.end} {vkey} run{run_i} 失败: {err[:160]}", flush=True)
            dur = round(time.time() - t0, 1)
            st = client.stats_report() if client else {"calls": 0, "prompt_tokens": 0,
                                                       "completion_tokens": 0}
            rec = {"variant": vkey, "run": run_i, "end": args.end,
                   "pages": pages, "duration_s": dur,
                   "started_at": started_at,
                   "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "llm": st,
                   "command": getattr(args, "_argv", ""), **payload}
            if err:
                rec["error"] = err
            util.write_json(fpath, rec)
            print(f"  {args.end} {vkey} run{run_i}: {dur}s  "
                  f"tokens p={st.get('prompt_tokens')} c={st.get('completion_tokens')} "
                  f"calls={st.get('calls')}" + ("  [FAILED]" if err else ""), flush=True)
            summary.append({"file": fname, "variant": vkey, "run": run_i,
                            "end": args.end, "duration_s": dur,
                            "started_at": started_at, "llm": st,
                            "error": err})
    util.write_json(os.path.join(args.out, f"variant_summary_{args.end}.json"),
                    {"testset": pages, "runs": args.runs, "rows": summary})
    print(f"\n汇总留存: {os.path.join(args.out, f'variant_summary_{args.end}.json')}")


def _apply(cfg, client, sm, plan, rel, items) -> int:
    state = tr.State(cfg)
    guide = styles_mod.load_guide(cfg) if cfg.get("style", "rules_enabled", default=True) else ""
    focus = cfg.get("translators_notes", "focus", default="")
    _, _, user_rules = tr.build_context(cfg, sm, plan, rel)
    grouped: dict[tuple, list[dict]] = {}
    for it in items:
        if it.get("status") != "adopted":
            continue
        for sid in it.get("segments", []) or []:
            grouped.setdefault((it["page"], str(sid)), []).append(it)
    ok = 0
    for (r, sid), ops in grouped.items():
        opinions = [{"severity": o.get("severity", ""), "reason": o.get("reason", ""),
                     "src_quote": o.get("src_quote", ""), "dst_quote": o.get("dst_quote", ""),
                     "suggestion": o.get("suggestion", "")} for o in ops]
        res = tr.apply_qa_fix(cfg, client, r, sid, opinions, state, sm, plan,
                              guide=guide, user_rules=user_rules, focus=focus)
        if res.get("ok"):
            ok += 1
            for o in ops:
                o["status"] = "applied"
            print(f"    ✓ 段{sid} 已修正", flush=True)
        else:
            print(f"    ✗ 段{sid} 修正失败", flush=True)
    state.save()
    return ok


def main():
    ap = argparse.ArgumentParser(description="监督式自动 QA 探针")
    ap.add_argument("mode", choices=["judge", "e2e", "variant"])
    ap.add_argument("--data-dir", required=True,
                    help="数据目录（judge 可指向只读实例；e2e/variant 须指向可写沙箱副本）")
    ap.add_argument("--pages", nargs="+", required=True)
    ap.add_argument("--out", default=OUT_DIR, help="结果输出目录（默认 _out/）")
    ap.add_argument("--no-apply", action="store_true")
    ap.add_argument("--no-qa2", action="store_true", help="e2e 后不重跑 QA2")
    ap.add_argument("--resume", action="store_true", help="跳过结果文件中已完成的页")
    # variant 专用
    ap.add_argument("--end", choices=["translate", "qa", "judge"], default="qa",
                    help="variant 模式：运行哪一端")
    ap.add_argument("--variant", nargs="+", default=None,
                    help="变体集（默认 V0..V5）")
    ap.add_argument("--style", default="auto",
                    help="translate 端覆盖风格（auto=用变体定义；或 standard/faithful/fluent）")
    ap.add_argument("--runs", type=int, default=3, help="每变体重复次数")
    ap.add_argument("--skip-existing", action="store_true",
                    help="已有有效结果（calls>=页数）则跳过；坏次自动重跑")
    args = ap.parse_args()
    args._argv = " ".join(sys.argv[1:])
    cfg = _cfg(args.data_dir, args.out)
    if args.mode == "judge":
        cmd_judge(cfg, args)
    elif args.mode == "e2e":
        cmd_e2e(cfg, args)
    else:
        cmd_variant(cfg, args)


if __name__ == "__main__":
    main()
