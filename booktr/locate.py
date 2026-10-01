"""段落定位：按原文/译文片段在页面内定位翻译段落。

人无法迅速知道一段文本位于哪个翻译段落，但从渲染页面上复制一段原文/译文
片段很容易。本模块提供通用的机械定位：给定页面与片段，返回候选段号及分数。

匹配优先级（与 QA 定位一致，单一口径）：
  1/2/3. 原文或译文（含去占位符变体）精确子串
  4.    引文按换行拆分后逐行命中（跨段引用）
  5.    整段模糊（Dice ≥ 阈值）
"""
from __future__ import annotations

import re

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


def locate(
    segs: list,
    src_frag: str = "",
    dst_frag: str = "",
    include_untranslated: bool = False,
    top: int = 5,
) -> list[dict]:
    """在段列表中定位引用片段。

    返回列表（按匹配质量排序，最多 top 项）：
    ``{sid, score, method, src, dst}``，method ∈
    ``exact`` / ``strip_ph`` / ``line_split`` / ``fuzzy``。

    ``include_untranslated``：默认仅检索已翻译段（与 QA 一致）；置 True 时
    纳入所有含文本的段（即使尚无译文），便于在重译前定位。
    """
    src_frag = src_frag or ""
    dst_frag = dst_frag or ""
    cand = [
        s for s in segs
        if ((getattr(s, "translation", None) or include_untranslated)
            and ((getattr(s, "text", "") or "") or (getattr(s, "translation", "") or "")))
    ]
    if not cand:
        return []
    if not _norm(src_frag) and not _norm(dst_frag):
        return []

    # 1/2/3. 原文或译文（含去占位符变体）子串命中
    for method, strip in (("exact", False), ("strip_ph", True)):
        hits = [
            s for s in cand
            if _matches(src_frag, s.text or "", strip_ph=strip)
            or _matches(dst_frag, s.translation or "", strip_ph=strip)
        ]
        if hits:
            return _results(hits, method)

    # 4. 引文按换行拆分，逐行命中（跨段引用）
    quote_lines = [ln for ln in src_frag.splitlines() if _norm(ln)]
    if len(quote_lines) > 1:
        hits = [s for s in cand if any(_matches(ln, s.text or "") for ln in quote_lines)]
        if hits:
            return _results(hits, "line_split")

    # 5. 模糊匹配（整段 Dice），返回 top 个候选
    target = _strip_ph(src_frag) or _strip_ph(dst_frag)
    if target:
        scored = []
        for s in cand:
            hay = _strip_ph(s.text or "") or _strip_ph(s.translation or "")
            sc = util.dice_coefficient(hay, target)
            if sc >= _FUZZY_THRESHOLD:
                scored.append((sc, s))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            out = []
            for sc, s in scored[: max(1, top)]:
                out.append({"sid": s.id, "score": round(sc, 3), "method": "fuzzy",
                            "src": (s.text or "")[:200],
                            "dst": (s.translation or "")[:200]})
            return out
    return []


def _results(hits: list, method: str) -> list[dict]:
    return [{"sid": s.id, "score": 1.0, "method": method,
             "src": (s.text or "")[:200],
             "dst": (s.translation or "")[:200]} for s in hits]


def locate_for_page(
    cfg: Config,
    rel: str,
    src_frag: str = "",
    dst_frag: str = "",
    include_untranslated: bool = False,
    top: int = 5,
) -> list[dict]:
    """读取页面段列表并定位片段。"""
    from . import segments as seg_mod

    segs = seg_mod.segments_for_page(cfg, rel)
    return locate(segs, src_frag=src_frag, dst_frag=dst_frag,
                  include_untranslated=include_untranslated, top=top)
