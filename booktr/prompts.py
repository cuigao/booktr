"""Prompt 模板集中管理。

所有模板均为函数，接收结构化参数，便于用户通过 config 注入额外规则。
"""
from __future__ import annotations

from . import util

SRC_LANG_NAME = {"ja": "日语（日本原文）", "en": "英语", "zh": "中文"}


def lang_name(code: str) -> str:
    return SRC_LANG_NAME.get(code, code)


def build_translate_system(
    cfg,
    glossary: list[dict],
    style_guide: str,
    user_rules: str,
    focus: str,
) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    src = cfg.get("lang", "source", default="ja")
    parts = [
        f"你是一名资深译者，负责把{lang_name(src)}网站内容翻译成{lang_name(tgt)}。",
        "翻译要求：",
        "- 忠实传达原意，保持原文的语气、人称与个人色彩",
        "- 目标语言需自然流畅，不要逐字硬译",
        "- 输出严格为 JSON，只输出 JSON 本身，不要任何额外文字或 markdown 围栏",
    ]
    if glossary:
        gl_lines = []
        for g in glossary:
            gl_lines.append(
                f"- {g['src']} → {g['dst']}"
                + (f"（{g.get('note','')}）" if g.get("note") else "")
                + (f" [{g.get('category','')}]" if g.get("category") else "")
            )
        parts.append("## 词汇表（必须遵循，翻译专名时优先使用）\n" + "\n".join(gl_lines))
    if style_guide:
        parts.append(f"## 风格指南\n{style_guide}")
    if focus:
        parts.append(f"## 译者关注点\n{focus}")
    if user_rules:
        parts.append(f"## 用户附加规则\n{user_rules}")
    parts.append(
        "## 输出格式\n"
        '返回 JSON：{"translation": "译文", "confidence": 0到1, '
        '"glossary_conflicts": ["发现问题的术语条目"], '
        '"notes": ["需要记录的重要/存疑信息"], "needs_human": true/false}'
    )
    return "\n\n".join(parts)


def build_translate_user(
    cfg,
    src_text: str,
    page_ctx: str,
    prior_ctx: str,
    exemplars: list[dict],
    tm_hits: list[dict],
) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    parts = [f"请将下面的{_srcname(cfg)}翻译成{tgt}。"]
    if page_ctx:
        parts.append(f"## 当前页面上下文\n{page_ctx}")
    if prior_ctx:
        parts.append(f"## 前文上下文（保持叙事与术语一致）\n{prior_ctx}")
    if tm_hits:
        tm_lines = []
        for h in tm_hits:
            tm_lines.append(f"{h['src']} → {h['dst']}")
        parts.append("## 翻译记忆命中（可参考，但优先词汇表）\n" + "\n".join(tm_lines))
    if exemplars:
        ex_lines = []
        for e in exemplars:
            ex_lines.append(f"原文：{e['src']}\n参考译文：{e['dst']}")
        parts.append(
            "## 风格参照样例（仅模仿其风格与措辞倾向，勿照抄内容）\n"
            + "\n\n".join(ex_lines)
        )
    parts.append("## 待翻译文本\n|TEXT|\n" + src_text)
    return "\n\n".join(parts)


def _srcname(cfg) -> str:
    return lang_name(cfg.get("lang", "source", default="ja"))


def build_glossary_extract_system(cfg) -> str:
    return (
        "你是术语抽取助手。从给定的日文文本中识别专有名词与值得进入词汇表的术语。\n"
        "专名包括：人名、团体名、歌曲/专辑名、节目名、地名、作品名、特有的固定译法等。\n"
        "输出 JSON：{\"terms\": [{\"src\": \"原文\", \"dst\": \"建议译文\", "
        "\"category\": \"person|song|album|show|place|term|other\", "
        "\"confidence\": 0到1, \"note\": \"依据说明\"}]}\n"
        "只输出 JSON，不要额外文字。"
    )


def build_glossary_extract_user(text: str) -> str:
    return "## 待分析文本\n|TEXT|\n" + text


def build_summary_system(cfg) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    return (
        f"你是一名网站内容分析师。请用{tgt}概括给定页面内容，用于后续翻译的上下文参考。\n"
        "输出 JSON：{\"summary\": \"不超过200字的概要\", "
        "\"entities\": [\"出现的重要专名/人名/事件\"], "
        "\"content_type\": \"日记|介绍|推荐|通告|索引|其他\"}\n"
        "只输出 JSON。"
    )


def build_summary_user(src_text: str) -> str:
    return "## 页面文本\n|TEXT|\n" + src_text


def build_style_guide_system(cfg) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    return (
        f"你是一名翻译风格分析师。下面给出若干组\"{_srcname(cfg)}原文 — {tgt}参考译文\"对照。\n"
        "请提炼出稳定、可复用的翻译风格规则（人称与称谓、语气、句式长短倾向、"
        "术语处理、句末语气词、标点习惯等），供后续翻译参考。\n"
        f"输出为纯文本规则列表（每行一条，使用{tgt}），不要 JSON。"
    )


def build_style_guide_user(refs: list[dict]) -> str:
    lines = []
    for i, r in enumerate(refs, 1):
        lines.append(f"### 样例 {i}" + (f"（来源：{r['source']}）" if r.get("source") else ""))
        lines.append(f"原文：{r['src']}")
        lines.append(f"参考译文：{r['dst']}")
        lines.append("")
    return "\n".join(lines)


def build_qa_system(cfg) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    return (
        f"你是翻译质量审核员。检查译文是否：1)术语与词汇表一致 2)未泄漏HTML标签/占位符 "
        "3)漏译、误译、生硬处。\n"
        f"输出 JSON：{{\"issues\": [{{\"segment_id\": 数字, \"problem\": \"描述\", "
        f"\"suggestion\": \"修改建议\", \"severity\": \"high|mid|low\"}}]}}\n"
        f"没有问题则 issues 为空数组。只输出 JSON。"
    )


def build_qa_user(src_text: str, dst_text: str, glossary: list[dict]) -> str:
    gl = "\n".join(f"- {g['src']} → {g['dst']}" for g in glossary) or "(空)"
    return (
        f"## 词汇表\n{gl}\n"
        f"## 原文\n|TEXT|\n{src_text}\n\n"
        f"## 译文\n|DST|\n{dst_text}"
    )


def build_translator_note_system(cfg) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    return (
        f"你是网站研究的译者注作者。基于给定页面的译文与全站摘要，发现值得向读者交代的"
        f"前后文关联、创作背景、历史考据或趣味细节。\n"
        f"输出 JSON：{{\"notes\": [{{\"content\": \"{tgt}的译者注内容\", "
        f"\"related_pages\": [\"关联页面路径\"], \"type\": "
        f"\"前文呼应|创作背景|历史考据|趣味细节|其他\"}}]}}\n"
        "只输出 JSON。没有可写的就返回空数组。"
    )


def build_translator_note_user(page_rel: str, translated: str, summaries: dict) -> str:
    sum_lines = [f"- {k}: {v}" for k, v in summaries.items()] if summaries else "(无摘要)"
    return (
        f"## 当前页面\n{page_rel}\n## 页面译文\n|TEXT|\n{translated}\n\n"
        f"## 全站其他页面摘要（用于发现关联）\n" + "\n".join(sum_lines)
    )


def build_survey_system(cfg) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    return (
        f"你是网站结构分析师。分析全站页面清单与标题，判断每页的主题、类型与页面间可能存在的"
        f"语义关联（即使没有显式超链接）。\n"
        f"输出 JSON：{{\"pages\": [{{\"rel\": \"页面路径\", \"theme\": \"主题概括\", "
        f"\"content_type\": \"...\", \"priority\": 0到1}}], "
        f"\"relations\": [{{\"from\": \"页面A\", \"to\": \"页面B\", "
        f"\"reason\": \"关联原因\"}}]}}\n"
        f"全部用{tgt}表达。只输出 JSON。"
    )


def build_survey_user(page_list: str) -> str:
    return f"## 页面清单（路径 | 标题）\n{page_list}"
