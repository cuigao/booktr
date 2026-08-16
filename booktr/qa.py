"""一致性 QA pass：LLM 校验术语一致性与 HTML 安全。"""
from __future__ import annotations

import os

from . import glossary as gl
from . import llm as llm_mod
from . import prompts
from . import segments as seg_mod
from . import util
from .config import Config


def run_qa(cfg: Config, client, rel: str) -> list[dict]:
    """对单页做 QA，返回问题列表。"""
    segs = seg_mod.segments_for_page(cfg, rel)
    issues = []
    gl_confirmed = gl.all_confirmed(cfg)
    for s in segs:
        if s.translation is None:
            continue
        # HTML 安全：占位符必须全部保留且未被破坏
        ph_open = cfg.get("segments", "placeholder_open", default="[[P")
        ph_close = cfg.get("segments", "placeholder_close", default="]]")
        n_ph = s.text.count(ph_open)
        n_ph_out = s.translation.count(ph_open)
        if n_ph != n_ph_out:
            issues.append(
                {"segment_id": s.id, "severity": "high",
                 "problem": f"占位符数量不一致（原文 {n_ph}，译文 {n_ph_out}）",
                 "suggestion": "检查内联标签是否被删除或改动"}
            )
        # 术语一致性：译文应包含已确认术语的译文
        for g in gl_confirmed:
            if g["src"] in s.text and g["dst"] not in s.translation and s.translation:
                issues.append(
                    {"segment_id": s.id, "severity": "mid",
                     "problem": f"术语「{g['src']}」未按词汇表译为「{g['dst']}」",
                     "suggestion": "核对术语译文"}
                )

    # LLM 深度检查（可配置，默认对长段执行）
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
                    issues.append(
                        {"segment_id": iss.get("segment_id", 0),
                         "severity": iss.get("severity", "mid"),
                         "problem": iss.get("problem", ""),
                         "suggestion": iss.get("suggestion", "")}
                    )
            except llm_mod.LLMError:
                pass
    return issues


def qa_report(cfg: Config, client, rels: list[str]) -> dict:
    report = {"pages": {}, "total_issues": 0, "high": 0}
    for rel in rels:
        issues = run_qa(cfg, client, rel)
        if issues:
            report["pages"][rel] = issues
            report["total_issues"] += len(issues)
            report["high"] += sum(1 for i in issues if i["severity"] == "high")
    out = os.path.join(cfg.work_dir, "qa_report.json")
    util.write_json(out, report)
    return report
