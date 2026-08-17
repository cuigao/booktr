"""配置加载与路径解析。

所有相对路径均相对"数据根目录"（data base）解析。数据根即 config.json 所在
目录，由 --data-dir 决定（缺省为 <项目根>/data）。config.json 不包含 data_dir
字段——config 与数据总是同处一处。
绝对路径（如 source_dir 指向 src 之外的镜像）原样返回，不做重定位。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


DEFAULTS: dict[str, Any] = {
    "source_dir": "love.life.coocan.jp",
    "output_dir": "out",
    "work_dir": "work",
    "lang": {"source": "ja", "target": "zh-Hans"},
    "llm": {
        "provider": "mock",  # openai-compatible | mock
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "BOOKTR_API_KEY",
        "temperature": 0.3,
        "max_tokens": 4096,
        "timeout": 120,
        "max_retries": 3,
        "max_requests_per_minute": 60,
        "max_repair": 3,  # 解析失败自愈重试次数
        "max_history_segments": 50,  # 多轮对话保留历史段落数
        "summary_enabled": True,  # 摘要接力开关
    },
    "planner": {
        "static_first": True,
        "diary_chronological": True,
        "context_window": 5,
        "survey_enabled": False,
        "summarize": True,
        "weights": {
            "semantic": 1.0,
            "has_semantic": 0.0,
            "is_orphan": 0.0,
            "is_index": 0.0,
            "hotness": 0.0,
            "depth": 0.0,
            "chrono": 0.0,
            "volume": 0.0,
            "nav": 0.0,
            "dfs": 0.0,
            "len": 0.0,
        },
        "directions": {
            "semantic": "forward",
            "chrono": "forward",
            "volume": "forward",
            "nav": "forward",
            "dfs": "forward",
            "len": "forward",
        },
        "index_threshold": 0.6,
    },
    "segments": {
        "block_tags": [
            "p", "div", "blockquote", "table", "tr", "td", "th", "li", "ul", "ol",
            "h1", "h2", "h3", "h4", "h5", "h6", "center", "pre", "hr",
            "dl", "dt", "dd", "form", "option", "select", "title", "html",
            "head", "body",
        ],
        "translate_alt": True,
        "translate_title": True,
        "placeholder_open": "[[P",
        "placeholder_close": "]]",
        "min_text_len": 1,
    },
    "glossary": {
        "path": "work/glossary.json",
        "auto_extract": True,
        "extract_pages_limit": 0,  # 0 = 全部页面
    },
    "style": {
        "refs_path": "style_refs.json",
        "guide_path": "work/style_guide.md",
        "rules_enabled": True,
        "exemplar_enabled": True,
        "exemplar_topk": 3,
    },
    "notes": {"path": "work/notes.jsonl"},
    "phrases": {"path": "work/phrase_memory.json", "max_len": 30},
    "tm": {"path": "work/tm.jsonl", "enabled": True},
    "qa": {"deep_llm_check": True},
    "translators_notes": {"path": "work/translators_notes.json", "focus": ""},
    "review": {"path": "work/review_queue.json"},
    "state": {"path": "work/state.json"},
    "summaries": {"dir": "work/summaries"},
    "segments_dir": "work/segments",
    "llm_logs": {"dir": "work/llm_logs"},
    "mode": "auto",  # auto | interactive
    "pause_on_review": True,
    "chunk_size": 600,  # 每段最大源字符数，超过则再切分
    "user_rules": "",  # 用户注入的全局翻译规则
}


@dataclass
class Config:
    root: str
    data: dict[str, Any] = field(default_factory=dict)
    data_dir: str | None = None  # 数据根（绝对路径），由 load_config 确定；缺省回退 root/data

    def __post_init__(self) -> None:
        # 深合并默认值，避免共享引用
        merged = json.loads(json.dumps(DEFAULTS))
        self._deep_merge(merged, self.data)
        self.data = merged
        if not self.data_dir:
            self.data_dir = os.path.normpath(os.path.join(self.root, "data"))

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                Config._deep_merge(base[k], v)
            else:
                base[k] = v

    # ---- 基础路径（相对数据根解析） ----
    @property
    def source_dir(self) -> str:
        return self._path(self.data["source_dir"])

    @property
    def output_dir(self) -> str:
        return self._path(self.data["output_dir"])

    @property
    def work_dir(self) -> str:
        return self._path(self.data["work_dir"])

    def _path(self, rel: str) -> str:
        if os.path.isabs(rel):
            return rel
        return os.path.normpath(os.path.join(self.data_dir, rel))

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        # 路径型叶子键（path/dir/_dir 等）自动解析为基于数据根的绝对路径
        if isinstance(cur, str) and keys:
            k = keys[-1].lower()
            if k in ("path", "dir", "directory", "refs_path", "guide_path") or k.endswith("_dir"):
                return self._path(cur)
        return cur

    def set(self, value: Any, *keys: str) -> None:
        cur = self.data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value


def load_config(data_dir: str | None = None) -> Config:
    """加载配置。config.json 位于数据根内。

    data_dir：--data-dir CLI 传入（绝对或相对项目根），缺省为 <root>/data。
    """
    root = find_project_root()
    if data_dir:
        if os.path.isabs(data_dir):
            resolved = os.path.normpath(data_dir)
        else:
            resolved = os.path.normpath(os.path.join(root, data_dir))
    else:
        resolved = os.path.normpath(os.path.join(root, "data"))
    path = os.path.join(resolved, "config.json")
    data: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    cfg = Config(root=root, data=data, data_dir=resolved)
    return cfg


def save_config(cfg: Config, path: str | None = None) -> str:
    """将配置写入文件（默认 <data_dir>/config.json）。"""
    if path is None:
        path = os.path.join(cfg.data_dir, "config.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg.data, f, ensure_ascii=False, indent=2)
    return path


def find_project_root() -> str:
    """基于包路径确定性返回项目根（src/ 目录），与 cwd 无关。

    config.py 位于 <root>/booktr/config.py，故向上两级即项目根。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_dirs(cfg: Config) -> None:
    for d in (
        cfg.data_dir,
        cfg.work_dir,
        cfg.output_dir,
        cfg.get("summaries", "dir", default=""),
        cfg.get("segments_dir", default=""),
        cfg.get("llm_logs", "dir", default=""),
        os.path.dirname(cfg.get("glossary", "path", default="")),
        os.path.dirname(cfg.get("notes", "path", default="")),
        os.path.dirname(cfg.get("tm", "path", default="")),
        os.path.dirname(cfg.get("review", "path", default="")),
        os.path.join(cfg.work_dir, "inbox"),
    ):
        if d:
            os.makedirs(d, exist_ok=True)
