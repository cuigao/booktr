"""短语记忆：导航词等短文本的精确命中复用。

成功翻译一次后记录（去占位符文本 ≤ max_len），后续全文精确命中直接采用，
避免重复调用 LLM 处理固定短短语（HOME/RETURN/栏目名/曲名等）。
"""
from __future__ import annotations

import os

from . import util
from .config import Config


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
