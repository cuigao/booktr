"""QA 审核队列：标准化 QA 问题的持久化与交互式裁定。

与 review_queue 不同，qa_queue 专用于 QA 发现的问题：
- 每条含定位到的段号（机械定位）、severity、原因、原文/译文片段、建议。
- 人工交互裁定 status ∈ {open, adopted, rejected, applied} 或丢弃。
- 采纳后由 `qa-apply` 触发定点重译（见 translate.apply_qa_fix）。

未定位成功（resolved=False）的条目保留在队列，需人工 `[m]` 指定段号或 `[d]` 丢弃。
"""
from __future__ import annotations

import hashlib
import os

from . import util
from .config import Config

STATUS_OPEN = "open"
STATUS_ADOPTED = "adopted"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"


def _path(cfg: Config) -> str:
    return cfg.get("qa", "queue_path", default="work/qa_queue.json")


def load(cfg: Config) -> list[dict]:
    return util.read_json(_path(cfg), [])


def save(cfg: Config, items: list[dict]) -> None:
    util.write_json(_path(cfg), items)


def _item_id(page: str, reason: str, src_quote: str, dst_quote: str,
             suggestion: str) -> str:
    raw = "|".join([page or "", reason or "", src_quote or "",
                    dst_quote or "", suggestion or ""])
    return "qa_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def make_item(page: str, issue: dict, origin: dict | None = None) -> dict:
    """由标准化 issue 构造队列条目。"""
    item = {
        "id": _item_id(page, issue.get("reason", ""), issue.get("src_quote", ""),
                       issue.get("dst_quote", ""), issue.get("suggestion", "")),
        "page": page,
        "segments": list(issue.get("segments", []) or []),
        "resolved": bool(issue.get("resolved", bool(issue.get("segments")))),
        "severity": issue.get("severity", "mid"),
        "reason": issue.get("reason", ""),
        "src_quote": issue.get("src_quote", ""),
        "dst_quote": issue.get("dst_quote", ""),
        "suggestion": issue.get("suggestion", ""),
        "status": STATUS_OPEN,
    }
    if origin:
        item["origin"] = origin
    return item


def append_items(cfg: Config, items: list[dict]) -> int:
    """把新条目并入队列（按 id 去重，已存在则保留原状态）。返回新增条数。"""
    queue = load(cfg)
    existing = {it.get("id") for it in queue}
    added = 0
    for it in items:
        if it.get("id") in existing:
            continue
        queue.append(it)
        existing.add(it.get("id"))
        added += 1
    if added:
        save(cfg, queue)
    return added


def stats(cfg: Config) -> dict:
    from collections import Counter
    items = load(cfg)
    by_status = Counter(it.get("status", "?") for it in items)
    return {
        "total": len(items),
        "open": by_status.get(STATUS_OPEN, 0),
        "adopted": by_status.get(STATUS_ADOPTED, 0),
        "by_severity": dict(Counter(it.get("severity", "?") for it in items
                                    if it.get("status") == STATUS_OPEN)),
    }


def _format_item(idx: int, it: dict) -> str:
    sev = it.get("severity", "")
    segs = it.get("segments", [])
    loc = f"段 {segs}" if segs else "（未定位）"
    lines = [
        f"[{idx}] {it.get('page')}  {loc}  [{sev}]  {it.get('status')}",
        f"    原因: {it.get('reason', '')}",
        f"    相关原文: {it.get('src_quote', '')}",
        f"    现有译文问题: {it.get('dst_quote', '')}",
        f"    建议: {it.get('suggestion', '')}",
    ]
    return "\n".join(lines)


def _show_segment_context(cfg: Config, page: str, seg_ids: list) -> None:
    """打印定位段及其上下文（原文/现有译文）。"""
    from . import segments as seg_mod
    from . import translate as tr

    try:
        segs = seg_mod.segments_for_page(cfg, page)
    except (OSError, ValueError, util.EncodingError):
        print("    （无法读取段缓存）")
        return
    by_id = {s.id: s for s in segs}
    for sid in seg_ids:
        s = by_id.get(sid)
        if not s:
            continue
        print(f"    ── 段{sid} ──")
        print(f"    原文: {(s.text or '')[:300]}")
        print(f"    现译: {(s.translation or '')[:300]}")
    if seg_ids:
        first = by_id.get(seg_ids[0])
        if first:
            try:
                before, after = tr._get_adjacent_translations(
                    cfg, first, segs, tr.State(cfg), page, 400)
            except Exception:
                before, after = "", ""
            if before:
                print(f"    前文: {before[-200:]}")
            if after:
                print(f"    后文: {after[:200]}")


def interactive_qa_review(cfg: Config, prompt: str = None,
                          max_items: int = 0) -> int:
    """交互式裁定 QA 队列条目。返回处理条数。

    prompt 非 None 时用于自动化（逐条固定动作）。
    """
    items = load(cfg)
    open_items = [it for it in items if it.get("status") == STATUS_OPEN]
    if not open_items:
        print("QA 队列为空。")
        return 0
    remaining = open_items[:max_items] if max_items > 0 else open_items
    handled = 0
    for idx, it in enumerate(remaining, 1):
        print("\n" + "=" * 60)
        print(_format_item(idx, it))
        seg_ids = it.get("segments", []) or []
        if it.get("resolved") and seg_ids:
            _show_segment_context(cfg, it.get("page"), seg_ids)
            print("操作: [a]采纳  [r]拒绝  [m]手工指定段号  [d]丢弃  [s]跳过  [q]退出")
        else:
            print("⚠ 未定位到段：请 [m] 手工指定段号，或 [d] 丢弃。")
            print("操作: [m]手工指定段号  [d]丢弃  [s]跳过  [q]退出")
        act = (input("> ").strip().lower() or "s") if not prompt else prompt
        if act == "q":
            break
        if act == "a":
            if not (it.get("resolved") and seg_ids):
                print("  未定位段，无法采纳（请先 [m] 指定段号）。")
                handled += 1
                continue
            it["status"] = STATUS_ADOPTED
        elif act == "r":
            it["status"] = STATUS_REJECTED
        elif act == "m":
            if prompt:
                print("  自动化模式不支持手工指定段号。")
            else:
                raw = input("  请输入段号（空格分隔，如 '4 5'）: ").strip()
                ids = [int(x) for x in raw.split() if x.isdigit()]
                if ids:
                    it["segments"] = ids
                    it["resolved"] = True
                    print(f"  已指定段 {ids}；请再次运行以采纳。")
                else:
                    print("  未输入有效段号。")
        elif act == "d":
            items.remove(it)
            print("  已丢弃该条目。")
            handled += 1
            continue
        elif act == "s":
            pass
        handled += 1
    save(cfg, items)
    return handled
