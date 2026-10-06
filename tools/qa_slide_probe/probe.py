"""QA 滑窗覆盖实验探针（whole-page vs sliding-window）。

目的：**只读**地在真实实例上对比"整页一次送入"与"同款无段号文本按滑窗切块"
两种 QA 送文的**问题覆盖率 / 精度 / 成本**，为是否把滑窗落地到
``booktr.qa.run_qa`` 提供数据（详见 ``instance/report/qa_slide_experiment.md``）。

设计要点（与正式实现同口径）：
- **整页 baseline**：`whole` 条完全复刻 ``qa.run_qa`` 的 LLM 送文（含硬编码
  ``[:6000]`` 截断），本地确定性检查（占位符/术语）直接复用 ``qa.run_qa``
  （临时置 ``deep_llm_check=False``），保证与生产一字不差。
- **滑窗**：把**已翻译段**按累积字符预算（默认 1500 / 2500）切成窗口，
  **相邻窗口重叠 N 段**（默认 2）以保留邻接语境；每窗口一次 LLM 调用；
  各窗口问题合并后按 ``(段号集合, 归一 reason)`` 去重。
- 提示词仍为**无段号的整块文本**（复用 ``prompts.build_qa_user``，不引入段号），
  问题定位复用 ``qa.locate_segments``（对**整页**段列表），故段号口径一致。
- 只读：不写实例任何文件；LLM 日志隔离到工具 ``_out/logs``。

用法（仓库根 `src/`）：
    python tools/qa_slide_probe/probe.py coverage \
        --data-dir ../../instance/data-deepseek-v4.1-flash \
        --pages disco/disco.html today/today15.html --runs 3
    # 省略 --pages 时按段数分位自动选页（跨尺寸）
    python tools/qa_slide_probe/probe.py coverage \
        --data-dir ../../instance/data-deepseek-v4.1-flash --auto-pages
    # 结果落 _out/coverage_result.json；再用 analyze.py 汇总
    python tools/qa_slide_probe/analyze.py --result tools/qa_slide_probe/_out/coverage_result.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# 本文件位于 <repo>/tools/qa_slide_probe/probe.py，故仓库根（src/）为向上两级。
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, SRC)

from booktr import glossary as gl  # noqa: E402
from booktr import llm as llm_mod  # noqa: E402
from booktr import prompts  # noqa: E402
from booktr import qa as qa_mod  # noqa: E402
from booktr import segments as seg_mod  # noqa: E402
from booktr import translate as tr  # noqa: E402
from booktr import util  # noqa: E402
from booktr.config import load_config  # noqa: E402

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(TOOL_DIR, "_out")

# ── 纯函数：切窗 / 去重（可单测） ──────────────────────────────────────────


def build_windows(segs: list, budget: int, overlap: int) -> list[list]:
    """把段按累积**源文字符**预算切成窗口，相邻窗口重叠 overlap 段。

    不做段内截断；单个超过 budget 的段自成一窗。空列表返回空列表。
    """
    if not segs:
        return []
    budget = max(1, int(budget))
    overlap = max(0, int(overlap))
    windows: list[list] = []
    n = len(segs)
    i = 0
    while i < n:
        j = i
        chars = 0
        while j < n:
            ln = len(getattr(segs[j], "text", "") or "")
            if j > i and chars + ln > budget:
                break
            chars += ln
            j += 1
        windows.append(segs[i:j])
        if j >= n:
            break
        i = max(i + 1, j - overlap)  # 必前进，避免死循环
    return windows


def norm_key(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def issue_key(iss: dict) -> tuple:
    """去重键：段号集合 + 归一化 reason。"""
    segs = tuple(sorted(str(x) for x in (iss.get("segments") or [])))
    return (segs, norm_key(iss.get("reason", "")))


def dedup_issues(issues: list[dict]) -> list[dict]:
    """按 issue_key 去重（保序保留首个）。"""
    seen: set = set()
    out: list[dict] = []
    for it in issues:
        k = issue_key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ── 配置 / 客户端（日志隔离到工具目录） ────────────────────────────────────


def _cfg(data_dir: str, out_dir: str, mock: bool = False):
    cfg = load_config(data_dir=data_dir)
    if mock:
        cfg.set("mock", "llm", "provider")  # 冒烟测试：不消耗配额
    log_dir = os.path.join(out_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    cfg.set(log_dir, "llm_logs", "dir")
    return cfg


def _client(cfg, run_tag: str = ""):
    cli = llm_mod.LLMClient(cfg, run_tag=run_tag)
    cli.timeout = 600
    cli.connect_timeout = 20
    cli.max_retries = 2
    return cli


# ── 单次 LLM 调用捕获（reasoning / duration / tokens） ────────────────────


def _capture_chat(client, sysp: str, usr: str, temperature: float, tag: str,
                  reasoning_effort: str | None = None) -> tuple[str, dict]:
    """调用 client.chat 并用 on_delta 捕获 reasoning；返回 (resp, meta)。"""
    acc = {"reasoning": "", "content": ""}

    def on_delta(kind, text):
        acc[kind] = acc.get(kind, "") + text

    before = client.stats_report()
    t0 = time.monotonic()
    ok, err, resp = True, "", ""
    try:
        resp = client.chat(sysp, usr, temperature=temperature, tag=tag,
                           reasoning_effort=reasoning_effort, on_delta=on_delta)
    except llm_mod.LLMError as e:
        ok, err = False, str(e)
        acc["reasoning"] = getattr(e, "reasoning", "") or acc["reasoning"]
    dur = time.monotonic() - t0
    after = client.stats_report()
    reasoning = acc["reasoning"]
    meta = {
        "ok": ok, "error": err,
        "reasoning_chars": len(reasoning),
        "duration_s": round(dur, 1),
        "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
        "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
        "loop": bool(reasoning) and bool(llm_mod.find_repetition(reasoning)),
        "finish_len": "length" in err,
    }
    return resp, meta


# ── 本地确定性检查（复用生产 run_qa，仅关 LLM） ────────────────────────────


def _local_issues(cfg, client, rel: str) -> list[dict]:
    old = cfg.get("qa", "deep_llm_check", default=True)
    cfg.set(False, "qa", "deep_llm_check")
    try:
        return qa_mod.run_qa(cfg, client, rel)
    finally:
        cfg.set(old, "qa", "deep_llm_check")


# ── 单条件 LLM 问题 ────────────────────────────────────────────────────────


def _llm_issues_from_texts(cfg, client, gl_confirmed, user_rules, audit_policy,
                            history_block, src_text, dst_text, all_segs,
                            tag) -> tuple[list[dict], dict]:
    """把一段（已拼好的）原文/译文送 QA，返回 (issues, meta)。

    复刻 ``qa.run_qa`` 的 LLM 段（``booktr/qa.py`` :122-147）。
    """
    sysp = prompts.build_qa_system(cfg, user_rules, audit_policy=audit_policy)
    usr = prompts.build_qa_user(src_text, dst_text, gl_confirmed,
                                history_block=history_block)
    eff = (cfg.get("qa", "reasoning_effort", default="")
           or cfg.get("llm", "reasoning_effort", default=""))
    resp, meta = _capture_chat(client, sysp, usr, 0.2, tag, eff or None)
    issues: list[dict] = []
    if meta["ok"] and resp.strip():
        try:
            data = llm_mod.parse_json_response(resp)
        except llm_mod.LLMError:
            data = {}
        for iss in data.get("issues", []) or []:
            sq = iss.get("src_quote", "") or ""
            dq = iss.get("dst_quote", "") or ""
            found = qa_mod.locate_segments(all_segs, sq, dq)
            issues.append({
                "severity": iss.get("severity", "mid"),
                "reason": iss.get("reason", "") or iss.get("problem", ""),
                "src_quote": sq, "dst_quote": dq,
                "suggestion": iss.get("suggestion", ""),
                "segments": found, "resolved": bool(found),
            })
    return issues, meta


def run_condition(cfg, client, rel, segs, cond: str, gl_confirmed, user_rules,
                  audit_policy, history_block, overlap: int) -> dict:
    """跑一个条件（whole / win<budget>），返回该条件的运行记录。

    ``whole``：复刻 ``qa.run_qa`` 的 LLM 送文（拼接 + ``[:6000]`` 截断），1 次调用。
    ``win<N>``：累积字符预算 N 切窗，重叠 overlap 段，逐窗调用后合并去重。
    """
    # 与生产 qa.run_qa 一致：仅取译文非空的段
    tr_segs = [s for s in segs if s.translation]
    safe = rel.replace("/", "_")
    if cond == "whole":
        src_all = "\n".join(s.text for s in tr_segs)
        dst_all = "\n".join(s.translation or "" for s in tr_segs)
        issues, meta = _llm_issues_from_texts(
            cfg, client, gl_confirmed, user_rules, audit_policy, history_block,
            src_all[:6000], dst_all[:6000], segs, tag=f"qa_whole_{safe}")
        return {"calls": 1, "windows": [{"seg_ids": [s.id for s in tr_segs]}],
                "llm_issues": dedup_issues(issues),
                "calls_meta": [meta],
                "prompt_tokens": meta["prompt_tokens"],
                "completion_tokens": meta["completion_tokens"],
                "duration_s": meta["duration_s"],
                "reasoning_chars": meta["reasoning_chars"],
                "loops": int(meta["loop"])}
    # 滑窗
    budget = int(cond[3:])
    wins = build_windows(tr_segs, budget, overlap)
    all_issues: list[dict] = []
    metas: list[dict] = []
    win_info: list[dict] = []
    for wi, w in enumerate(wins, 1):
        src_w = "\n".join(s.text for s in w)
        dst_w = "\n".join(s.translation or "" for s in w)
        iss, meta = _llm_issues_from_texts(
            cfg, client, gl_confirmed, user_rules, audit_policy, history_block,
            src_w, dst_w, segs, tag=f"qa_{cond}_{safe}_w{wi}")
        all_issues += iss
        metas.append(meta)
        win_info.append({"seg_ids": [s.id for s in w]})
    merged = dedup_issues(all_issues)
    return {
        "calls": len(wins), "windows": win_info,
        "llm_issues": merged, "raw_issue_count": len(all_issues),
        "calls_meta": metas,
        "prompt_tokens": sum(m["prompt_tokens"] for m in metas),
        "completion_tokens": sum(m["completion_tokens"] for m in metas),
        "duration_s": round(sum(m["duration_s"] for m in metas), 1),
        "reasoning_chars": sum(m["reasoning_chars"] for m in metas),
        "loops": sum(int(m["loop"]) for m in metas),
        "finish_len": sum(int(m["finish_len"]) for m in metas),
    }


cfg_overlap = 2  # 已废弃：overlap 改为显式传参


# ── 选页 ────────────────────────────────────────────────────────────────────


def _page_sizes(cfg) -> list[tuple[str, int]]:
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    order = plan.get("order") or list((plan.get("pages") or {}).keys())
    rows = []
    for rel in order:
        try:
            segs = seg_mod.segments_for_page(cfg, rel)
        except Exception:
            continue
        tr_n = sum(1 for s in segs if s.translation is not None)
        rows.append((rel, tr_n))
    return rows


def auto_pages(cfg, count: int) -> list[str]:
    """按已翻译段数分位跨尺寸选页（含最大页）。"""
    rows = _page_sizes(cfg)
    rows.sort(key=lambda x: -x[1])
    if not rows:
        return []
    picks = []
    n = len(rows)
    for k in range(count):
        idx = round(k * (n - 1) / max(1, count - 1)) if count > 1 else 0
        picks.append(rows[idx][0])
    # 去重保序
    seen, out = set(), []
    for p in picks:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── 模式 ────────────────────────────────────────────────────────────────────


def cmd_coverage(cfg, args) -> None:
    pages = args.pages or auto_pages(cfg, args.auto_pages)
    if not pages:
        print("未选定页面（--pages 或 --auto-pages）", flush=True)
        return
    conditions = ["whole"] + [f"win{b}" for b in args.windows]
    os.makedirs(args.out, exist_ok=True)
    result_path = os.path.join(args.out, "coverage_result.json")
    result = util.read_json(result_path, {}) if args.resume else {}
    result.setdefault("meta", {})
    result["meta"].update({
        "data_dir": os.path.abspath(cfg.data_dir),
        "conditions": conditions, "windows": args.windows,
        "overlap": args.overlap, "runs": args.runs,
        "model": cfg.get("llm", "model", default=""),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    result.setdefault("pages", {})
    gl_confirmed = gl.all_confirmed(cfg)
    client = _client(cfg)
    t_all = time.time()
    for rel in pages:
        if args.resume and rel in result["pages"]:
            print(f"{rel}: 已完成，跳过（resume）", flush=True)
            continue
        segs = seg_mod.segments_for_page(cfg, rel)
        tr_n = sum(1 for s in segs if s.translation is not None)
        src_chars = sum(len(s.text or "") for s in segs if s.translation is not None)
        print(f"\n=== {rel}  段={tr_n} 源文字符={src_chars} ===", flush=True)
        # 本地确定性检查（各条件共用）
        local = _local_issues(cfg, client, rel)
        # 提示词公共参数
        user_rules = tr.effective_user_rules(cfg)
        audit_policy = cfg.get("qa", "reduce_style_reports", default=True)
        history_block = ""
        if cfg.get("qa", "inject_history", default=False):
            history_block = prompts.build_qa_history_block(
                qa_mod.build_history(cfg, rel))
        page_rec = {"seg_count": tr_n, "src_chars": src_chars,
                    "local_issue_count": len(local), "conditions": {}}
        for cond in conditions:
            runs = []
            for run_i in range(1, args.runs + 1):
                t0 = time.time()
                rec = run_condition(cfg, client, rel, segs, cond, gl_confirmed,
                                    user_rules, audit_policy, history_block,
                                    args.overlap)
                rec["run"] = run_i
                rec["wall_s"] = round(time.time() - t0, 1)
                runs.append(rec)
                print(f"  {cond:8} run{run_i}: {rec['calls']} 调用  "
                      f"{len(rec['llm_issues'])} LLM问题  {rec['wall_s']}s  "
                      f"reason={rec['reasoning_chars']}c  loops={rec['loops']}",
                      flush=True)
            page_rec["conditions"][cond] = {"runs": runs}
        result["pages"][rel] = page_rec
        util.write_json(result_path, result)
        print(f"  增量落盘: {result_path}", flush=True)
    result["meta"]["total_wall_s"] = round(time.time() - t_all, 1)
    util.write_json(result_path, result)
    print(f"\n结果留存: {result_path}", flush=True)


def cmd_sizes(cfg, args) -> None:
    for rel, n in sorted(_page_sizes(cfg), key=lambda x: -x[1]):
        print(f"{n:4}  {rel}")


def main():
    ap = argparse.ArgumentParser(description="QA 滑窗覆盖实验探针")
    ap.add_argument("mode", choices=["coverage", "sizes"])
    ap.add_argument("--data-dir", required=True,
                    help="数据目录（只读；可指向生产实例）")
    ap.add_argument("--pages", nargs="*", default=None)
    ap.add_argument("--auto-pages", type=int, default=0,
                    help="不传 --pages 时，按段数分位自动选 N 页")
    ap.add_argument("--windows", nargs="+", type=int, default=[1500, 2500],
                    help="滑窗字符预算（可多个）")
    ap.add_argument("--overlap", type=int, default=2, help="相邻窗口重叠段数")
    ap.add_argument("--runs", type=int, default=3, help="每条件重复次数")
    ap.add_argument("--out", default=OUT_DIR, help="结果输出目录（默认 _out/）")
    ap.add_argument("--resume", action="store_true", help="跳过已完成页")
    ap.add_argument("--mock", action="store_true",
                    help="用 mock LLM（冒烟测试，不消耗配额、无真实问题）")
    args = ap.parse_args()
    args._argv = " ".join(sys.argv[1:])

    cfg = _cfg(args.data_dir, args.out, mock=args.mock)
    if args.mode == "sizes":
        cmd_sizes(cfg, args)
    else:
        if not args.pages and args.auto_pages <= 0:
            args.auto_pages = 10
        cmd_coverage(cfg, args)


if __name__ == "__main__":
    main()
