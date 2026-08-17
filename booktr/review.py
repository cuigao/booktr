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


def _regenerate_page(cfg: Config, rel: str) -> bool:
    """从段索引离线重组指定页面的译文并写回 out。返回是否成功。"""
    from .segments import segments_for_page, reassemble, write_page_output
    from .crawler import resolve_local_path

    segs = segments_for_page(cfg, rel)
    if not segs or not any(s.translation for s in segs):
        return False
    raw = open(resolve_local_path(cfg, rel), "rb").read()
    html, _ = util.decode_html(raw)
    out_html = reassemble(html, segs)
    write_page_output(cfg, rel, out_html)
    return True


def _finalize_review(cfg: Config, page: str) -> None:
    """处理完所有审核条目后，更新页面状态并重生成 out。"""
    from . import translate as tr
    from . import state as state_mod

    items = load_queue(cfg)
    has_open = any(it["page"] == page and it["status"] == "open" for it in items)

    state = state_mod.State(cfg)
    pstate = state.page(page)

    if not has_open:
        if pstate.get("status") == tr.STATUS["review"]:
            pstate["status"] = tr.STATUS["done"]
            state.save()

    _regenerate_page(cfg, page)


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
    accepted_pages = set()
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
            accepted_pages.add(it["page"])
        elif act == "d":
            it["status"] = "deleted"
            # 删除该段翻译，标记为 pending
            from .translate import State, STATUS
            state = State(cfg)
            pstate = state.page(it["page"])
            sid = str(it["segment_id"])
            seg_state = pstate.get("segments", {}).get(sid)
            if seg_state:
                seg_state["translation"] = None
                seg_state["needs_human"] = False
                seg_state["untrusted"] = False
            # 从 done_pages 中移除（如果有）
            if it["page"] in state.data.get("done_pages", []):
                state.data["done_pages"].remove(it["page"])
            state.save()
        elif act == "s":
            it["status"] = "skipped"
        handled += 1
    save_queue(cfg, items)

    # 自动重生成
    auto_regenerate = cfg.get("review", "auto_regenerate", default=True)
    if auto_regenerate and accepted_pages:
        for page in accepted_pages:
            _finalize_review(cfg, page)
            print(f"  📄 已重生成: {page}")

    return handled


def queue_stats(cfg: Config) -> dict:
    items = load_queue(cfg)
    from collections import Counter

    by_reason = Counter(it.get("reason", "?") for it in items if it.get("status") == "open")
    return {"open": sum(1 for it in items if it.get("status") == "open"),
            "by_reason": dict(by_reason)}
