"""通用工具：编码探测、文件读写、JSON 辅助。"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

_JAP_CHAR = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE
)


class EncodingError(RuntimeError):
    """无法识别页面编码（所有候选编码均无法严格解码）。"""


# 语言 → 常见编码候选（早期网站常见），硬编码。候选顺序即尝试优先级。
LANG_ENCODINGS: dict[str, list[str]] = {
    # cp932 即微软 Shift-JIS 扩展，比严格 shift_jis 更宽容，兼容更多日文老站
    "ja": ["cp932", "euc_jp", "iso2022_jp"],
    "zh-Hans": ["gbk", "gb2312"],
    "zh-Hant": ["big5"],
    "zh": ["gbk", "big5"],
    "ko": ["euc_kr", "cp949"],
    "ru": ["cp1251", "koi8_r"],
    "en": ["cp1252", "iso8859-1"],
}

# 未预设语言的合理回退列表（utf-8 兜底在最前，现代站点通用）。
DEFAULT_ENCODINGS: list[str] = ["utf-8", "cp1252", "latin1"]


def count_japanese(text: str) -> int:
    return len(_JAP_CHAR.findall(text))


def _encoding_candidates(declared: str | None, lang: str) -> list[str]:
    """构造编码候选列表：meta 声明优先 → 语言常见编码 → utf-8 兜底。"""
    cands: list[str] = []
    if declared:
        cands.append(declared)
    cands.extend(LANG_ENCODINGS.get(lang, DEFAULT_ENCODINGS))
    if "utf-8" not in cands:
        cands.append("utf-8")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        lc = c.lower()
        if lc not in seen:
            seen.add(lc)
            out.append(c)
    return out


def detect_encoding(raw: bytes, declared: str | None, lang: str) -> str | None:
    """按候选优先级尝试严格解码，返回能解码的编码；全部失败返回 None。

    候选顺序：<meta charset> 声明 → 语言常见编码 → utf-8 兜底。
    """
    for cand in _encoding_candidates(declared, lang):
        try:
            raw.decode(cand)
            return cand
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def decode_html(raw: bytes, lang: str) -> tuple[str, str]:
    """严格解码，返回 (文本, 编码)。无法解码抛 EncodingError（不静默替换）。"""
    declared = None
    m = _META_CHARSET_RE.search(raw)
    if m:
        declared = m.group(1).decode("ascii", errors="ignore")
    enc = detect_encoding(raw, declared, lang)
    if enc is None:
        raise EncodingError(
            f"无法识别编码（lang={lang}，候选={_encoding_candidates(declared, lang)}）"
        )
    return raw.decode(enc), enc


def decode_html_loose(raw: bytes, lang: str) -> tuple[str, str]:
    """宽松解码（errors='replace'），用于 fix 工具修复坏文件。无法解码时兜底 utf-8。"""
    declared = None
    m = _META_CHARSET_RE.search(raw)
    if m:
        declared = m.group(1).decode("ascii", errors="ignore")
    enc = detect_encoding(raw, declared, lang) or "utf-8"
    return raw.decode(enc, errors="replace"), enc


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str, data: Any, indent: int = 2) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def read_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def append_jsonl(path: str, item: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def ngram(text: str, n: int = 2) -> list[str]:
    """字符 n-gram，用于相似度计算。跳过空白与标点。"""
    cleaned = re.sub(r"[\s\u3000,。、.!?！？'\"「」『』（）()【】\[\]\-\n]", "", text)
    if len(cleaned) < n:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + n] for i in range(len(cleaned) - n + 1)]


def dice_coefficient(a: str, b: str, n: int = 2) -> float:
    """基于字符 n-gram 的 Dice 系数，0..1。"""
    ga, gb = set(ngram(a, n)), set(ngram(b, n))
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return 2.0 * inter / (len(ga) + len(gb))


def extract_bracketed(text: str) -> list[str]:
    """提取文本中的日文括号内容，用于关键词抓取。"""
    return re.findall(r"[（(]([^（）()\n]{1,60})[）)]", text)


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def normalize_ws(text: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", text).strip()


def char_len(s: str) -> int:
    return len(s)


def is_mostly_ascii(text: str) -> bool:
    if not text:
        return True
    non_ascii = sum(1 for ch in text if ord(ch) > 0x2E)
    return non_ascii / len(text) < 0.3


def east_asian_width(text: str) -> int:
    """估算文本显示宽度（中日韩全角按2）。"""
    w = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w
