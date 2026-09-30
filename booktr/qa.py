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
from . import util
from .config import Config

_FUZZY_THRESHOLD = 0.6


def _norm(text: str) -> str:
    return util.normalize_ws(text or "")


def _strip_ph(text: str) -> str:
    return _norm(re.sub(r"\[\[P\d+\]\]", "", text or ""))


def _matches(quote: str, hay: str, strip_ph: bool = False) -> bool:
    if not quote:
        return False
    q = _strip_ph(quote) if strip_ph else _norm(quote)
    h = _strip_ph(hay) if strip_ph else _norm(hay)
    return bool(q) and q in h


def locate_segments(segs: list, src_quote: str, dst_quote: str) -> list[int]:
    """把 QA 引用的原文/译文片段机械定位到段号。

    匹配优先级：原文精确子串 → 译文精确子串 → 去占位符后子串 →
    引文按换行拆分后逐行命中 → 模糊（Dice ≥ 阈值）。
    返回命中的段 id 列表（可能为空）。
    """
    text_segs = [s for s in segs if getattr(s, "kind", "") == "text"
                 and (s.text or s.translation)]
    if not text_segs:
        return []

    # 1/2/3. 原文或译文（含去占位符变体）子串命中
    hits = []
    for s in text_segs:
        if _matches(src_quote, s.text or "") or _matches(dst_quote, s.translation or ""):
            hits.append(s.id)
    if hits:
        return hits
    for s in text_segs:
        if _matches(src_quote, s.text or "", strip_ph=True) \
                or _matches(dst_quote, s.translation or "", strip_ph=True):
            hits.append(s.id)
    if hits:
        return hits

    # 4. 引文按换行拆分，逐行命中（跨段引用）
    quote_lines = [ln for ln in (src_quote or "").splitlines() if _norm(ln)]
    if len(quote_lines) > 1:
        for s in text_segs:
            if any(_matches(ln, s.text or "") for ln in quote_lines):
                hits.append(s.id)
        if hits:
            return hits

    # 5. 模糊匹配（整段 Dice）
    target = _strip_ph(src_quote or "") or _strip_ph(dst_quote or "")
    if target:
        scored = [(util.dice_coefficient(_strip_ph(s.text or ""), target), s.id)
                  for s in text_segs]
        best = max(scored, key=lambda x: x[0]) if scored else (0.0, None)
        if best[0] >= _FUZZY_THRESHOLD:
            return [best[1]]
    return []


def run_qa(cfg: Config, client, rel: str) -> list[dict]:
    """对单页做 QA，返回标准化问题列表。

    每项：{severity, reason, src_quote, dst_quote, suggestion, segments, resolved}。
    """
    segs = seg_mod.segments_for_page(cfg, rel)
    issues: list[dict] = []
    gl_confirmed = gl.all_confirmed(cfg)
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
            sysp = prompts.build_qa_system(cfg)
            usr = prompts.build_qa_user(src_all[:6000], dst_all[:6000], gl_confirmed)
            try:
                resp = client.chat(sysp, usr, temperature=0.2, tag="qa")
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


def qa_report(cfg: Config, client, rels: list[str],
              out: str | None = None) -> dict:
    """对给定页面列表执行 QA，返回报告。

    ``out`` 非空时写入该路径（含时间戳的报告文件），并同步刷新稳定的
    ``work/qa_report.json`` 别名；缺省则仅写 ``work/qa_report.json``。
    无论何种方式，均逐页增量落盘。
    """
    report = {"pages": {}, "total_issues": 0, "high": 0, "unresolved": 0}
    alias = os.path.join(cfg.work_dir, "qa_report.json")
    total = len(rels)
    for i, rel in enumerate(rels, 1):
        t0 = time.monotonic()
        issues = run_qa(cfg, client, rel)
        dt = time.monotonic() - t0
        if issues:
            report["pages"][rel] = issues
            report["total_issues"] += len(issues)
            report["high"] += sum(1 for x in issues if x["severity"] == "high")
            report["unresolved"] += sum(1 for x in issues if not x.get("resolved", True))
        util.write_json(alias, report)  # 增量落盘，长跑中断不丢失
        if out:
            util.write_json(out, report)
        print(f"[{i}/{total}] {rel}  {len(issues)} 问题 ({dt:.1f}s)", flush=True)
    return report
