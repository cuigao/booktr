"""短语记忆：导航词等短文本的精确命中复用。

成功翻译一次后记录（去占位符文本 ≤ max_len），后续全文精确命中直接采用，
避免重复调用 LLM 处理固定短短语（HOME/RETURN/栏目名/曲名等）。
"""
from __future__ import annotations

import os
import re

from . import util
from .config import Config

_PH_RE = re.compile(r"\[\[P\d+\]\]")


def _path(cfg: Config) -> str:
    return cfg.get("phrases", "path", default="work/phrase_memory.json")


def _max_len(cfg: Config) -> int:
    return cfg.get("phrases", "max_len", default=30)


def load(cfg: Config) -> dict:
    data = util.read_json(_path(cfg), {})
    return data if isinstance(data, dict) else {}


def save(cfg: Config, data: dict) -> None:
    util.write_json(_path(cfg), data)


def add(cfg: Config, src: str, dst: str) -> bool:
    """记录短语。如果词汇表已有 read_only 条目，不写入。"""
    from . import glossary as gl
    if gl.lookup_read_only(cfg, src) is not None:
        return False
    key = util.normalize_ws(src)
    if not key or len(key) > _max_len(cfg):
        return False
    data = load(cfg)
    data[key] = {"dst": dst, "usage": data.get(key, {}).get("usage", 0) + 1}
    save(cfg, data)
    return True


def lookup(cfg: Config, text: str) -> str | None:
    """精确命中返回译文，否则 None。"""
    key = util.normalize_ws(text)
    if not key or len(key) > _max_len(cfg):
        return None
    data = load(cfg)
    hit = data.get(key)
    return hit.get("dst") if hit else None


def relevant(cfg: Config, text: str, limit: int = 10) -> list[dict]:
    """返回与给定文本相关的短语记忆条目（宽松子串匹配，忽略大小写/空白）。"""
    data = load(cfg)
    if not data:
        return []
    norm_text = util.normalize_ws(text).lower()
    scored = []
    for src, info in data.items():
        if not src:
            continue
        norm_src = util.normalize_ws(src).lower()
        if not norm_src:
            continue
        if norm_src in norm_text:
            scored.append({"src": src, "dst": info["dst"],
                           "usage": info.get("usage", 0), "source": "phrase"})
    scored.sort(key=lambda x: x["usage"], reverse=True)
    return scored[:limit]


def size(cfg: Config) -> int:
    return len(load(cfg))


# ── 学习 / 对称清理 ─────────────────────────────────────────────────────

def _plain(text: str) -> str:
    """去占位符并规范化空白（短语记忆的比对口径）。"""
    return util.normalize_ws(_PH_RE.sub("", text or ""))


def learn(cfg: Config, src_chunk: str, translation: str) -> bool:
    """按主翻译流程的规则记录一条短语。返回是否写入。

    条件：译文非空；源 chunk 去占位符后非空；占位符仅在 chunk 首/尾；
    译文去占位符后非空且不含 |TEXT|/|DST|；`phrases.add` 内部再判
    （词汇表 read_only 优先、长度 ≤ max_len）。
    """
    if not translation:
        return False
    chk_plain = _PH_RE.sub("", src_chunk or "").strip()
    if not chk_plain:
        return False
    ph_positions = [m.start() for m in _PH_RE.finditer(src_chunk or "")]
    if ph_positions:
        all_at_edges = all(
            p == 0 or p + len("[[P0]]") >= len(src_chunk) - 1
            for p in ph_positions
        )
        if not all_at_edges:
            return False
    t_plain = _PH_RE.sub("", translation).strip()
    if not t_plain or "|TEXT|" in t_plain or "|DST|" in t_plain:
        return False
    return add(cfg, chk_plain, t_plain)


def segment_removals(cfg: Config, source: str, from_translation: str,
                     to_translation: str = "") -> list[str]:
    """返回应清理的短语 key：条目 (k,d) 满足

      ① k ∈ plain(source)      （短语确在本段源文出现）
      ② d ∈ plain(from_translation)（该译法确被本段旧译文用到）
      ③ d ∉ plain(to_translation)（修正/回滚后已不出现）

    三条件同时成立才删除；译文未变（d 仍在新译文）或证据不足则保留。
    """
    data = load(cfg)
    if not data:
        return []
    s = _plain(source)
    f = _plain(from_translation)
    t = _plain(to_translation)
    if not s or not f:
        return []
    out = []
    for key, info in data.items():
        d = (info or {}).get("dst") or ""
        if key and key in s and d and d in f and d not in t:
            out.append(key)
    return out


def purge_for_segment(cfg: Config, source: str, from_translation: str,
                      to_translation: str = "") -> int:
    """按段源文与译文变化清理短语记忆（删除而非改写）。返回删除条数。"""
    keys = segment_removals(cfg, source, from_translation, to_translation)
    if not keys:
        return 0
    data = load(cfg)
    for k in keys:
        data.pop(k, None)
    save(cfg, data)
    return len(keys)
