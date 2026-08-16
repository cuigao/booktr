"""通用工具：编码探测、文件读写、JSON 辅助。"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

_JAP_CHAR = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def count_japanese(text: str) -> int:
    return len(_JAP_CHAR.findall(text))


def detect_encoding(raw: bytes, declared: str | None = None) -> str:
    """探测文件编码。

    优先按 <meta charset> 声明的编码尝试；否则优先 Shift-JIS(cp932)，
    若严格解码失败则回退 UTF-8。
    """
    declared = (declared or "").lower()
    for cand in (declared, "cp932", "utf-8"):
        if not cand:
            continue
        try:
            raw.decode(cand)
            return cand
        except (UnicodeDecodeError, LookupError):
            continue
    return "cp932"


def decode_html(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 编码)。"""
    declared = None
    m = re.search(
        rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", raw, re.IGNORECASE
    )
    if m:
        declared = m.group(1).decode("ascii", errors="ignore")
    enc = detect_encoding(raw, declared)
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
