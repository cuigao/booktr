"""翻译记忆（Translation Memory）：双语片段缓存。"""
from __future__ import annotations

from . import util
from .config import Config


def _path(cfg: Config) -> str:
    return cfg.get("tm", "path", default="work/tm.jsonl")


def add(cfg: Config, src: str, dst: str, page: str, segment_id: int | None) -> None:
    if not src or not dst or util.normalize_ws(src) == util.normalize_ws(dst):
        return
    # 规范化：压缩空白，作为键
    key = util.normalize_ws(src)
    if not key:
        return
    records = util.read_jsonl(_path(cfg))
    for r in records:
        if r["key"] == key:
            r["dst"] = dst
            r["page"] = page
            r["usage_count"] = r.get("usage_count", 0) + 1
            _rewrite(cfg, records)
            return
    util.append_jsonl(
        _path(cfg),
        {"key": key, "src": src, "dst": dst, "page": page,
         "segment_id": segment_id, "usage_count": 1},
    )


def _rewrite(cfg: Config, records: list[dict]) -> None:
    path = _path(cfg)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(util.json.dumps(r, ensure_ascii=False) + "\n")


def lookup(cfg: Config, text: str, threshold: float = 0.9) -> list[dict]:
    """查找相似命中。"""
    target = util.normalize_ws(text)
    if not target:
        return []
    out = []
    for r in util.read_jsonl(_path(cfg)):
        score = util.dice_coefficient(r["key"], target)
        if score >= threshold:
            out.append({**r, "score": round(score, 3)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:5]


def size(cfg: Config) -> int:
    return len(util.read_jsonl(_path(cfg)))


def purge_term(cfg: Config, src_term: str, dst_term: str) -> int:
    """清理与指定术语规范译法矛盾的翻译记忆记录。

    规则：记录的 src 含术语原文、且 dst 不含其规范译文 → 删除（视为过时错译）；
    保留 dst 已含规范译文的记录。返回删除条数。
    """
    if not src_term or not dst_term:
        return 0
    records = util.read_jsonl(_path(cfg))
    kept = []
    removed = 0
    for r in records:
        if src_term in (r.get("src") or "") and dst_term not in (r.get("dst") or ""):
            removed += 1
            continue
        kept.append(r)
    if removed:
        _rewrite(cfg, kept)
    return removed


def purge_keywords(cfg: Config, keywords: list[str]) -> int:
    """清理译文（dst）中含任一指定关键词的翻译记忆记录。返回删除条数。"""
    kws = [k for k in (keywords or []) if k]
    if not kws:
        return 0
    records = util.read_jsonl(_path(cfg))
    kept = []
    removed = 0
    for r in records:
        dst = r.get("dst") or ""
        if any(k in dst for k in kws):
            removed += 1
            continue
        kept.append(r)
    if removed:
        _rewrite(cfg, kept)
    return removed


