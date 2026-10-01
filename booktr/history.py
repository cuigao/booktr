"""段落历史与回滚：段颗粒度的版本管理（非线性、可 pick 恢复）。

设计要点：
- **提交即版本**：每次成功写入某段译文后，向该段历史追加一条**完整版本**
  （state + 段缓存 + 该段 TM/notes 快照）。因此被 reset/qa-apply 覆盖前的值
  必然已在历史中，回滚 = 挑一个更早版本恢复即可（段落历史无线性编辑序）。
- **纯计划 / 应用分离**：``plan_restore`` 只读地算出完整回滚计划（译文 diff、
  TM/notes 增删、标志位、页面状态），``apply_plan`` 负责落盘。dry-run 与正式
  执行渲染同一计划对象，保证"所见即所得"。
- **回滚也是版本**：恢复时追加一条 ``op=rollback``、``restored_from`` 指向源
  版本，保留完整审计轨迹（从不删除版本）。
- **整页/整命令撤销**：每个版本带 ``op_id``（一次命令调用共享），
  ``--op <id>`` 可把该次命令触碰的所有段恢复到其之前的版本。

存储：``work/segment_history/<rel __>.json`` =
``{"encoding":..., "segments": {"<sid>": [版本...]}}``，与 ``work/segments/`` 平行。
默认不自动裁剪，提供 ``purge`` 管理。
"""
from __future__ import annotations

import difflib
import os
import time
import uuid

from . import util
from .config import Config


# ── 基础存取 ────────────────────────────────────────────────────────────

def enabled(cfg: Config) -> bool:
    return bool(cfg.get("segment_history", "enabled", default=True))


def _dir(cfg: Config) -> str:
    return cfg.get("segment_history", "dir", default="work/segment_history")


def _path(cfg: Config, rel: str) -> str:
    return os.path.join(_dir(cfg), rel.replace("/", "__") + ".json")


def _load(cfg: Config, rel: str) -> dict:
    data = util.read_json(_path(cfg, rel), None)
    if not isinstance(data, dict):
        return {"encoding": "", "segments": {}}
    data.setdefault("encoding", "")
    data.setdefault("segments", {})
    return data


def _save(cfg: Config, rel: str, data: dict) -> None:
    util.write_json(_path(cfg, rel), data)


def new_op_id(cmd: str, ts: int | None = None) -> str:
    return f"op_{ts if ts is not None else int(time.time() * 1000)}_{cmd}"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── 读取当前值 ──────────────────────────────────────────────────────────

def _cache_seg(cfg: Config, rel: str, sid: str) -> dict:
    from . import segments as seg_mod  # noqa: F401
    path = os.path.join(cfg.get("segments_dir", default=""),
                        rel.replace("/", "__") + ".json")
    raw = util.read_json(path, None)
    if isinstance(raw, dict):
        items = raw.get("segments", [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    for it in items:
        if isinstance(it, dict) and str(it.get("id")) == str(sid):
            return it
    return {}


def _state_seg(cfg: Config, rel: str, sid: str, state=None) -> dict:
    if state is not None:
        return state.page(rel).get("segments", {}).get(str(sid), {}) or {}
    raw = util.read_json(cfg.get("state", "path", default="work/state.json"), {})
    return ((raw.get("pages", {}).get(rel, {}) or {})
            .get("segments", {}).get(str(sid), {}) or {})


def _current_translation(cfg: Config, rel: str, sid: str,
                         state_segments: dict | None = None) -> str | None:
    """段当前译文的权威来源：state 优先（显式 None 即未译），缺项才回退段缓存。

    不能用"state 值为 None 就回退缓存"——reset 段重置时 state 置 None 而段缓存
    仍为旧值，回退会误取到已被清除的译文。
    """
    segs = state_segments if state_segments is not None else \
        (util.read_json(cfg.get("state", "path", default="work/state.json"), {})
         .get("pages", {}).get(rel, {}) or {}).get("segments", {})
    if str(sid) in segs:
        return segs[str(sid)].get("translation")
    return _cache_seg(cfg, rel, sid).get("translation")


def _tm_records(cfg: Config, page: str, sid: str) -> list[dict]:
    from . import tm as tm_mod

    return [r for r in util.read_jsonl(tm_mod._path(cfg))
            if r.get("page") == page and str(r.get("segment_id")) == str(sid)]


def _notes_records(cfg: Config, page: str, sid: str) -> list[dict]:
    from . import notes as notes_mod

    return [n for n in notes_mod.all_notes(cfg)
            if n.get("page") == page and str(n.get("segment_id")) == str(sid)]


# ── 提交（追加版本） ────────────────────────────────────────────────────

def commit(cfg: Config, rel: str, sid: str, op: str, op_id: str,
           state=None, author: str = "llm", restored_from: str = "",
           ts: int | None = None) -> dict | None:
    """把某段当前值追加为一条历史版本。返回版本 dict（未启用则 None）。"""
    if not enabled(cfg):
        return None
    sid = str(sid)
    st = dict(_state_seg(cfg, rel, sid, state=state))
    cache = _cache_seg(cfg, rel, sid)
    src_text = (cache.get("text") or st.get("text") or "")
    version = {
        "id": "ver_" + uuid.uuid4().hex[:12],
        "ts": ts if ts is not None else _now_ms(),
        "op_id": op_id,
        "op": op,
        "author": author,
        "restored_from": restored_from,
        "src_text": src_text,
        "state": st,
        "cache": {k: cache.get(k) for k in
                  ("translation", "confidence", "needs_human", "untrusted",
                   "repaired", "repair_methods")},
        "tm": _tm_records(cfg, rel, sid),
        "notes": _notes_records(cfg, rel, sid),
    }
    data = _load(cfg, rel)
    data["segments"].setdefault(sid, []).append(version)
    _save(cfg, rel, data)
    return version


# ── 查询 ────────────────────────────────────────────────────────────────

def versions(cfg: Config, rel: str, sid: str) -> list[dict]:
    return _load(cfg, rel).get("segments", {}).get(str(sid), [])


def get_version(cfg: Config, rel: str, sid: str, version_id: str) -> dict | None:
    for v in versions(cfg, rel, sid):
        if v.get("id") == version_id:
            return v
    return None


def ops(cfg: Config, rel: str) -> list[dict]:
    """按 op_id 聚合该页历史中的命令调用（时间升序）。"""
    data = _load(cfg, rel)
    grouped: dict[str, dict] = {}
    for sid, vers in data.get("segments", {}).items():
        for v in vers:
            oid = v.get("op_id")
            if not oid:
                continue
            e = grouped.setdefault(oid, {"op_id": oid, "ts": v.get("ts", 0),
                                         "op": v.get("op", ""), "sids": set()})
            e["sids"].add(sid)
            e["ts"] = max(e["ts"], v.get("ts", 0))
    out = []
    for e in grouped.values():
        e["sids"] = sorted(e["sids"], key=lambda s: (len(s), s))
        e["count"] = len(e["sids"])
        out.append(e)
    out.sort(key=lambda x: (x["ts"], x["op_id"]))
    return out


# ── 选择恢复目标 ────────────────────────────────────────────────────────

def _different_from(versions_: list[dict], current: str | None) -> str | None:
    """返回最近一个译文与当前不同的版本 id（用于"撤销上一次覆盖"）。"""
    for v in reversed(versions_):
        if v.get("state", {}).get("translation") != current:
            return v["id"]
    return None


def resolve_targets(cfg: Config, page: str, sids=None,
                    src_frag: str = "", dst_frag: str = "",
                    op: str | None = None,
                    include_untranslated: bool = False) -> list[dict]:
    """解析回滚目标段与目标版本。

    返回 ``[{"sid","to_version","note"}]``；``to_version`` 为 None 表示该段
    没有可恢复的更早版本（将被跳过）。
    """
    from . import locate as locate_mod

    page_sids: list[str]
    note = ""
    if op:
        entries = [e for e in ops(cfg, page) if e["op_id"] == op]
        if not entries:
            return []
        page_sids = list(entries[0]["sids"])
        note = f"op {op}"
    elif sids:
        page_sids = [str(s) for s in sids]
    elif src_frag or dst_frag:
        from . import segments as seg_mod

        segs = seg_mod.segments_for_page(cfg, page)
        res = locate_mod.locate(segs, src_frag=src_frag, dst_frag=dst_frag,
                                include_untranslated=include_untranslated, top=5)
        if not res:
            return []
        page_sids = [str(r["sid"]) for r in res[:1]]
        note = f"片段定位 → 段{page_sids[0]}（{res[0]['method']}, {res[0]['score']}）"
    else:
        return []

    targets = []
    for sid in page_sids:
        vs = versions(cfg, page, sid)
        if not vs:
            targets.append({"sid": sid, "to_version": None,
                            "note": "无历史版本"})
            continue
        if op:
            to = None
            for v in reversed(vs):
                if v.get("op_id") != op:
                    to = v["id"]
                    break
            targets.append({"sid": sid, "to_version": to,
                            "note": note or "恢复到该命令之前"})
        else:
            cur = _current_translation(cfg, page, sid)
            to = _different_from(vs, cur)
            targets.append({"sid": sid, "to_version": to,
                            "note": note or ("恢复到上一个不同版本" if to
                                             else "与当前相同，无更早版本")})
    return targets


# ── 纯计划（dry-run 与执行共用） ────────────────────────────────────────

def _reconcile_tm(cfg: Config, page: str, sid: str, snap: list[dict],
                  tm_all: list[dict]) -> dict:
    """计算 TM 增删（安全策略：key 空闲或同属主才恢复）。"""
    cur = [r for r in tm_all
           if r.get("page") == page and str(r.get("segment_id")) == str(sid)]
    cur_keys = {r.get("key") for r in cur}
    snap_keys = {r.get("key") for r in snap}
    owner = {}
    for r in tm_all:
        owner[r.get("key")] = (r.get("page"), str(r.get("segment_id")))
    remove = [r for r in cur if r.get("key") not in snap_keys]
    add, skipped = [], []
    for r in snap:
        k = r.get("key")
        if k not in owner or owner[k] == (page, str(sid)):
            add.append(r)
        else:
            skipped.append({"record": r, "owner_page": owner[k][0]})
    return {"remove": remove, "add": add, "skipped_collisions": skipped}


def _reconcile_notes(cfg: Config, page: str, sid: str, snap: list[dict],
                     notes_all: list[dict]) -> dict:
    cur = [n for n in notes_all
           if n.get("page") == page and str(n.get("segment_id")) == str(sid)]
    cur_ids = {n.get("id") for n in cur}
    existing_ids = {n.get("id") for n in notes_all}
    snap_ids = {n.get("id") for n in snap}
    remove = [n for n in cur if n.get("id") not in snap_ids]
    add = [n for n in snap if n.get("id") not in existing_ids]
    return {"remove": remove, "add": add}


def _recompute_status(segments: dict) -> str:
    if not segments:
        return "pending"
    translated = [s for s in segments.values() if s.get("translation") is not None]
    if not translated:
        return "pending"
    if any(s.get("needs_human") for s in segments.values()):
        return "review"
    return "done"


def plan_restore(cfg: Config, page: str, targets: list[dict]) -> dict:
    """构建回滚计划（只读，不修改任何文件）。"""
    from . import notes as notes_mod
    from . import tm as tm_mod

    tm_all = util.read_jsonl(tm_mod._path(cfg))
    notes_all = notes_mod.all_notes(cfg)

    state_raw = util.read_json(cfg.get("state", "path", default="work/state.json"),
                               {"pages": {}, "done_pages": []})
    pstate = state_raw.get("pages", {}).get(page, {}) or {}
    segments = dict(pstate.get("segments", {}))
    status_before = _recompute_status(segments)

    per_seg = []
    virtual = dict(segments)
    for t in targets:
        sid = str(t["sid"])
        vid = t["to_version"]
        if not vid:
            per_seg.append({"sid": sid, "skipped": True,
                            "reason": t.get("note") or "无可用版本"})
            continue
        ver = get_version(cfg, page, sid, vid)
        if ver is None:
            per_seg.append({"sid": sid, "skipped": True, "reason": "版本不存在"})
            continue
        cur_state = segments.get(sid, {}) or {}
        cur_cache = _cache_seg(cfg, page, sid)
        from_t = _current_translation(cfg, page, sid, state_segments=segments)
        to_state = ver.get("state", {}) or {}
        to_t = to_state.get("translation")
        tm_plan = _reconcile_tm(cfg, page, sid, ver.get("tm", []) or [], tm_all)
        notes_plan = _reconcile_notes(cfg, page, sid, ver.get("notes", []) or [],
                                      notes_all)
        flags = {}
        for k in ("needs_human", "untrusted"):
            a, b = bool(cur_state.get(k)), bool(to_state.get(k))
            if a != b:
                flags[k] = f"{a}→{b}"
        if cur_state.get("confidence") != to_state.get("confidence"):
            flags["confidence"] = f"{cur_state.get('confidence')}→{to_state.get('confidence')}"
        per_seg.append({
            "sid": sid, "skipped": False,
            "src_text": ver.get("src_text", ""),
            "from": {"translation": from_t,
                     "confidence": cur_state.get("confidence"),
                     "needs_human": cur_state.get("needs_human"),
                     "untrusted": cur_state.get("untrusted")},
            "to": {"version_id": vid, "ts": ver.get("ts"),
                   "op": ver.get("op"), "author": ver.get("author"),
                   "translation": to_t,
                   "confidence": to_state.get("confidence"),
                   "needs_human": to_state.get("needs_human"),
                   "untrusted": to_state.get("untrusted")},
            "flags_change": flags,
            "tm": tm_plan,
            "notes": notes_plan,
            "cache_present": bool(cur_cache),
            "version": ver,
        })
        virtual[sid] = dict(to_state)

    status_after = _recompute_status(virtual)
    done_pages = state_raw.get("done_pages", [])
    out_path = os.path.join(cfg.output_dir, page.replace("/", os.sep))
    return {
        "page": page,
        "status_before": status_before,
        "status_after": status_after,
        "done_pages_add": status_after == "done" and page not in done_pages,
        "done_pages_remove": status_after != "done" and page in done_pages,
        "out_path": out_path,
        "out_exists": os.path.exists(out_path),
        "segments": per_seg,
        "totals": {
            "segments": sum(1 for s in per_seg if not s.get("skipped")),
            "skipped": sum(1 for s in per_seg if s.get("skipped")),
            "tm_remove": sum(len(s["tm"]["remove"]) for s in per_seg
                             if not s.get("skipped")),
            "tm_add": sum(len(s["tm"]["add"]) for s in per_seg
                          if not s.get("skipped")),
            "tm_skipped": sum(len(s["tm"]["skipped_collisions"]) for s in per_seg
                              if not s.get("skipped")),
            "notes_remove": sum(len(s["notes"]["remove"]) for s in per_seg
                                if not s.get("skipped")),
            "notes_add": sum(len(s["notes"]["add"]) for s in per_seg
                             if not s.get("skipped")),
        },
    }


# ── 应用计划 ────────────────────────────────────────────────────────────

def _rewrite_tm(cfg: Config, records: list[dict]) -> None:
    from . import tm as tm_mod

    tm_mod._rewrite(cfg, records)


def _rewrite_notes(cfg: Config, records: list[dict]) -> None:
    from . import notes as notes_mod

    notes_mod._rewrite(cfg, records)


def apply_plan(cfg: Config, plan: dict) -> dict:
    """执行回滚计划。返回 {restored, out_regenerated}。"""
    from . import notes as notes_mod
    from . import tm as tm_mod
    from . import translate as tr

    page = plan["page"]
    op_id = new_op_id("rollback")

    tm_all = util.read_jsonl(tm_mod._path(cfg))
    notes_all = notes_mod.all_notes(cfg)
    tm_dirty = notes_dirty = False

    state = tr.State(cfg)
    pstate = state.page(page)
    restored = 0

    for seg in plan["segments"]:
        if seg.get("skipped"):
            continue
        sid = str(seg["sid"])
        ver = seg["version"]
        to_state = ver.get("state", {}) or {}
        to_cache = ver.get("cache", {}) or {}

        # state
        seg_state = pstate.setdefault("segments", {}).setdefault(sid, {})
        seg_state.clear()
        seg_state.update(to_state)

        # 段缓存
        cache_path = os.path.join(cfg.get("segments_dir", default=""),
                                  page.replace("/", "__") + ".json")
        raw = util.read_json(cache_path, None)
        if isinstance(raw, dict):
            items = raw.get("segments", [])
        elif isinstance(raw, list):
            items = raw
        else:
            items = None
        if items is not None:
            for it in items:
                if isinstance(it, dict) and str(it.get("id")) == sid:
                    for k in ("translation", "confidence", "needs_human",
                              "untrusted", "repaired", "repair_methods"):
                        if k in to_cache:
                            it[k] = to_cache[k]
                        elif k in it and k != "translation":
                            it.pop(k, None)
                    break
            util.write_json(cache_path,
                            raw if isinstance(raw, dict) else items)

        # TM 安全恢复
        tm_plan = seg["tm"]
        remove_keys = {r.get("key") for r in tm_plan["remove"]}
        if remove_keys:
            tm_all = [r for r in tm_all if not (
                r.get("page") == page and str(r.get("segment_id")) == sid
                and r.get("key") in remove_keys)]
            tm_dirty = True
        for r in tm_plan["add"]:
            k = r.get("key")
            hit = next((x for x in tm_all if x.get("key") == k), None)
            if hit is None:
                tm_all.append(dict(r))
                tm_dirty = True
            elif (hit.get("page"), str(hit.get("segment_id"))) == (page, sid):
                if hit.get("dst") != r.get("dst"):
                    hit["dst"] = r.get("dst")
                    tm_dirty = True

        # notes 恢复
        notes_plan = seg["notes"]
        remove_ids = {n.get("id") for n in notes_plan["remove"]}
        if remove_ids:
            notes_all = [n for n in notes_all if not (
                n.get("page") == page and str(n.get("segment_id")) == sid
                and n.get("id") in remove_ids)]
            notes_dirty = True
        for n in notes_plan["add"]:
            if not any(x.get("id") == n.get("id") for x in notes_all):
                notes_all.append(dict(n))
                notes_dirty = True

        restored += 1

    # 页面状态
    pstate["status"] = _recompute_status(pstate.get("segments", {}))
    if pstate["status"] == "done":
        if page not in state.data.get("done_pages", []):
            state.data.setdefault("done_pages", []).append(page)
    else:
        if page in state.data.get("done_pages", []):
            state.data["done_pages"].remove(page)
    state.save()

    if tm_dirty:
        _rewrite_tm(cfg, tm_all)
    if notes_dirty:
        _rewrite_notes(cfg, notes_all)

    # 追加 rollback 版本（提交即版本）
    for seg in plan["segments"]:
        if seg.get("skipped"):
            continue
        commit(cfg, page, seg["sid"], "rollback", op_id, state=state,
               author="human", restored_from=seg["to"]["version_id"])

    # 离线重组 out
    out_ok = False
    try:
        from . import review as review_mod

        out_ok = review_mod._regenerate_page(cfg, page)
    except Exception:
        out_ok = False
    return {"restored": restored, "out_regenerated": out_ok}


# ── 维护：purge / backfill ──────────────────────────────────────────────

def purge(cfg: Config, page: str | None = None, keep_last: int | None = None,
          dry_run: bool = False) -> dict:
    """裁剪/清理历史。

    - ``page`` 指定时只处理该页；否则处理全部历史文件。
    - ``keep_last`` 给定则每段仅保留最近 N 个版本；否则删除整页/全部历史。
    """
    files = []
    if page:
        p = _path(cfg, page)
        if os.path.exists(p):
            files.append((page, p))
    else:
        d = _dir(cfg)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    files.append((fn[:-5].replace("__", "/"), os.path.join(d, fn)))

    removed_versions = 0
    removed_files = 0
    for rel, path in files:
        if keep_last is None:
            removed_files += 1
            if not dry_run:
                os.remove(path)
            continue
        data = util.read_json(path, {"segments": {}})
        changed = False
        for sid, vers in list(data.get("segments", {}).items()):
            if len(vers) > keep_last:
                removed_versions += len(vers) - keep_last
                data["segments"][sid] = vers[-keep_last:]
                changed = True
        if changed and not dry_run:
            util.write_json(path, data)
    return {"files": len(files), "removed_files": removed_files,
            "removed_versions": removed_versions,
            "keep_last": keep_last, "page": page}


def backfill(cfg: Config, dry_run: bool = False) -> int:
    """为已有译文（无历史）的段补一条 ``v1`` 版本。返回补录段数。"""
    if not enabled(cfg):
        return 0
    from . import translate as tr

    state = tr.State(cfg)
    op_id = new_op_id("backfill")
    count = 0
    for rel, p in state.data.get("pages", {}).items():
        for sid, s in (p.get("segments", {}) or {}).items():
            if s.get("translation") is None:
                continue
            if versions(cfg, rel, sid):
                continue
            count += 1
            if not dry_run:
                commit(cfg, rel, sid, "backfill", op_id, state=state,
                       author="llm")
    return count


# ── 渲染 ────────────────────────────────────────────────────────────────

def inline_diff(old: str, new: str) -> str:
    """字符级内联 diff：``[-旧-]{+新+}``，用于段译文对照。"""
    old = old or ""
    new = new or ""
    sm = difflib.SequenceMatcher(None, old, new)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(old[i1:i2])
        elif tag == "delete":
            out.append("[-" + old[i1:i2] + "-]")
        elif tag == "insert":
            out.append("{+" + new[j1:j2] + "+}")
        else:  # replace
            out.append("[-" + old[i1:i2] + "-]{+" + new[j1:j2] + "+}")
    return "".join(out)


def format_plan(plan: dict) -> str:
    """把回滚计划渲染为可读文本。"""
    lines = []
    lines.append(f"页面 {plan['page']}")
    lines.append(f"页面状态: {plan['status_before']} → {plan['status_after']}"
                 + (f"  (done_pages +{plan['page']})" if plan["done_pages_add"] else "")
                 + (f"  (done_pages -{plan['page']})" if plan["done_pages_remove"] else ""))
    lines.append(f"out 重组: {plan['out_path']}"
                 + ("（已存在，将覆盖）" if plan["out_exists"] else "（尚不存在）"))
    lines.append("")
    for seg in plan["segments"]:
        if seg.get("skipped"):
            lines.append(f"  段{seg['sid']}: 跳过（{seg.get('reason','')}）")
            continue
        frm, to = seg["from"], seg["to"]
        lines.append(f"  段{seg['sid']}  {frm.get('confidence')} → "
                     f"{to['version_id']} ({to.get('op')}, {to.get('author')}, "
                     f"{to.get('confidence')})")
        lines.append("    原文: " + (seg.get("src_text") or "")[:120])
        lines.append("    译文 diff: " + inline_diff(frm.get("translation"),
                                                   to.get("translation")))
        if seg.get("flags_change"):
            flags = ", ".join(f"{k}: {v}" for k, v in seg["flags_change"].items())
            lines.append("    标志变化: " + flags)
        tm = seg["tm"]
        lines.append(f"    TM: -{len(tm['remove'])} +{len(tm['add'])}"
                     + (f" ⚠跳过{len(tm['skipped_collisions'])}（key 被他页占用）"
                        if tm["skipped_collisions"] else ""))
        nt = seg["notes"]
        lines.append(f"    笔记: -{len(nt['remove'])} +{len(nt['add'])}")
    t = plan["totals"]
    lines.append("")
    lines.append(f"合计: 恢复 {t['segments']} 段（跳过 {t['skipped']}） | "
                 f"TM -{t['tm_remove']} +{t['tm_add']}"
                 + (f" ⚠{t['tm_skipped']}" if t["tm_skipped"] else "")
                 + f" | 笔记 -{t['notes_remove']} +{t['notes_add']}")
    return "\n".join(lines)
