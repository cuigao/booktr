"""一致性 QA pass：LLM 校验术语一致性与 HTML 安全。

QA 为页面级、无状态：逐页观察整页原文/译文，产出标准化问题清单
（severity/reason/src_quote/dst_quote/suggestion），再按引用的原文/译文片段
**机械定位**到具体段（LLM 不再给出段号，避免臆造）。
"""
from __future__ import annotations

import os
import re
import time

from . import glossary as gl
from . import llm as llm_mod
from . import prompts
from . import segments as seg_mod
from . import translate as tr
from . import util
from .config import Config


def locate_segments(segs: list, src_quote: str, dst_quote: str) -> list[int]:
    """把 QA 引用的原文/译文片段机械定位到段号。

    委托通用定位器 ``locate.locate``（单一口径）。仅检索**已翻译段**，
    模糊匹配只取最优一个候选，返回命中的段 id 列表（可能为空）。
    """
    from . import locate as locate_mod

    results = locate_mod.locate(
        segs, src_frag=src_quote, dst_frag=dst_quote,
        include_untranslated=False, top=1)
    return [r["sid"] for r in results]


def build_history(cfg: Config, rel: str) -> list[dict]:
    """组装某页"已发生的 QA 决策"（供 QA/判官注入，减少摇摆）。只读。

    含三类：已采纳（applied，附该段旧→新译文）、已拒绝（rejected，附理由与被拒建议）、
    以及归档的 `llm_suggestion` 原始建议。
    """
    from . import history as hist_mod
    from . import qa_queue as qqa

    out: list[dict] = []
    for it in qqa.load(cfg):
        if it.get("page") != rel:
            continue
        status = it.get("status")
        if status not in ("applied", "rejected"):
            continue
        h = {
            "segments": it.get("segments") or [],
            "status": status,
            "reason": it.get("reason", ""),
            "suggestion": it.get("suggestion", ""),
            "llm_suggestion": it.get("llm_suggestion", ""),
            "old_translation": "", "new_translation": "",
        }
        # applied：从段历史取该段最后一个非 qa-apply 与最后一个 qa-apply 版本译文
        if status == "applied":
            for sid in it.get("segments") or []:
                vers = hist_mod.versions(cfg, rel, str(sid))
                if not vers:
                    continue
                new_t = ""
                old_t = ""
                for v in reversed(vers):
                    if v.get("op") == "qa-apply" and not new_t:
                        new_t = (v.get("cache") or {}).get("translation", "") or ""
                    if v.get("op") != "qa-apply" and new_t and not old_t:
                        old_t = (v.get("cache") or {}).get("translation", "") or ""
                        break
                h["old_translation"], h["new_translation"] = old_t, new_t
                break
        out.append(h)
    return out


def run_qa(cfg: Config, client, rel: str) -> list[dict]:
    """对单页做 QA，返回标准化问题列表。

    每项：{severity, reason, src_quote, dst_quote, suggestion, segments, resolved}。
    """
    segs = seg_mod.segments_for_page(cfg, rel)
    issues: list[dict] = []
    gl_confirmed = gl.all_confirmed(cfg)
    history_block = ""
    if cfg.get("qa", "inject_history", default=False):
        history_block = prompts.build_qa_history_block(build_history(cfg, rel))
    for s in segs:
        if s.translation is None:
            continue
        # HTML 安全：占位符必须全部保留且未被破坏
        ph_open = cfg.get("segments", "placeholder_open", default="[[P")
        n_ph = s.text.count(ph_open)
        n_ph_out = s.translation.count(ph_open)
        if n_ph != n_ph_out:
            issues.append({
                "severity": "high",
                "reason": f"占位符数量不一致（原文 {n_ph}，译文 {n_ph_out}）",
                "src_quote": s.text, "dst_quote": s.translation,
                "suggestion": "检查内联标签是否被删除或改动",
                "segments": [s.id], "resolved": True,
            })
        # 术语一致性：译文应包含已确认术语的译文
        for g in gl_confirmed:
            if g["src"] in s.text and g["dst"] not in s.translation and s.translation:
                issues.append({
                    "severity": "mid",
                    "reason": f"术语「{g['src']}」未按词汇表译为「{g['dst']}」",
                    "src_quote": g["src"], "dst_quote": s.translation,
                    "suggestion": f"核对术语译文（应为「{g['dst']}」）",
                    "segments": [s.id], "resolved": True,
                })

    # LLM 深度检查
    deep = cfg.get("qa", "deep_llm_check", default=True)
    if deep:
        src_all = "\n".join(s.text for s in segs if s.translation)
        dst_all = "\n".join(s.translation or "" for s in segs if s.translation)
        if src_all.strip():
            user_rules = tr.effective_user_rules(cfg)
            audit_policy = cfg.get("qa", "reduce_style_reports", default=True)
            sysp = prompts.build_qa_system(cfg, user_rules, audit_policy=audit_policy)
            usr = prompts.build_qa_user(src_all[:6000], dst_all[:6000], gl_confirmed,
                                        history_block=history_block)
            try:
                eff = (cfg.get("qa", "reasoning_effort", default="")
                       or cfg.get("llm", "reasoning_effort", default=""))
                resp = client.chat(
                    sysp, usr, temperature=0.2, tag="qa",
                    reasoning_effort=eff or None)
                data = llm_mod.parse_json_response(resp)
                for iss in data.get("issues", []) or []:
                    src_quote = iss.get("src_quote", "") or ""
                    dst_quote = iss.get("dst_quote", "") or ""
                    found = locate_segments(segs, src_quote, dst_quote)
                    issues.append({
                        "severity": iss.get("severity", "mid"),
                        "reason": iss.get("reason", "") or iss.get("problem", ""),
                        "src_quote": src_quote,
                        "dst_quote": dst_quote,
                        "suggestion": iss.get("suggestion", ""),
                        "segments": found,
                        "resolved": bool(found),
                    })
            except llm_mod.LLMError as e:
                print(f"  ⚠ {rel}: LLM 深度检查失败（已跳过）: {e}", flush=True)
    return issues


def new_report(ts: str, scope: str) -> dict:
    """构造 version 2 空报告（供 qa_report 与 qa-auto 共用）。"""
    return {
        "version": 2,
        "ts": ts,
        "scope": scope,
        "pages": {},
        "checked": {},
        "total_issues": 0,
        "high": 0,
        "unresolved": 0,
        "total_pages_checked": 0,
    }


def record_checked(report: dict, rel: str, issues: list, duration_s: float,
                   deep: bool, ts: str = "") -> None:
    """把一页的 QA 结果并入报告（checked + pages + 聚合），原地修改。"""
    n_high = sum(1 for x in issues if x.get("severity") == "high")
    n_mid = sum(1 for x in issues if x.get("severity") == "mid")
    n_low = sum(1 for x in issues if x.get("severity") == "low")
    n_unres = sum(1 for x in issues if not x.get("resolved", True))
    report.setdefault("checked", {})[rel] = {
        "ts": ts or report.get("ts", ""), "total": len(issues), "high": n_high,
        "mid": n_mid, "low": n_low, "unresolved": n_unres, "deep": deep,
        "duration_s": round(duration_s, 1),
    }
    report["total_pages_checked"] = report.get("total_pages_checked", 0) + 1
    if issues:
        report.setdefault("pages", {})[rel] = issues
        report["total_issues"] = report.get("total_issues", 0) + len(issues)
        report["high"] = report.get("high", 0) + n_high
        report["unresolved"] = report.get("unresolved", 0) + n_unres


def qa_report(cfg: Config, client, rels: list[str],
              out: str | None = None, ts: str = "",
              scope: str = "") -> dict:
    """对给定页面列表执行 QA，返回报告。

    ``out`` 非空时写入该路径（含时间戳的报告文件），并同步刷新稳定的
    ``work/qa_report.json`` 别名；缺省则仅写 ``work/qa_report.json``。
    无论何种方式，均逐页增量落盘。

    报告结构（version 2）：
    - ``pages``：仅有问题的页 → issue 列表（向后兼容）。
    - ``checked``：**每个被检查的页**（含 0 问题）→ ``{ts,total,high,mid,low,
      unresolved,deep,duration_s}``，用于聚合"每页 QA 状态/最近时间"。
    - ``ts``/``scope``/``total_pages_checked``：本次运行的元信息。
    """
    report = new_report(ts, scope)
    alias = os.path.join(cfg.work_dir, "qa_report.json")
    deep = cfg.get("qa", "deep_llm_check", default=True)
    total = len(rels)
    for i, rel in enumerate(rels, 1):
        t0 = time.monotonic()
        issues = run_qa(cfg, client, rel)
        dt = time.monotonic() - t0
        record_checked(report, rel, issues, dt, deep, ts)
        util.write_json(alias, report)  # 增量落盘，长跑中断不丢失
        if out:
            util.write_json(out, report)
        print(f"[{i}/{total}] {rel}  {len(issues)} 问题 ({dt:.1f}s)", flush=True)
    return report


def collect_page_status(cfg: Config) -> dict:
    """扫描 ``qa_reports/*.json``，聚合每页最近一次 QA 状态（只读）。

    返回 ``{rel: {"ts","total","high","mid","low","unresolved","deep",
    "duration_s"}}``，取每页 ``checked`` 中 ``ts`` 最大者。

    兼容 version 1（无 ``checked``）的旧报告：退化为从 ``pages``（仅有问题
    的页）推断，字段以 ``ts`` 与 ``total/high`` 为准，其余缺省。旧报告不含
    0 问题页，故该页的"已 QA"未必可查——新报告（version 2）才完整。
    """
    report_dir = cfg.get("qa", "report_dir", default="work/qa_reports")
    alias = os.path.join(cfg.work_dir, "qa_report.json")
    files = []
    if os.path.isdir(report_dir):
        files += [os.path.join(report_dir, f) for f in os.listdir(report_dir)
                  if f.startswith("qa_") and f.endswith(".json")]
    if os.path.exists(alias):
        files.append(alias)

    latest: dict[str, dict] = {}
    for path in files:
        data = util.read_json(path, {})
        if not isinstance(data, dict):
            continue
        ts = str(data.get("ts") or "")
        if not ts:
            m = re.search(r"qa_(\d{8}_\d{6})", os.path.basename(path))
            ts = m.group(1) if m else ""
        checked = data.get("checked") or {}
        if checked:
            for rel, info in checked.items():
                rec = dict(info)
                rec.setdefault("ts", ts)
                if rel not in latest or str(rec.get("ts", "")) >= str(latest[rel].get("ts", "")):
                    latest[rel] = rec
        else:
            # 旧报告：仅有问题的页
            for rel, issues in (data.get("pages") or {}).items():
                n_high = sum(1 for x in issues if x.get("severity") == "high")
                rec = {"ts": ts, "total": len(issues), "high": n_high,
                       "mid": sum(1 for x in issues if x.get("severity") == "mid"),
                       "low": sum(1 for x in issues if x.get("severity") == "low"),
                       "unresolved": sum(1 for x in issues if not x.get("resolved", True)),
                       "deep": None, "duration_s": None}
                if rel not in latest or str(ts) >= str(latest[rel].get("ts", "")):
                    latest[rel] = rec
    return latest
