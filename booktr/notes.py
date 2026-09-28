"""翻译笔记：追加式 JSONL，记录重要/存疑/决策信息，可溯源。"""
from __future__ import annotations

import uuid

from . import util
from .config import Config


def _path(cfg: Config) -> str:
    return cfg.get("notes", "path", default="work/notes.jsonl")


def add(
    cfg: Config,
    page: str,
    segment_id: int | None,
    quote: str,
    summary: str,
    kind: str = "其他",
    created_by: str = "llm",
) -> dict:
    """追加一条笔记。kind ∈ {重要信息, 存疑, 决策, 关联, 其他}。"""
    note = {
        "id": uuid.uuid4().hex[:12],
        "page": page,
        "segment_id": segment_id,
        "quote": quote[:500],
        "summary": summary,
        "kind": kind,
        "created_by": created_by,
    }
    util.append_jsonl(_path(cfg), note)
    return note


def add_user(cfg: Config, text: str) -> dict:
    """用户从 inbox 或交互中注入的笔记。"""
    return add(cfg, "", None, "", text, kind="用户注入", created_by="user")


def all_notes(cfg: Config) -> list[dict]:
    return util.read_jsonl(_path(cfg))


def _rewrite(cfg: Config, notes: list[dict]) -> None:
    path = _path(cfg)
    with open(path, "w", encoding="utf-8") as f:
        for n in notes:
            f.write(util.json.dumps(n, ensure_ascii=False) + "\n")


def purge_term(cfg: Config, src_term: str, dst_term: str) -> int:
    """清理讨论指定术语但未采用其规范译法的翻译笔记。

    规则：note 的 summary 含术语原文、且不含其规范译文 → 删除（视为过时错译）；
    保留 summary 已含规范译文的笔记。返回删除条数。
    """
    if not src_term or not dst_term:
        return 0
    notes = all_notes(cfg)
    kept = []
    removed = 0
    for n in notes:
        if src_term in (n.get("summary") or "") and dst_term not in (n.get("summary") or ""):
            removed += 1
            continue
        kept.append(n)
    if removed:
        _rewrite(cfg, kept)
    return removed


def purge_keywords(cfg: Config, keywords: list[str]) -> int:
    """清理说明（summary）中含任一指定关键词的翻译笔记。返回删除条数。"""
    kws = [k for k in (keywords or []) if k]
    if not kws:
        return 0
    notes = all_notes(cfg)
    kept = []
    removed = 0
    for n in notes:
        s = n.get("summary") or ""
        if any(k in s for k in kws):
            removed += 1
            continue
        kept.append(n)
    if removed:
        _rewrite(cfg, kept)
    return removed


def purge_segments(cfg: Config, page: str, segment_ids, dry_run: bool = False) -> int:
    """清理指定页面若干段的翻译笔记。返回（将）删除条数。

    仅匹配 page 非空且 segment_id 相等的笔记，故用户注入笔记（page=""）不受影响。
    """
    if not page or not segment_ids:
        return 0
    ids = {str(s) for s in segment_ids}
    notes = all_notes(cfg)
    kept = []
    removed = 0
    for n in notes:
        if n.get("page") and n.get("page") == page \
                and str(n.get("segment_id")) in ids:
            removed += 1
            continue
        kept.append(n)
    if removed and not dry_run:
        _rewrite(cfg, kept)
    return removed


def purge_page(cfg: Config, page: str, dry_run: bool = False) -> int:
    """清理指定页面的全部翻译笔记（不含用户注入笔记）。返回（将）删除条数。"""
    if not page:
        return 0
    notes = all_notes(cfg)
    kept = []
    removed = 0
    for n in notes:
        if n.get("page") == page:
            removed += 1
            continue
        kept.append(n)
    if removed and not dry_run:
        _rewrite(cfg, kept)
    return removed


def relevant(cfg: Config, text: str, limit: int = 8) -> list[dict]:
    """返回与文本相关的笔记（按引文/摘要关键词命中）。"""
    import re

    notes = all_notes(cfg)
    scored = []
    for n in notes:
        hay = (n.get("quote", "") + " " + n.get("summary", ""))
        cnt = sum(len(m.group(0)) for m in re.finditer(r"\S+", text) if m.group(0) in hay)
        if cnt > 0:
            scored.append((cnt, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored[:limit]]
