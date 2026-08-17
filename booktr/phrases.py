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
    """记录短语（src/dst 均为去占位符后的纯文本）。"""
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


def size(cfg: Config) -> int:
    return len(load(cfg))
