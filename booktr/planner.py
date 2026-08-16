"""翻译规划：统一加权排序模型。

所有页面获得一组归一化指标分（semantic/has_semantic/is_orphan/hotness/depth/
chrono/volume/nav/dfs/len），统一加权求和，按总分降序排列。索引语义序只是其中
一个指标（semantic），孤儿/无索引页面该分=0。方向（forward/reverse）与权重均可
配置，支持 GUI 调权（v2）与 CLI 参数覆盖。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from . import util
from .config import Config


def load_site_map(cfg: Config) -> dict:
    path = os.path.join(cfg.work_dir, "site_map.json")
    sm = util.read_json(path, {})
    if not sm:
        raise RuntimeError("site_map.json 不存在，请先运行 `booktr scan`")
    return sm


DEFAULT_WEIGHTS = {
    "semantic": 1.0, "has_semantic": 0.0, "is_orphan": 0.0, "is_index": 0.0,
    "hotness": 0.0, "depth": 0.0, "chrono": 0.0, "volume": 0.0,
    "nav": 0.0, "dfs": 0.0, "len": 0.0,
}
DIRECTIONAL = ("semantic", "chrono", "volume", "nav", "dfs", "len")


def _norm_minmax(vals: dict[str, float], positive: bool = True) -> dict[str, float]:
    """min-max 归一化到 0~1。positive=True 时大者得高分，否则小者得高分。"""
    if not vals:
        return {}
    lo, hi = min(vals.values()), max(vals.values())
    out = {}
    for k, v in vals.items():
        if hi == lo:
            out[k] = 0.5
        elif positive:
            out[k] = (v - lo) / (hi - lo)
        else:
            out[k] = (hi - v) / (hi - lo)
    return out


def _apply_direction(score_map: dict[str, float], direction: str) -> dict[str, float]:
    """forward: 原样；reverse: 取反（1-x）。"""
    if direction == "reverse":
        return {k: 1.0 - v for k, v in score_map.items()}
    return score_map


def _discover_indexes(pages: dict, threshold: float = 0.6) -> dict:
    """递归发现各级索引。

    返回 {目录: 结构信息}，其中结构信息为：
      {"index": 索引页rel或None,
       "members": 本级成员列表（html文件 + 子目录入口文件），
       "semantic_pos": {成员: 索引页链接顺序位置}，
       "count": 成员数}

    每级的成员 = 本级 html 文件 + 各子目录的"入口文件"（子目录内被本级索引
    链接到的第一个文件，通常就是目录入口）。索引判定：某页 links 覆盖本级成员
    比例 ≥ threshold（分母 = 成员数 - 自身）。
    """
    file_dir: dict[str, str] = {}
    for rel in pages:
        file_dir[rel] = os.path.dirname(rel)

    dir_files: dict[str, list[str]] = defaultdict(list)
    for rel, d in file_dir.items():
        dir_files[d].append(rel)

    subdirs_of: dict[str, list[str]] = defaultdict(list)
    for d in dir_files:
        if d:
            parent = os.path.dirname(d)
            subdirs_of[parent].append(d)

    level_members: dict[str, list[str]] = {}
    entry_of_subdir: dict[str, str] = {}

    def build_members(d: str) -> None:
        members = list(dir_files.get(d, []))
        for sd in sorted(subdirs_of.get(d, [])):
            linked = []
            for rel in dir_files.get(d, []):
                for o in pages[rel].get("links_out", []):
                    if os.path.dirname(o) == sd and o not in linked:
                        linked.append(o)
            if linked:
                entry = linked[0]
            elif dir_files.get(sd):
                entry = sorted(dir_files[sd])[0]
            else:
                entry = None
            if entry:
                entry_of_subdir[sd] = entry
                members.append(entry)
        level_members[d] = members

    for d in list(dir_files):
        build_members(d)

    result: dict[str, dict] = {}

    def process(d: str) -> None:
        members = level_members.get(d, [])
        if not members:
            result[d] = {"index": None, "members": [], "semantic_pos": {}, "count": 0}
            return
        best_idx = None
        best_cov = 0.0
        for rel in members:
            outs = pages[rel].get("links_out", [])
            cov = sum(1 for o in outs if o in members)
            denom = max(1, len(members) - 1)
            ratio = cov / denom
            if ratio >= threshold and ratio > best_cov:
                best_idx, best_cov = rel, ratio
        if best_idx is None:
            result[d] = {"index": None, "members": members, "semantic_pos": {}, "count": len(members)}
            return
        order = [o for o in pages[best_idx].get("links_out", []) if o in members]
        pos = {rel: i for i, rel in enumerate(order)}
        # 索引页自身视为本级语义序第一位
        pos[best_idx] = -1
        result[d] = {"index": best_idx, "members": members, "semantic_pos": pos, "count": len(members)}

    for d in level_members:
        process(d)

    return result


def _date_to_float(dt: str) -> float:
    """YYYY-MM-DD → 浮点年，用于 chrono 归一化。"""
    try:
        y, m, d = (int(x) for x in dt.split("-"))
        return y + (m - 1) / 12 + (d - 1) / 365.0
    except (ValueError, AttributeError):
        return 0.0


def _compute_scores(cfg: Config, pages: dict, indexes: dict,
                    weights: dict, directions: dict) -> tuple[dict, dict]:
    """计算每页指标分与加权总分。返回 (scores, semantic_sources)。"""
    rels = list(pages.keys())

    semantic = {rel: 0.0 for rel in rels}
    has_semantic = {rel: 0 for rel in rels}
    is_orphan = {rel: 0 for rel in rels}
    semantic_sources: dict[str, dict] = {}

    # ---- semantic（层级路径级联的复合位置分）----
    # 每个有索引的目录 d 计算 path_rank[d]：从 root 沿目录链到 d 的 rank 级联值。
    # 页面的 composite 统一为：path_rank[目录] × BASE + 本级 rank（无本级索引则本级 rank=0）。
    # 这样单页目录与子目录页面量纲一致，跨目录按"父级锚点 → 本级序"自然分层。
    BASE = 1000
    level_order: dict[str, dict[str, int]] = {}
    for d, info in indexes.items():
        if not info["index"]:
            continue
        pos = dict(info["semantic_pos"])
        ordered = [info["index"]]
        others = sorted(
            (rel for rel in info["members"] if rel != info["index"]),
            key=lambda r: pos.get(r, 10**9),
        )
        ordered.extend(others)
        level_order[d] = {rel: i for i, rel in enumerate(ordered)}

    # 每个子目录在其父级索引中的 rank（父级索引的成员里，属于该子目录入口的 rank）
    # 对单页目录，入口即其唯一页面
    dir_rank: dict[str, int] = {}
    for d, ord_map in level_order.items():
        for rel, rank in ord_map.items():
            subd = os.path.dirname(rel)
            if subd and subd != d:
                dir_rank[subd] = rank

    # 目录链的 rank 级联：path_rank[d] = path_rank[父] × BASE + dir_rank[d]
    def path_rank(d: str) -> float:
        if not d:
            return 0.0
        parent = os.path.dirname(d)
        pr = path_rank(parent)
        return pr * BASE + dir_rank.get(d, 0)

    composite: dict[str, float] = {}
    for rel in rels:
        d = os.path.dirname(rel)
        local = level_order[d].get(rel, 0) if d in level_order else 0
        composite[rel] = path_rank(d) * BASE + local

    # is_index：是否本级索引页（0/1，用户可调高权重获得"绝对结构优先级"）
    is_index = {rel: 0 for rel in rels}
    for d, info in indexes.items():
        if info["index"]:
            is_index[info["index"]] = 1

    # 归一化（反向：位置越前 → 语义分越高）
    semantic = _norm_minmax(composite, positive=False)
    for rel in rels:
        d = os.path.dirname(rel)
        info = indexes.get(d)
        # 孤儿：本级有索引，但该成员未被索引页链接覆盖（不在 semantic_pos 中），
        # 且不是索引页自身。孤儿仍排在语义序末尾，但正确标记 is_orphan。
        if info and info["index"] and d in level_order and rel in level_order[d]:
            sp = info.get("semantic_pos", {})
            if rel not in sp and rel != info["index"]:
                is_orphan[rel] = 1
        has_semantic[rel] = 1 if d in level_order and rel in level_order[d] else 0
        if d in level_order and rel in level_order[d]:
            semantic_sources[rel] = {
                "index": indexes[d]["index"],
                "pos": level_order[d][rel],
                "n": len(level_order[d]),
            }

    # ---- hotness ----
    indeg = {rel: p.get("in_degree", 0) for rel, p in pages.items()}
    hotness = _norm_minmax(indeg, positive=True)

    # ---- depth（越浅越高） ----
    depth_raw = {rel: p.get("depth", 0) for rel, p in pages.items()}
    depth = _norm_minmax(depth_raw, positive=False)

    # ---- chrono（日期，forward=越早越高） ----
    chrono_raw = {rel: _date_to_float(p["date"]) if p.get("date") else 0.0
                  for rel, p in pages.items()}
    if any(v > 0 for v in chrono_raw.values()):
        chrono = _norm_minmax(chrono_raw, positive=True)
    else:
        chrono = {rel: 0.0 for rel in rels}

    # ---- volume（编号，forward=越小越高） ----
    vol_raw = {rel: p.get("volume") or 0 for rel, p in pages.items()}
    vol_norm = _norm_minmax(vol_raw, positive=False)

    # ---- nav（root 导航位次，forward=越前越高） ----
    nav_raw = {rel: p.get("nav_pos") or 9999 for rel, p in pages.items()}
    nav = _norm_minmax(nav_raw, positive=False)

    # ---- dfs（DFS 访问序，forward=越前越高） ----
    dfs_raw = {rel: p.get("dfs_order", 0) for rel, p in pages.items()}
    dfs = _norm_minmax(dfs_raw, positive=False)

    # ---- len（原文长度，forward=越长越高） ----
    len_raw = {rel: p.get("text_len", 0) for rel, p in pages.items()}
    len_ = _norm_minmax(len_raw, positive=True)

    scores = {}
    for rel in rels:
        s = {
            "semantic": semantic[rel],
            "has_semantic": float(has_semantic[rel]),
            "is_orphan": float(is_orphan[rel]),
            "is_index": float(is_index[rel]),
            "hotness": hotness.get(rel, 0.0),
            "depth": depth.get(rel, 0.0),
            "chrono": chrono.get(rel, 0.0),
            "volume": vol_norm.get(rel, 0.0),
            "nav": nav.get(rel, 0.0),
            "dfs": dfs.get(rel, 0.0),
            "len": len_.get(rel, 0.0),
        }
        for key in DIRECTIONAL:
            if key in directions:
                s[key] = _apply_direction({rel: s[key]}, directions[key])[rel]
        total = sum(weights.get(k, 0.0) * s[k] for k in s)
        s["total"] = total
        scores[rel] = s

    return scores, semantic_sources


def _reason(rel: str, p: dict, scores: dict) -> list[str]:
    r = []
    s = scores.get(rel, {})
    total = s.get("total", 0.0)
    r.append(f"加权 {total:.3f}")
    top = max((k for k in s if k != "total"), key=lambda k: s[k])
    r.append(f"主导指标 {top}({s[top]:.2f})")
    if s.get("has_semantic"):
        r.append("有语义序")
    if s.get("is_index"):
        r.append("索引页")
    if s.get("is_orphan"):
        r.append("孤儿")
    return r


def build_plan(cfg: Config, survey: dict | None = None,
               weights: dict | None = None, directions: dict | None = None,
               index_threshold: float | None = None,
               reset_order: bool = False) -> dict:
    sm = load_site_map(cfg)
    pages = sm["pages"]
    pcfg = cfg.get("planner", default={})

    w = dict(DEFAULT_WEIGHTS)
    if pcfg.get("weights"):
        w.update({k: v for k, v in pcfg["weights"].items() if v is not None})
    if weights:
        w.update({k: v for k, v in weights.items() if v is not None})

    dr = dict(pcfg.get("directions", {}))
    if directions:
        dr.update({k: v for k, v in directions.items() if v is not None})

    thr = index_threshold if index_threshold is not None else pcfg.get("index_threshold", 0.6)

    indexes = _discover_indexes(pages, thr)
    scores, semantic_sources = _compute_scores(cfg, pages, indexes, w, dr)

    ranked = sorted(pages.keys(), key=lambda rel: -scores[rel]["total"])
    auto_order = ranked

    out_path = os.path.join(cfg.work_dir, "plan.json")
    existing = util.read_json(out_path, {})
    if reset_order or not existing.get("order"):
        order = list(auto_order)
    else:
        order = existing["order"]

    reasons = {rel: _reason(rel, p, scores) for rel, p in pages.items()}

    indexes_info = {}
    for d, info in indexes.items():
        indexes_info[d or "(root)"] = info

    plan = {
        "survey_used": bool(survey),
        "weights": w,
        "directions": dr,
        "index_threshold": thr,
        "indexes": indexes_info,
        "semantic_sources": semantic_sources,
        "scores": scores,
        "order": order,
        "auto_order": auto_order,
        "reasons": reasons,
        "stats": {"total": len(order)},
    }
    util.write_json(out_path, plan)
    return plan


def plan_summary(cfg: Config) -> str:
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    if not plan:
        return "暂无计划，请先运行 `booktr plan`"
    lines = [f"计划共 {plan['stats']['total']} 页（权重: {json.dumps(plan.get('weights', {}), ensure_ascii=False)}）"]
    for i, rel in enumerate(plan["order"], 1):
        reasons = "；".join(plan["reasons"].get(rel, []))
        lines.append(f"{i:3d}. {rel}  [{reasons}]")
    return "\n".join(lines)
