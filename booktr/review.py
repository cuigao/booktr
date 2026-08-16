"""交互审核队列：处理词汇表冲突、低置信度、QA 问题。"""
from __future__ import annotations

import json
import os

from . import glossary as gl
from . import util
from .config import Config


def load_queue(cfg: Config) -> list[dict]:
    path = cfg.get("review", "path", default="work/review_queue.json")
    return util.read_json(path, [])


def save_queue(cfg: Config, items: list[dict]) -> None:
    path = cfg.get("review", "path", default="work/review_queue.json")
    util.write_json(path, items)


def enqueue(cfg: Config, item: dict) -> None:
    items = load_queue(cfg)
    items.append(item)
    save_queue(cfg, items)


def interactive_review(cfg: Config, prompt: str = None, max_items: int = 0) -> int:
    """在终端逐条审核。返回处理条数。"""
    items = load_queue(cfg)
    open_items = [it for it in items if it.get("status") == "open"]
    if not open_items:
        print("审核队列为空。")
        return 0
    remaining = open_items
    if max_items > 0:
        remaining = remaining[:max_items]
    handled = 0
    for it in remaining:
        print("\n" + "=" * 60)
        print(f"页面: {it['page']}  段: {it['segment_id']}  原因: {it['reason']}")
        print(f"原文: {it['src'][:200]}")
        if it.get("detail"):
            print(f"详情: {str(it['detail'])[:200]}")
        if it["reason"] == "glossary_conflict":
            print("操作: [c]确认加入词汇表  [a]接受译文  [s]跳过  [d]删除  [q]退出")
        else:
            print("操作: [a]接受  [s]跳过  [d]删除  [q]退出")
        act = (input("> ").strip().lower() or "a") if not prompt else prompt
        if act == "q":
            break
        if act == "c":
            # 将冲突术语加入词汇表
            detail = str(it.get("detail", ""))
            # detail 形如「术语X」已有不同译文...
            m = __import__("re").search(r"[「『](.+?)[」』]", detail)
            if m:
                gl.upsert(cfg, {"src": m.group(1), "dst": detail.split("→")[-1].strip()[:40], "category": "other"}, author="review")
        if act in ("a", "c"):
            it["status"] = "accepted"
        elif act == "d":
            it["status"] = "deleted"
        elif act == "s":
            it["status"] = "skipped"
        handled += 1
    save_queue(cfg, items)
    return handled


def queue_stats(cfg: Config) -> dict:
    items = load_queue(cfg)
    from collections import Counter

    by_reason = Counter(it.get("reason", "?") for it in items if it.get("status") == "open")
    return {"open": sum(1 for it in items if it.get("status") == "open"),
            "by_reason": dict(by_reason)}
