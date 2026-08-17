"""HTML 文本段切分。

在原始解码文本上定位可翻译文字段（字符偏移），段内内联标签转占位符，
翻译后原位拼回，保证标签/属性/注释/空白等字节不变。
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

from . import util
from .config import Config

_TAG_RE = re.compile(
    r"(?P<comment><!--.*?-->)|(?P<decl><!DOCTYPE[^>]*>)|(?P<tag><\/?[A-Za-z][^>]*>)",
    re.I | re.S,
)

_ATTR_RE = re.compile(
    r'\b(?:alt|title)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.I
)


@dataclass
class Segment:
    id: int
    kind: str  # text | attr_title | attr_alt | head_title
    start: int
    end: int
    text: str  # 源文本（占位符已替换为 token）
    placeholders: dict[str, str] = field(default_factory=dict)  # token -> 原始片段
    translation: str | None = None
    confidence: float | None = None
    needs_human: bool = False
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_text: str = ""  # 未替换占位符的原文（用于溯源）

    def to_dict(self) -> dict:
        return asdict(self)


def _placeholder(ph_open: str, ph_close: str, idx: int) -> str:
    return f"{ph_open}{idx}{ph_close}"


def split_segments(html: str, cfg: Config) -> list[Segment]:
    """将 HTML 文本切分为可翻译段，含字符偏移。"""
    block_tags = set(cfg.get("segments", "block_tags", default=[]))
    ph_open = cfg.get("segments", "placeholder_open", default="⟪")
    ph_close = cfg.get("segments", "placeholder_close", default="⟫")
    translate_alt = cfg.get("segments", "translate_alt", default=True)
    translate_title = cfg.get("segments", "translate_title", default=True)
    min_len = cfg.get("segments", "min_text_len", default=1)

    segments: list[Segment] = []
    seg_id = [0]
    body = html
    title_text = _extract_head_title(html)
    if title_text is not None and translate_title and title_text.strip():
        t_start = html.find(title_text)
        s = _make_segment(
            seg_id, kind="head_title", text=title_text,
            start=t_start, end=t_start + len(title_text),
            placeholders={}, cfg=cfg,
        )
        if s:
            segments.append(s)

    # 遍历标签，收集文本 token
    pos = 0
    # 当前累积文本段：文本块与内联标签的混合
    cur_chunks: list[tuple[str, int, int]] = []  # (kind, start, end)
    in_script_style = False

    def flush() -> None:
        nonlocal cur_chunks
        if not cur_chunks:
            return
        # 无实质文字内容则丢弃
        text_joined = "".join(html[c[1] : c[2]] for c in cur_chunks if c[0] == "text")
        if not any(ch.strip() for ch in text_joined):
            cur_chunks = []
            return
        # 构造带占位符的文本（相邻 inline 标签合并为一个占位符）
        ph_map: dict[str, str] = {}
        out_parts: list[str] = []
        ph_idx = [0]
        start = cur_chunks[0][1]
        end = cur_chunks[-1][2]
        i = 0
        nchunks = len(cur_chunks)
        while i < nchunks:
            kind, s, e = cur_chunks[i]
            if kind == "text":
                out_parts.append(html[s:e])
                i += 1
            else:
                # 合并连续的 tag 块
                tag_start = s
                tag_end = e
                i += 1
                while i < nchunks and cur_chunks[i][0] == "tag":
                    tag_end = cur_chunks[i][2]
                    i += 1
                token = _placeholder(ph_open, ph_close, ph_idx[0])
                ph_idx[0] += 1
                ph_map[token] = html[tag_start:tag_end]
                out_parts.append(token)
        display = "".join(out_parts)
        if len(display.strip()) < min_len:
            cur_chunks = []
            return
        seg = _make_segment(
            seg_id, kind="text", text=display, start=start, end=end,
            placeholders=ph_map, cfg=cfg, source_text=text_joined,
        )
        if seg:
            segments.append(seg)
        cur_chunks = []

    # 主体扫描
    i = 0
    n = len(html)
    in_tag_until = -1
    while i < n:
        m = _TAG_RE.match(html, i)
        if m:
            token = m.group(0)
            if m.group("comment") is not None:
                # 注释强制分段边界
                flush()
            elif m.group("decl") is not None:
                pass
            elif m.group("tag") is not None:
                tagname, is_end, self_closing = _parse_tag(token)
                if tagname in ("script", "style"):
                    flush()
                    # 跳过脚本/样式内容到闭合标签
                    m_end = re.search(rf"</{tagname}\s*>", html[i:], re.I)
                    if not m_end:
                        end = n
                    else:
                        end = i + m_end.end()
                    i = end
                    continue
                if tagname == "title" and not is_end:
                    # title 内容由 head_title 段单独处理，主循环跳过
                    m_end = re.search(r"</title\s*>", html[i:], re.I)
                    if not m_end:
                        end = n
                    else:
                        end = i + m_end.end()
                    i = end
                    continue
                if is_end:
                    if tagname in block_tags:
                        flush()
                    else:
                        # 内联闭合标签也作为占位符保留，避免被段替换覆盖
                        cur_chunks.append(("tag", i, i + len(token)))
                else:
                    if tagname in block_tags:
                        flush()
                    elif self_closing:
                        # 自闭合内联标签（如 <img>）加入当前段为占位符
                        cur_chunks.append(("tag", i, i + len(token)))
                    else:
                        # 普通内联开标签
                        cur_chunks.append(("tag", i, i + len(token)))
            i = m.end()
        else:
            # 文本 token
            nxt = _TAG_RE.search(html, i)
            j = nxt.start() if nxt else n
            chunk = html[i:j]
            if chunk:
                cur_chunks.append(("text", i, j))
            i = j
    flush()

    # 属性段（alt/title 属性值）
    if translate_alt or translate_title:
        for m in _ATTR_RE.finditer(html):
            attr = m.group(0)
            value = m.group(1) or m.group(2) or m.group(3) or ""
            if not value.strip():
                continue
            # 定位值范围
            if m.group(1) is not None:
                vs, ve = m.start(1), m.end(1)
            elif m.group(2) is not None:
                vs, ve = m.start(2), m.end(2)
            else:
                vs, ve = m.start(3), m.end(3)
            kind = "attr_alt" if "alt" in attr[:6].lower() else "attr_title"
            if kind == "attr_alt" and not translate_alt:
                continue
            if kind == "attr_title" and not translate_title:
                continue
            if not _contains_cjk(value):
                continue
            seg = _make_segment(
                seg_id, kind=kind, text=value, start=vs, end=ve,
                placeholders={}, cfg=cfg, source_text=value,
            )
            if seg:
                segments.append(seg)

    return segments


def _make_segment(
    seg_id: list[int], kind: str, text: str, start: int, end: int,
    placeholders: dict, cfg: Config, source_text: str = "",
) -> Segment | None:
    if not text or not text.strip():
        return None
    seg_id[0] += 1
    return Segment(
        id=seg_id[0], kind=kind, start=start, end=end, text=text,
        placeholders=placeholders, source_text=source_text,
    )


def _parse_tag(token: str) -> tuple[str, bool, bool]:
    inner = token.strip("<>/")
    is_end = token.startswith("</")
    self_closing = inner.endswith("/") or inner in ("br", "hr", "img", "input", "meta", "link", "wbr")
    tagname = re.split(r"[\s/>]", inner)[0].lower()
    return tagname, is_end, self_closing


_INLINE = {
    "a", "b", "i", "u", "em", "strong", "small", "big", "font", "span", "sub", "sup",
    "strike", "s", "tt", "code", "br", "img", "abbr", "acronym", "cite", "label",
}


def _extract_head_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    return re.sub(r"<[^>]+>", "", m.group(1))


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text))


def segments_for_page(cfg: Config, rel: str) -> list[Segment]:
    """读取/生成某页的段列表，并缓存到 work/segments/。"""
    seg_path = os.path.join(cfg.get("segments_dir", default=""), rel.replace("/", "__") + ".json")
    if os.path.exists(seg_path):
        data = util.read_json(seg_path, [])
        field_names = {f.name for f in Segment.__dataclass_fields__.values()}
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            clean = {k: v for k, v in d.items() if k in field_names}
            out.append(Segment(**clean))
        return out
    # 重新切分
    from .crawler import resolve_local_path
    raw = open(resolve_local_path(cfg, rel), "rb").read()
    decoded, _ = util.decode_html(raw)
    segs = split_segments(decoded, cfg)
    os.makedirs(os.path.dirname(seg_path) or ".", exist_ok=True)
    util.write_json(seg_path, [s.to_dict() for s in segs])
    return segs


def reassemble(html: str, segments: list[Segment]) -> str:
    """将翻译结果拼回原文。按 start 倒序替换，避免偏移失效。"""
    out = html
    for seg in sorted(segments, key=lambda s: s.start, reverse=True):
        if seg.translation is None:
            continue
        replacement = _restore_placeholders(seg.translation, seg.placeholders)
        out = out[: seg.start] + replacement + out[seg.end :]
    return out


def _restore_placeholders(text: str, ph_map: dict[str, str]) -> str:
    """将占位符换回原始内联标签。"""
    for token, raw in ph_map.items():
        text = text.replace(token, raw)
    return text


def write_page_output(cfg: Config, rel: str, html: str) -> None:
    """写出翻译后的页面（统一 UTF-8 并保证 charset meta）。"""
    out = _ensure_charset(html)
    out_path = os.path.join(cfg.output_dir, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(out)


def _ensure_charset(html: str) -> str:
    """补/改 <meta charset> 为 UTF-8。"""
    if re.search(r'<meta[^>]+charset\s*=\s*["\']?utf-8', html, re.I):
        return html
    m = re.search(r"<meta[^>]+charset\s*=\s*[\"']?[A-Za-z0-9_\-]+", html, re.I)
    if m:
        return html[: m.start()] + '<meta charset="utf-8">' + html[m.end() :]
    head = re.search(r"<head[^>]*>", html, re.I)
    if head:
        return html[: head.end()] + '<meta charset="utf-8">' + html[head.end() :]
    return html
