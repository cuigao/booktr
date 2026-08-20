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
    items = data if isinstance(data, list) else []
    # 迁移：confirmed 条目默认 read_only=True
    changed = False
    for it in items:
        if it.get("status") == "confirmed" and "read_only" not in it:
            it["read_only"] = True
            changed = True
    if changed:
        save(cfg, items)
    return items


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
            if entry.get("status") == "confirmed":
                it["read_only"] = True
            save(cfg, items)
            return True, conflict or "已更新"
    item = {
        "src": src,
        "dst": dst,
        "category": entry.get("category", "other"),
        "note": entry.get("note", ""),
        "status": entry.get("status", "confirmed"),
        "read_only": entry.get("status") == "confirmed",  # confirmed 默认 read_only
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


def _normalize_match(text: str) -> str:
    """宽松匹配归一化：去空白 + 小写。"""
    return util.normalize_ws(text).lower()


def relevant(cfg: Config, text: str, limit: int = 30) -> list[dict]:
    """返回与给定文本相关的词汇表条目（宽松子串匹配，忽略大小写/空白）。"""
    items = load(cfg)
    if not items:
        return []
    norm_text = _normalize_match(text)
    scored = []
    for it in items:
        src = it.get("src", "")
        if not src:
            continue
        norm_src = _normalize_match(src)
        if not norm_src:
            continue
        cnt = norm_text.count(norm_src)
        if cnt > 0:
            scored.append((cnt * len(src), it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:limit]]


def lookup_read_only(cfg: Config, text: str) -> str | None:
    """查找词汇表中 read_only=True 的条目，精确匹配返回译文。"""
    key = util.normalize_ws(text)
    if not key:
        return None
    items = load(cfg)
    for it in items:
        if it.get("read_only") and it.get("src") == key:
            return it.get("dst")
    return None


def all_confirmed(cfg: Config) -> list[dict]:
    return [it for it in load(cfg) if it.get("status") == "confirmed"]


def candidates(cfg: Config) -> list[dict]:
    return [it for it in load(cfg) if it.get("status") == "auto-candidate"]


def add_term(cfg: Config, src: str, dst: str, category: str = "term",
             note: str = "", author: str = "user") -> None:
    """添加词汇表条目，并清理对应的短语记忆条目。"""
    ok, msg = upsert(cfg, {"src": src, "dst": dst, "category": category,
                           "note": note, "status": "confirmed"}, author=author)
    print(f"  {src} → {dst}: {msg}")
    # 清理短语记忆中的同名条目（词汇表优先，短语记忆旧条目无用）
    from . import phrases as phrases_mod
    key = util.normalize_ws(src)
    data = phrases_mod.load(cfg)
    if key in data:
        del data[key]
        phrases_mod.save(cfg, data)
        print(f"  已清理短语记忆: {src}")


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
