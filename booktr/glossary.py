"""词汇表：读写、去重、冲突检测、LLM 候选追加。"""
from __future__ import annotations

import re
from typing import Any

from . import util
from .config import Config

CATEGORIES = ("person", "song", "album", "show", "place", "term", "other")


def load(cfg: Config) -> list[dict]:
    path = cfg.get("glossary", "path", default="work/glossary.json")
    data = util.read_json(path, [])
    return data if isinstance(data, list) else []


def save(cfg: Config, items: list[dict]) -> None:
    path = cfg.get("glossary", "path", default="work/glossary.json")
    util.write_json(path, items)


def upsert(cfg: Config, entry: dict, author: str = "user") -> tuple[bool, str]:
    """插入或更新词汇表条目。返回 (是否冲突, 说明)。"""
    items = load(cfg)
    src = entry.get("src", "").strip()
    if not src:
        return False, "缺少 src"
    dst = entry.get("dst", "").strip()
    conflict = ""
    for it in items:
        if it["src"] == src:
            if it.get("dst") != dst:
                conflict = f"术语「{src}」已有不同译文「{it.get('dst')}」，将覆盖为「{dst}」"
            it["dst"] = dst
            it.setdefault("author", author)
            it["updated"] = author
            if entry.get("category"):
                it["category"] = entry["category"]
            if entry.get("note"):
                it["note"] = entry["note"]
            save(cfg, items)
            return True, conflict or "已更新"
    item = {
        "src": src,
        "dst": dst,
        "category": entry.get("category", "other"),
        "note": entry.get("note", ""),
        "status": entry.get("status", "confirmed"),
        "confidence": entry.get("confidence", 1.0),
        "usage_count": 0,
        "author": author,
    }
    items.append(item)
    save(cfg, items)
    return True, "已新增"


def merge_candidates(cfg: Config, candidates: list[dict]) -> list[dict]:
    """合并自动抽取的候选（status=auto-candidate），返回与现有条目冲突的列表。"""
    items = load(cfg)
    existing = {it["src"] for it in items}
    conflicts = []
    for cand in candidates:
        src = cand.get("src", "").strip()
        if not src:
            continue
        if src in existing:
            conflicts.append(cand)
            continue
        items.append(
            {
                "src": src,
                "dst": cand.get("dst", ""),
                "category": cand.get("category", "other") if cand.get("category") in CATEGORIES else "other",
                "note": cand.get("note", ""),
                "status": "auto-candidate",
                "confidence": cand.get("confidence", 0.5),
                "usage_count": 0,
                "author": "llm",
            }
        )
    save(cfg, items)
    return conflicts


def relevant(cfg: Config, text: str, limit: int = 30) -> list[dict]:
    """返回与给定文本相关的词汇表条目（按出现次数与长度打分）。"""
    items = load(cfg)
    if not items:
        return []
    scored = []
    for it in items:
        src = it.get("src", "")
        if not src:
            continue
        cnt = len(re.findall(re.escape(src), text))
        if cnt > 0:
            scored.append((cnt * len(src), it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:limit]]


def all_confirmed(cfg: Config) -> list[dict]:
    return [it for it in load(cfg) if it.get("status") == "confirmed"]


def candidates(cfg: Config) -> list[dict]:
    return [it for it in load(cfg) if it.get("status") == "auto-candidate"]


def confirm(cfg: Config, src: str, dst: str | None = None) -> bool:
    items = load(cfg)
    changed = False
    for it in items:
        if it["src"] == src:
            it["status"] = "confirmed"
            if dst:
                it["dst"] = dst
            changed = True
    if changed:
        save(cfg, items)
    return changed


def remove(cfg: Config, src: str) -> bool:
    items = load(cfg)
    before = len(items)
    items = [it for it in items if it["src"] != src]
    if len(items) != before:
        save(cfg, items)
        return True
    return False
