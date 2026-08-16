"""风格锚定：style_refs 对照样例库、规则抽取 pass、相似样例检索。

style_refs.json 由用户提供（原文-某译者译文对照对）。为空时全部功能自动跳过。
"""
from __future__ import annotations

import os
import re

from . import util
from .config import Config


def load_refs(cfg: Config) -> list[dict]:
    path = cfg.get("style", "refs_path", default="style_refs.json")
    data = util.read_json(path, [])
    if not isinstance(data, list):
        return []
    # 校验并过滤无效项
    valid = []
    for r in data:
        if isinstance(r, dict) and r.get("src") and r.get("dst"):
            valid.append(
                {
                    "src": str(r["src"]),
                    "dst": str(r["dst"]),
                    "source": r.get("source", ""),
                    "note": r.get("note", ""),
                }
            )
    return valid


def guide_path(cfg: Config) -> str:
    return cfg.get("style", "guide_path", default="work/style_guide.md")


def load_guide(cfg: Config) -> str:
    p = guide_path(cfg)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_guide(cfg: Config, text: str) -> None:
    os.makedirs(os.path.dirname(guide_path(cfg)), exist_ok=True)
    with open(guide_path(cfg), "w", encoding="utf-8") as f:
        f.write(text)


def extract_style_guide(cfg: Config, client) -> str:
    """从 style_refs 提炼风格规则，写入 style_guide.md。"""
    refs = load_refs(cfg)
    if not refs:
        return ""
    from . import prompts

    sysp = prompts.build_style_guide_system(cfg)
    usr = prompts.build_style_guide_user(refs)
    guide = client.chat(sysp, usr, temperature=0.3, tag="style_guide")
    save_guide(cfg, guide)
    return guide


def retrieve_exemplars(cfg: Config, text: str, topk: int | None = None) -> list[dict]:
    """基于字符 n-gram 相似度检索风格样例。"""
    refs = load_refs(cfg)
    if not refs:
        return []
    topk = topk or cfg.get("style", "exemplar_topk", default=3)
    scored = []
    for r in refs:
        score = util.dice_coefficient(r["src"], text)
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:topk]]
