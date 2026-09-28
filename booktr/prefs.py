"""偏好文件：跨实例复用的个人翻译偏好（不入库，显式指定路径）。

白名单仅含 user_rules / glossary / style_refs——不含数据路径、模型、语言等
实例专属配置。用于重建实例后快速恢复个人偏好。
"""
from __future__ import annotations

import json
import os

from . import glossary as gl
from . import util
from .config import Config

PREF_VERSION = 1
PREF_KEYS = ("user_rules", "glossary", "style_refs")


def collect(cfg: Config) -> dict:
    """从当前实例收集偏好（白名单内容）。"""
    refs_path = cfg.get("style", "refs_path", default="style_refs.json")
    return {
        "version": PREF_VERSION,
        "user_rules": cfg.get("user_rules", default="") or "",
        "glossary": gl.load(cfg),
        "style_refs": util.read_json(refs_path, []),
    }


def export(cfg: Config, path: str) -> str:
    """将当前实例偏好写入指定文件路径。返回写入路径。"""
    if not path:
        raise ValueError("必须指定偏好文件路径")
    data = collect(cfg)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load(path: str) -> dict:
    """读取偏好文件。"""
    if not os.path.exists(path):
        raise ValueError(f"偏好文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"偏好文件格式错误（应为 JSON 对象）: {path}")
    return data


def apply_data_files(cfg: Config, prefs: dict) -> dict:
    """把偏好中的 glossary/style_refs 写入实例数据文件。

    user_rules 不在此处理：由 init 在「先偏好、后风格」流程中合并写入 config。
    返回摘要：{glossary: int, style_refs: int}。
    """
    summary = {"glossary": 0, "style_refs": 0}
    gl_items = prefs.get("glossary")
    if isinstance(gl_items, list):
        gl.save(cfg, gl_items)
        summary["glossary"] = len(gl_items)
    refs = prefs.get("style_refs")
    if isinstance(refs, list):
        refs_path = cfg.get("style", "refs_path", default="style_refs.json")
        util.write_json(refs_path, refs)
        summary["style_refs"] = len(refs)
    return summary

