"""交互审核队列：处理词汇表冲突、低置信度、QA 问题。"""
from __future__ import annotations

import json
import os

from . import glossary as gl
from . import notes as notes_mod
from . import tm as tm_mod
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
    from .crawler import decode_page

    try:
        segs = segments_for_page(cfg, rel)
        if not segs or not any(s.translation for s in segs):
            return False
        html, _ = decode_page(cfg, rel)
        out_html = reassemble(html, segs)
        write_page_output(cfg, rel, out_html)
        return True
    except (OSError, ValueError, util.EncodingError):
        return False


def _clear_segment_cache_translation(cfg: Config, rel: str, sid: str) -> None:
    """把段缓存中指定段的译文清空（与 state 的 ``translation=None`` 对齐）。

    ``[d]`` 只清 state，缓存仍留旧译文，会使 ``_regenerate_page`` 用缓存重组出
    旧 out。此处在删除段译文时同步清缓存，使 out 立即反映删除结果。
    """
    seg_path = os.path.join(
        cfg.get("segments_dir", default=""), rel.replace("/", "__") + ".json"
    )
    if not os.path.exists(seg_path):
        return
    data = util.read_json(seg_path, {})
    seg_list = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(seg_list, list):
        return
    changed = False
    for s in seg_list:
        if isinstance(s, dict) and str(s.get("id")) == str(sid):
            s["translation"] = None
            s["needs_human"] = False
            s["untrusted"] = False
            changed = True
            break
    if not changed:
        return
    if isinstance(data, dict):
        util.write_json(seg_path, {"encoding": data.get("encoding", ""), "segments": seg_list})
    else:
        util.write_json(seg_path, seg_list)


def _finalize_review(cfg: Config, page: str) -> None:
    """处理完所有审核条目后，更新页面状态并重生成 out。

    - 页面有 deleted 条目（段翻译被删）→ 保持 pending，等待 --next 重译
    - 纯 accept/skip（无 deleted）→ 页面转 done
    """
    from .translate import State, STATUS

    items = load_queue(cfg)
    has_open = any(it["page"] == page and it["status"] == "open" for it in items)
    page_deleted = any(it["page"] == page and it["status"] == "deleted" for it in items)

    state = State(cfg)
    pstate = state.page(page)

    if not has_open:
        if page_deleted:
            # 有段被删除 → 保持 pending，等 --next 重译
            if pstate.get("status") in (STATUS["review"], STATUS["done"]):
                pstate["status"] = STATUS["pending"]
            # 防御性移除 done_pages（与 [d] 循环内逻辑一致）
            if page in state.data.get("done_pages", []):
                state.data["done_pages"].remove(page)
        else:
            # 纯 accept/skip → done
            if pstate.get("status") == STATUS["review"]:
                pstate["status"] = STATUS["done"]
                if page not in state.data.get("done_pages", []):
                    state.data["done_pages"].append(page)
        state.save()

    if not _regenerate_page(cfg, page):
        print(f"  ⚠ 无法重生成: {page}（段缓存缺失或为空）")


def interactive_review(cfg: Config, prompt: str = None, max_items: int = 0,
                       confirm: str = None) -> int:
    """在终端逐条审核。返回处理条数。

    prompt：非 None 时用于自动化（逐条固定动作），此时二次确认默认视为 "y"。
    confirm：删除类操作的二次确认应答（None 时用 input() 询问）。
    """
    from .translate import State, STATUS

    # 自动化模式（prompt 给定）下，二次确认默认视为 "y"（保持测试兼容）
    if confirm is None and prompt is not None:
        confirm = "y"

    items = load_queue(cfg)
    open_items = [it for it in items if it.get("status") == "open"]
    if not open_items:
        print("审核队列为空。")
        return 0
    remaining = open_items
    if max_items > 0:
        remaining = remaining[:max_items]
    handled = 0
    changed_pages = set()
    for it in remaining:
        print("\n" + "=" * 60)
        print(f"页面: {it['page']}  段: {it['segment_id']}  原因: {it['reason']}")
        print(f"原文: {it['src'][:200]}")
        if it.get("detail"):
            print(f"详情: {str(it['detail'])[:200]}")
        is_qa = str(it.get("reason", "")).startswith("qa_")
        if is_qa:
            print("操作: [a]标记已处理  [s]跳过  [q]退出")
        elif it["reason"] == "glossary_conflict":
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
            # QA 条目仅标记已处理，不触发页面状态变更
            if not is_qa:
                changed_pages.add(it["page"])
        elif act == "d":
            # 删除前预览将清理的 TM / notes / 短语记忆，并二次确认
            from . import phrases as phrases_mod

            sid = str(it["segment_id"])
            n_tm = tm_mod.purge_segments(cfg, it["page"], [sid], dry_run=True)
            n_notes = notes_mod.purge_segments(cfg, it["page"], [sid], dry_run=True)
            _st = State(cfg)
            cur_tr = (_st.page(it["page"]).get("segments", {}).get(sid, {}) or {}) \
                .get("translation") or ""
            src_text = it.get("src", "")
            n_ph = len(phrases_mod.segment_removals(cfg, src_text, cur_tr, ""))
            print(f"将删除该段译文，并清理：翻译记忆 {n_tm} 条、翻译笔记 {n_notes} 条、"
                  f"短语记忆 {n_ph} 条")
            if confirm is None:
                ans = input("确认删除该段并清理？[y/N] ").strip().lower()
            else:
                ans = str(confirm).strip().lower()
            if ans not in ("y", "yes"):
                it["status"] = "skipped"
                print("已跳过（未删除、未清理）。")
                handled += 1
                continue
            # 清理 TM / notes / 短语记忆
            tm_mod.purge_segments(cfg, it["page"], [sid])
            notes_mod.purge_segments(cfg, it["page"], [sid])
            phrases_mod.purge_for_segment(cfg, src_text, cur_tr, "")
            it["status"] = "deleted"
            # 删除该段翻译，标记为 pending（使 --next 可重译）
            state = State(cfg)
            pstate = state.page(it["page"])
            seg_state = pstate.get("segments", {}).get(sid)
            if seg_state:
                seg_state["translation"] = None
                seg_state["needs_human"] = False
                seg_state["untrusted"] = False
            if pstate.get("status") in (STATUS["review"], STATUS["done"]):
                pstate["status"] = STATUS["pending"]
            if it["page"] in state.data.get("done_pages", []):
                state.data["done_pages"].remove(it["page"])
            state.save()  # 立即持久化，避免被后续统一保存覆盖丢失
            _clear_segment_cache_translation(cfg, it["page"], sid)
            from . import history as hist_mod
            hist_mod.commit(cfg, it["page"], sid, "review-delete",
                            hist_mod.new_op_id("review-delete"), state=state)
            changed_pages.add(it["page"])
        elif act == "s":
            it["status"] = "skipped"
        handled += 1
    save_queue(cfg, items)

    # F: 统一保存状态（循环内 [d] 已修改，这里不再单独 save）
    if changed_pages:
        state = State(cfg)
        state.save()

    # 自动重生成
    auto_regenerate = cfg.get("review", "auto_regenerate", default=True)
    if auto_regenerate and changed_pages:
        for page in changed_pages:
            _finalize_review(cfg, page)
            print(f"  📄 已重生成: {page}")

    return handled


def queue_stats(cfg: Config) -> dict:
    items = load_queue(cfg)
    from collections import Counter

    by_reason = Counter(it.get("reason", "?") for it in items if it.get("status") == "open")
    return {"open": sum(1 for it in items if it.get("status") == "open"),
            "by_reason": dict(by_reason)}
