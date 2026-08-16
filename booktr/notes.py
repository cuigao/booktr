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
