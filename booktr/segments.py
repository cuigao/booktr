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
    repaired: bool = False  # 该段经过 JSON 机械修复
    repair_methods: list[str] = field(default_factory=list)  # 修复方法（可追溯）

    def to_dict(self) -> dict:
        return asdict(self)


def _placeholder(ph_open: str, ph_close: str, idx: int) -> str:
    return f"{ph_open}{idx}{ph_close}"


_WS_RUN_RE = re.compile(r"[\s\u3000]+|&(?:nbsp|#160|#32);", re.I)

# 段间骨架标记块（Q）额外包含的特殊标签（其内容不翻译，整体作边界）
_Q_ATOMIC = {"script", "style", "title"}


def _tokenize(html: str) -> list[tuple[str, int, int]]:
    """把 HTML 扫描为 (kind, start, end) span：tag / comment / decl / ws / text。

    script/style/title 整个元素作为单个 tag span（内容不参与翻译）。
    ws 含半角空白、换行、\u3000 与 nbsp 实体。
    """
    tokens: list[tuple[str, int, int]] = []
    i = 0
    n = len(html)
    while i < n:
        m = _TAG_RE.match(html, i)
        if m:
            if m.group("comment") is not None:
                tokens.append(("comment", i, m.end()))
                i = m.end()
                continue
            if m.group("decl") is not None:
                tokens.append(("decl", i, m.end()))
                i = m.end()
                continue
            token = m.group(0)
            tagname, is_end, _sc = _parse_tag(token)
            if tagname in _Q_ATOMIC:
                close = re.search(rf"</{tagname}\s*>", html[m.end():], re.I)
                end = m.end() + close.end() if close else n
                tokens.append(("tag", i, end))
                i = end
                continue
            tokens.append(("tag", i, m.end()))
            i = m.end()
            continue
        # 文本/空白段，直到下一个标签
        nxt = _TAG_RE.search(html, i)
        j = nxt.start() if nxt else n
        k = i
        while k < j:
            wm = _WS_RUN_RE.match(html, k)
            if wm and wm.start() == k:
                tokens.append(("ws", k, wm.end()))
                k = wm.end()
            else:
                s = k
                while k < j:
                    wm2 = _WS_RUN_RE.match(html, k)
                    if wm2 and wm2.start() == k:
                        break
                    k += 1
                tokens.append(("text", s, k))
        i = j
    return tokens


def _pre_ranges(html: str) -> list[tuple[int, int]]:
    """定位 <pre>...</pre> 范围（含内部换行升格区域）。"""
    ranges = []
    for m in re.finditer(r"<pre[^>]*>(.*?)</pre>", html, re.I | re.S):
        ranges.append((m.start(), m.end()))
    return ranges


def _build_blocks(
    tokens: list[tuple[str, int, int]], html: str, block_tags: set[str],
    pre_ranges: list[tuple[int, int]],
) -> list[dict]:
    """合并标签块并分类 Q/P。

    每个块 = 连续「标签 + 邻接空白」（含 &nbsp;），向两侧吸收紧邻空白；
    相邻块间无 text 则合并。分类：含 block 标签/注释/声明 → Q（段间骨架）；
    仅 inline → P（段内占位符）。<pre> 内的换行空白块单独升格：
    单一 \\n → P，连续换行（中间即使有空白）→ Q。
    """
    n = len(tokens)
    ws_adjacent = [False] * n
    for idx, (k, _s, _e) in enumerate(tokens):
        if k == "ws":
            ws_adjacent[idx] = (
                (idx > 0 and tokens[idx - 1][0] != "text")
                or (idx + 1 < n and tokens[idx + 1][0] != "text")
            )

    blocks: list[dict] = []
    i = 0
    while i < n:
        k, s, e = tokens[i]
        if k in ("tag", "comment", "decl"):
            bs, be = s, e
            tag_kinds = [k]
            # 左向吸收所有连续 ws（含 &nbsp;），直至遇到非 ws
            j = i - 1
            while j >= 0 and tokens[j][0] == "ws":
                bs = tokens[j][1]
                j -= 1
            i += 1
            while i < n:
                k2, s2, e2 = tokens[i]
                if k2 in ("tag", "comment", "decl"):
                    tag_kinds.append(k2)
                    be = e2
                    i += 1
                elif k2 == "ws" and ws_adjacent[i]:
                    be = e2
                    i += 1
                else:
                    break
            blocks.append({
                "start": bs, "end": be, "tag_kinds": tag_kinds,
                "raw": html[bs:be], "pre_newline": None,
            })
        else:
            i += 1

    # <pre> 内的换行空白块升格（未被子标签块吸收的独立换行）
    covered = set()
    for b in blocks:
        for idx, (k, s, e) in enumerate(tokens):
            if k == "ws" and s >= b["start"] and e <= b["end"]:
                covered.add(idx)
    for idx, (k, s, e) in enumerate(tokens):
        if k != "ws" or idx in covered:
            continue
        in_pre = any(s >= ps and e <= pe for ps, pe in pre_ranges)
        if not in_pre:
            continue
        ws_text = html[s:e]
        nl_count = ws_text.count("\n")
        if nl_count <= 0:
            continue
        blocks.append({
            "start": s, "end": e, "tag_kinds": ["ws"],
            "raw": ws_text, "pre_newline": "Q" if nl_count >= 2 else "P",
        })

    # 分类 Q/P
    q_tags = block_tags | _Q_ATOMIC
    for b in blocks:
        if b["pre_newline"] is not None:
            b["is_q"] = (b["pre_newline"] == "Q")
            continue
        b["is_q"] = False
        for k in b["tag_kinds"]:
            if k in ("comment", "decl"):
                b["is_q"] = True
                break
            if k == "tag":
                pass
        if b["is_q"]:
            continue
        for tag_match in _TAG_RE.finditer(b["raw"]):
            token = tag_match.group(0)
            if tag_match.group("tag"):
                tn, _ie, _sc = _parse_tag(token)
                if tn in q_tags:
                    b["is_q"] = True
                    break
    return blocks


def split_segments(html: str, cfg: Config) -> list[Segment]:
    """将 HTML 文本切分为可翻译段，含字符偏移。

    Q/P 标签块模型：
      - Q = 段间骨架（block 标签/注释/声明/script/style/title，不进 LLM）
      - P = 段内占位符（inline 标签 + 邻接空白，进 LLM）
      - 段文本首尾绝无空白与标签（start/end 收缩到正文边界）。
    """
    block_tags = set(cfg.get("segments", "block_tags", default=[]))
    ph_open = cfg.get("segments", "placeholder_open", default="⟪")
    ph_close = cfg.get("segments", "placeholder_close", default="⟫")
    translate_alt = cfg.get("segments", "translate_alt", default=True)
    translate_title = cfg.get("segments", "translate_title", default=True)
    min_len = cfg.get("segments", "min_text_len", default=1)

    segments: list[Segment] = []
    seg_id = [0]
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

    tokens = _tokenize(html)
    pre_ranges = _pre_ranges(html)
    blocks = _build_blocks(tokens, html, block_tags, pre_ranges)

    # 生成扁平 item 流：Q / P / TEXT / WS（去重，按 start 升序）
    items: list[tuple[str, int, int, str]] = []
    cursor = 0
    covered: set[int] = set()
    for b in sorted(blocks, key=lambda x: x["start"]):
        for idx, (k, s, e) in enumerate(tokens):
            if k in ("tag", "comment", "decl", "ws") and s >= b["start"] and e <= b["end"]:
                covered.add(idx)
    for idx, (k, s, e) in enumerate(tokens):
        if idx in covered:
            continue
        if k == "text":
            items.append(("TEXT", s, e, html[s:e]))
        else:
            items.append(("WS", s, e, html[s:e]))
    for b in sorted(blocks, key=lambda x: x["start"]):
        items.append(("Q" if b["is_q"] else "P", b["start"], b["end"], b["raw"]))
    items.sort(key=lambda x: (x[1], x[2]))

    # 判定每个 WS 是否"内部"（前有 text/占位符 且 后有 text）→ 保留进段；否则骨架
    n_items = len(items)
    is_internal_ws = [False] * n_items
    for idx in range(n_items):
        if items[idx][0] != "WS":
            continue
        before_text = False
        after_text = False
        for j in range(idx - 1, -1, -1):
            if items[j][0] in ("TEXT", "P"):
                before_text = True
                break
            if items[j][0] == "Q":
                break
        for j in range(idx + 1, n_items):
            if items[j][0] in ("TEXT", "P"):
                after_text = True
                break
            if items[j][0] == "Q":
                break
        is_internal_ws[idx] = before_text and after_text

    # 按 Q 切段：段 = 文本 + 段中 P + 内部 WS；首尾无空白无标签
    cur_text: list[str] = []
    cur_ph: dict[str, str] = {}
    ph_idx = [0]
    cur_start: int | None = None
    cur_end: int | None = None
    cur_src: list[str] = []

    def flush_seg() -> None:
        nonlocal cur_text, cur_ph, cur_start, cur_end, cur_src
        if cur_start is None:
            cur_text, cur_ph, cur_src = [], {}, []
            return
        display = "".join(cur_text)
        if len(display.strip()) < min_len:
            cur_text, cur_ph, cur_src, cur_start, cur_end = [], {}, [], None, None
            return
        seg = _make_segment(
            seg_id, kind="text", text=display, start=cur_start, end=cur_end,
            placeholders=dict(cur_ph), cfg=cfg, source_text="".join(cur_src),
        )
        if seg:
            segments.append(seg)
        cur_text, cur_ph, cur_src, cur_start, cur_end = [], {}, [], None, None

    for idx, (kind, s, e, raw) in enumerate(items):
        if kind == "Q":
            flush_seg()
        elif kind == "P":
            if cur_start is not None:
                token = _placeholder(ph_open, ph_close, ph_idx[0])
                ph_idx[0] += 1
                cur_ph[token] = raw
                cur_text.append(token)
                cur_src.append(raw)
                cur_end = e
            else:
                pass  # 段首 P → 骨架（天然保留）
        elif kind == "TEXT":
            if cur_start is None:
                cur_start = s
            cur_end = e
            cur_text.append(raw)
            cur_src.append(raw)
        elif kind == "WS":
            if is_internal_ws[idx] and cur_start is not None:
                cur_text.append(raw)
                cur_src.append(raw)
                cur_end = e
            # 否则骨架
    flush_seg()

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


def segments_for_page(cfg: Config, rel: str, html: str | None = None,
                      encoding: str | None = None) -> list[Segment]:
    """读取/生成某页的段列表，并缓存到 work/segments/。

    兼容两种缓存格式：
      - 新版：{"encoding": 编码, "segments": [...]}
      - 旧版：[...]（纯段列表，无编码）
    若传入已解码的 html，则跳过内部读取/解码（复用同一次解码，避免重复探测）。
    """
    seg_path = os.path.join(cfg.get("segments_dir", default=""), rel.replace("/", "__") + ".json")
    if os.path.exists(seg_path):
        data = util.read_json(seg_path, [])
        seg_list = data.get("segments", data) if isinstance(data, dict) else data
        field_names = {f.name for f in Segment.__dataclass_fields__.values()}
        out = []
        for d in seg_list:
            if not isinstance(d, dict):
                continue
            clean = {k: v for k, v in d.items() if k in field_names}
            out.append(Segment(**clean))
        if out:
            return out
    # 重新切分
    if html is None:
        from .crawler import decode_page

        html, encoding = decode_page(cfg, rel)
    segs = split_segments(html, cfg)
    os.makedirs(os.path.dirname(seg_path) or ".", exist_ok=True)
    util.write_json(seg_path, {
        "encoding": encoding or "",
        "segments": [s.to_dict() for s in segs],
    })
    return segs


def reassemble(html: str, segments: list[Segment]) -> str:
    """将翻译结果拼回原文。按 start 倒序替换，避免偏移失效。

    收译文先去首尾全角/半角空白（含 &nbsp;），再还原占位符。
    """
    out = html
    for seg in sorted(segments, key=lambda s: s.start, reverse=True):
        if seg.translation is None:
            continue
        t = re.sub(r"^[\s\u3000]+|[\s\u3000]+$", "", seg.translation)
        replacement = _restore_placeholders(t, seg.placeholders)
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
