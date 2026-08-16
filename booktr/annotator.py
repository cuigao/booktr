"""译者注：发现跨页关联与趣味细节，输出外部 JSON。"""
from __future__ import annotations

import os
import uuid

from . import llm as llm_mod
from . import prompts
from . import segments as seg_mod
from . import util
from .config import Config


def _notes_path(cfg: Config) -> str:
    return cfg.get("translators_notes", "path", default="work/translators_notes.json")


def load(cfg: Config) -> list[dict]:
    return util.read_json(_notes_path(cfg), [])


def add(cfg: Config, entry: dict) -> None:
    items = load(cfg)
    entry["id"] = uuid.uuid4().hex[:12]
    items.append(entry)
    util.write_json(_notes_path(cfg), items)


def generate_for_page(cfg: Config, client, rel: str, translated: str) -> int:
    """为单页生成译者注。返回新增条数。"""
    summaries = {}
    sdir = cfg.get("summaries", "dir", default="")
    if os.path.isdir(sdir):
        for fn in os.listdir(sdir):
            if fn.endswith(".json"):
                rel_key = fn[:-5].replace("__", "/")
                data = util.read_json(os.path.join(sdir, fn), {})
                if data.get("summary"):
                    summaries[rel_key] = data["summary"]
    sysp = prompts.build_translator_note_system(cfg)
    usr = prompts.build_translator_note_user(rel, translated[:5000], summaries)
    resp = client.chat(sysp, usr, temperature=0.6)
    try:
        data = llm_mod.parse_json_response(resp)
    except llm_mod.LLMError:
        return 0
    count = 0
    for n in data.get("notes", []) or []:
        content = n.get("content", "").strip()
        if not content:
            continue
        add(cfg, {
            "anchor": {
                "page": rel,
                "quote": translated[:120],
                "segment_id": None,
            },
            "content": content,
            "related_pages": n.get("related_pages", []) or [],
            "type": n.get("type", "其他"),
            "user_focus": cfg.get("translators_notes", "focus", default=""),
        })
        count += 1
    return count
