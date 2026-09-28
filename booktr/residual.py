"""残留检测：识别译文里未翻译的源语言片段（当前仅日语·平假名）。

日文专用——检测规则按源语言（``lang.source``）选择。当前仅实现 ``ja``。
检测为只读扫描，产出清单供人工核验后手动 ``reset --segments`` 重译。
"""
from __future__ import annotations

import os
import re

from .config import Config
from . import util

# 平假名（不含片假名 U+30A0–30FF）：专名/歌名括注多为片假名，只查平假名噪声更低。
_HIRA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f]+")

# 成对引号/括注：位于其中视为有意保留（歌名、引用等），检测时按深度跳过。
_BRACKET_OPEN = "\uff08\u300c\u300e\u300a\u3008\uff1c\u201c\u2018(\uff62"
_BRACKET_CLOSE = "\uff09\u300d\u300f\u300b\u3009\uff1e\u201d\u2019)\uff63"
# 对称切换的引号（ASCII 单双引号，无方向）：出现一次进入、再一次退出。
_TOGGLE_QUOTES = "\"'"

# 假名串（扩展为最大日语短语后）其后紧跟这些开括号 → “日文原名（中译）”模式。
_ANNOT_OPEN = "\uff08(\u300a\u3008\uff1c"

# 语境片段截取长度（首尾各若干字符，超出加省略号）。
_EXCERPT_PAD = 24


def _bracket_depths(text: str) -> list[int]:
    """每个字符所处的引号/括注深度（开括号内部深度 > 0）。"""
    depths = [0] * len(text)
    dep = 0
    toggle = 0  # ASCII 引号（无方向）单独计数，出现即切换
    for i, ch in enumerate(text):
        if ch in _TOGGLE_QUOTES:
            toggle ^= 1
            depths[i] = dep + toggle
        elif ch in _BRACKET_OPEN:
            dep += 1
            depths[i] = dep
        elif ch in _BRACKET_CLOSE:
            depths[i] = dep
            dep = max(0, dep - 1)
        else:
            depths[i] = dep + toggle
    return depths


def _is_jp_chunk_char(ch: str) -> bool:
    """是否属于“日语短语”连续块（用于把假名串扩展为完整原名，判断注解）。

    括号/引号一律不算短语字符，避免扩展时吞掉“（中译）”注解。
    """
    if ch in _BRACKET_OPEN or ch in _BRACKET_CLOSE:
        return False
    o = ord(ch)
    if 0x3040 <= o <= 0x30ff:            # 假名
        return True
    if 0x4e00 <= o <= 0x9fff:            # 汉字
        return True
    if 0xff10 <= o <= 0xff19:            # 全角数字
        return True
    if 0xff21 <= o <= 0xff3a or 0xff41 <= o <= 0xff5a:  # 全角拉丁
        return True
    if 0xff01 <= o <= 0xff5e:            # 其他全角符号
        return True
    if ch.isascii() and (ch.isalnum() or ch in "!?~-'&*+=<>."):
        return True
    if ch == "\u3000":                   # 全角空格
        return True
    if ch in "\u30fb\u30fc\u2015\u2010\u2026":  # ・ー―‐…
        return True
    return False


# 判定注解时：这些中文句读表示已进入正文，不再视为“日文原名（中译）”。
_SENTENCE_PUNCT = "\u3002\uff01\uff1f\uff1a\uff1b\n\r"  # 。！？：；


def _has_annotation_after(dst: str, end: int) -> bool:
    """从 ``end`` 起扫描，判断假名串是否属于“日文原名（中译）”，即其后（跨过
    标题内顿号与空格、汉字/假名）紧跟 ``（``。若先遇到中文句末标点则不算。"""
    k = end
    while k < len(dst):
        ch = dst[k]
        if ch in _SENTENCE_PUNCT:
            return False
        if ch in _ANNOT_OPEN:
            return True
        if ch in _BRACKET_OPEN or ch in _BRACKET_CLOSE or ch in _TOGGLE_QUOTES \
                or dst.startswith("[[P", k):
            return False
        if ch in " \u3000\u3001\uff0c":
            k += 1
            continue
        if _is_jp_chunk_char(ch):
            k += 1
            continue
        return False
    return False


def _excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - _EXCERPT_PAD)
    hi = min(len(text), end + _EXCERPT_PAD)
    head = "\u2026" if lo > 0 else ""
    tail = "\u2026" if hi < len(text) else ""
    return f"{head}{text[lo:hi]}{tail}"


def find_residual(src: str, dst: str) -> list[dict]:
    """返回译文 ``dst`` 中的残留平假名条目（纯函数）。

    判定：假名串同时出现在原文、且不位于引号/括注内、且不属于
    “日文原名（中译）”模式。

    返回 [{"token", "start", "excerpt"}]。
    """
    if not src or not dst:
        return []
    depths = _bracket_depths(dst)
    out: list[dict] = []
    for m in _HIRA_RE.finditer(dst):
        token = m.group()
        if token not in src:
            continue
        if depths[m.start()] > 0:
            continue
        # 扩展为最大日语短语后，若其后（跨标题内标点）紧跟注解开括号 → 有意保留的原名
        if _has_annotation_after(dst, m.end()):
            continue
        out.append({
            "token": token,
            "start": m.start(),
            "excerpt": _excerpt(dst, m.start(), m.end()),
        })
    return out


def check_segment(src: str, dst: str) -> list[dict]:
    """兼容别名：对单段做检测。"""
    return find_residual(src, dst)


def _load_segments(cfg: Config, rel: str) -> list[dict]:
    """读取某页段缓存（兼容 dict 包装与旧 list 格式）。"""
    path = os.path.join(cfg.get("segments_dir", default=""),
                        rel.replace("/", "__") + ".json")
    data = util.read_json(path, [])
    if isinstance(data, dict):
        return data.get("segments", [])
    return data if isinstance(data, list) else []


def scan(cfg: Config, pages: list[str] | None = None,
         data_dir: str | None = None) -> list[dict]:
    """扫描已译页面，返回按页分组的残留清单。

    每页：{"page", "reset", "items": [{"segment_id", "tokens", "excerpts"}]}。
    ``data_dir`` 非空时，reset 命令前置 ``--data-dir "..."``，避免照抄执行到错误数据根。
    """
    from .translate import State, STATUS

    state = State(cfg)
    if pages is None:
        pages = [rel for rel, p in state.data.get("pages", {}).items()
                 if p.get("status") in (STATUS["done"], STATUS["review"])]

    dd = f'--data-dir "{data_dir}" ' if data_dir else ""
    results: list[dict] = []
    for rel in pages:
        segs = _load_segments(cfg, rel)
        if not segs:
            continue
        items: list[dict] = []
        seg_ids: list[str] = []
        for s in segs:
            dst = s.get("translation")
            src = s.get("text") or s.get("source_text") or ""
            if not dst:
                continue
            hits = find_residual(src, dst)
            if not hits:
                continue
            sid = str(s.get("id"))
            items.append({
                "segment_id": s.get("id"),
                "tokens": [h["token"] for h in hits],
                "excerpts": [h["excerpt"] for h in hits],
            })
            seg_ids.append(sid)
        if not items:
            continue
        reset = (f"python booktr-cli.py {dd}reset {rel} --segments "
                 + " ".join(seg_ids))
        results.append({"page": rel, "reset": reset, "items": items})
    return results


# 源语言 → 检测函数。当前仅日语；其他源语言无对应规则。
SUPPORTED_SOURCE_LANGS: dict[str, object] = {"ja": find_residual}
