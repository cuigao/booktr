"""Prompt 模板集中管理。

所有模板均为函数，接收结构化参数，便于用户通过 config 注入额外规则。
"""
from __future__ import annotations

from . import util

LANG_NAMES = {
    "ja": "日语",
    "en": "英语",
    "zh": "中文",
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "it": "意大利语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
}


def lang_name(code: str) -> str:
    """语言代码转人类可读名称。支持 zh-Hans 等复合代码。"""
    if code in LANG_NAMES:
        return LANG_NAMES[code]
    main = code.split("-")[0]
    return LANG_NAMES.get(main, code)


def _build_retranslate_rules(src_name: str, tgt_name: str) -> str:
    """构建重新翻译规则（使用配置的源/目标语言名称）。"""
    return (
        "## 重新翻译任务（当收到\u201c重新翻译\u201d指令时适用）\n"
        "- 这是重新翻译任务，之前的翻译存在问题（可能未翻译或翻译不准确）\n"
        f"- 完整翻译为{tgt_name}，不要保留任何原文\n"
        "- 保持与前文/后文的术语和风格一致\n"
        "- 保留所有 [[Px]] 占位符\n"
        "- 保留全角写法（全角字母/数字/符号）、人名原形等规则仍然适用"
    )


def build_translate_system(
    cfg,
    glossary: list[dict],
    style_guide: str,
    user_rules: str,
    focus: str,
    is_retranslation: bool = False,
) -> str:
    tgt = cfg.get("lang", "target", default="zh-Hans")
    src = cfg.get("lang", "source", default="ja")
    src_name = lang_name(src)
    tgt_name = lang_name(tgt)
    parts = [
        f"你是一名资深译者，负责把{src_name}网站内容翻译成{tgt_name}。",
        "翻译要求：",
        "- 忠实传达原意，保持原文的语气、人称与个人色彩",
        "- 目标语言需自然流畅，不要逐字硬译",
        "- 输出严格为 JSON，只输出 JSON 本身，不要任何额外文字或 markdown 围栏",
        "- 保留原文中的全角写法：全角英文字母（Ａ-Ｚ, ａ-ｚ）、全角数字（０-９）、"
        "全角符号（！？～・＆＊＝＋＜＞等）保持全角不转半角，与 CJK 字符混排时全角形式融合更好；"
        "几何符号（●○■）、省略号（…）、破折号（――）、智能引号（\u201c\u201d\u2018\u2019）保持原样；"
        f"英文/拉丁字母不翻译；人名保留原形；其余{src_name}内容翻译为{tgt_name}",
        "- 译文中必须保留原文的所有 [[Px]] 占位符（如 [[P0]]、[[P1]]），"
        "它们是 HTML 标签的替代标记，翻译后需原样还原",
    ]
    if glossary:
        gl_lines = []
        for g in glossary:
            src = g.get("src", "")
            dst = g.get("dst", "")
            source = g.get("source", "")
            if source == "phrase":
                # 短语记忆条目
                gl_lines.append(f"- {src} → {dst} [参考译法]")
            elif g.get("read_only"):
                # 词汇表 confirmed 条目
                note = f"（{g.get('note', '')}）" if g.get("note") else ""
                gl_lines.append(f"- {src} → {dst}{note} [固定译法]")
            else:
                # 词汇表 auto-candidate 条目
                note = f"（{g.get('note', '')}）" if g.get("note") else ""
                cat = f" [{g.get('category', '')}]" if g.get("category") else ""
                gl_lines.append(f"- {src} → {dst}{note}{cat}")
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
    if is_retranslation:
        parts.append(_build_retranslate_rules(src_name, tgt_name))
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
    parts.append("### 待翻译文本\n\n" + src_text)
    return "\n\n".join(parts)


def build_translate_user_first(
    cfg,
    src_text: str,
    page_ctx: str,
    prior_ctx: str,
    exemplars: list[dict],
    tm_hits: list[dict],
    summary: str = "",
) -> str:
    """多轮对话首条消息：携带 page_ctx + 可选的前文翻译摘要。"""
    tgt = cfg.get("lang", "target", default="zh-Hans")
    parts = [f"请将下面的{_srcname(cfg)}翻译成{tgt}。"]
    if summary:
        parts.append(f"## 前文翻译摘要（保持术语与风格一致）\n{summary}")
    if page_ctx:
        parts.append(f"## 当前页面上下文\n{page_ctx}")
    if prior_ctx:
        parts.append(f"## 前文上下文（保持叙事与术语一致）\n{prior_ctx}")
    if tm_hits:
        tm_lines = [f"{h['src']} → {h['dst']}" for h in tm_hits]
        parts.append("## 翻译记忆命中（可参考，但优先词汇表）\n" + "\n".join(tm_lines))
    if exemplars:
        ex_lines = [f"原文：{e['src']}\n参考译文：{e['dst']}" for e in exemplars]
        parts.append(
            "## 风格参照样例（仅模仿其风格与措辞倾向，勿照抄内容）\n"
            + "\n\n".join(ex_lines)
        )
    parts.append("### 待翻译文本\n\n" + src_text)
    return "\n\n".join(parts)


def build_translate_user_subsequent(cfg, src_text: str) -> str:
    """多轮对话后续消息：仅携带待翻译文本。"""
    return f"### 待翻译文本\n\n{src_text}"


def build_retranslate_user(cfg, src_text: str, context: dict) -> str:
    """重新翻译时的用户消息（带上下文窗口）。"""
    tgt = cfg.get("lang", "target", default="zh-Hans")
    parts = [f"请将下面的{_srcname(cfg)}翻译成{tgt}。"]

    if context.get("page_ctx"):
        parts.append(f"## 页面上下文\n{context['page_ctx']}")
    if context.get("summary"):
        parts.append(f"## 页面摘要\n{context['summary']}")
    if context.get("context_before"):
        parts.append(f"## 前文（已翻译，保持术语与风格一致）\n{context['context_before']}")
    parts.append(f"## 待翻译文本\n\n{src_text}")
    if context.get("context_after"):
        parts.append(f"## 后文（已翻译，保持术语与风格一致）\n{context['context_after']}")

    return "\n\n".join(parts)


def build_conversation_summary(cfg) -> str:
    """多轮对话摘要 prompt（system）。"""
    return (
        "你是翻译助理。请总结以下翻译会话的要点，用于后续翻译的上下文参考。\n"
        "要求：\n"
        "- 本页主题与整体风格\n"
        "- 已使用的关键术语（如人名、专有名词的译法）\n"
        "- 翻译决策（如保留原文的部分、特殊处理）\n"
        "- 后续翻译需保持一致的要点\n"
        "控制在 200 字以内，用要点列表。只输出摘要文本，不要 JSON。"
    )


def build_conversation_summary_user(translations: list[str]) -> str:
    """多轮对话摘要 prompt（user）。"""
    lines = []
    for i, tr in enumerate(translations, 1):
        lines.append(f"[段{i}] {tr[:300]}")
    return "## 已翻译内容\n" + "\n\n".join(lines)


def _srcname(cfg) -> str:
    return lang_name(cfg.get("lang", "source", default="ja"))


def build_glossary_extract_system(cfg) -> str:
    src_name = lang_name(cfg.get("lang", "source", default="ja"))
    return (
        f"你是术语抽取助手。从给定的{src_name}文本中识别专有名词与值得进入词汇表的术语。\n"
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
