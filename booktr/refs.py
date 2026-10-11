"""人工参考译文参考层。

职责：
- 读取整页人工参考译文（`work/refs/` 或 `refs.dir` 镜像目录）。
- 运行时把某段译文与参考译文做定位 + 相似度/diff（不预对齐）。
- 勘误 sidecar（append-only）：记录"参考译文有误 → 修正"的可追溯点；
  `reset`/`clean` 不清理，失效标 `superseded` 保留。

设计见 `instance/plan/refs_layer_plan.md`。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import unicodedata
import uuid

from . import util
from .config import Config

_PH_RE = re.compile(r"\[\[P\d+\]\]")


# ── 文本工具 ──────────────────────────────────────────────────────────
def strip_placeholders(text: str) -> str:
    return _PH_RE.sub("", text or "")


def plain(text: str) -> str:
    """去占位符 + 折叠空白（含全角），用于相似度/diff。"""
    return re.sub(r"[\s\u3000]+", "", strip_placeholders(text))


_PUNCT_MAP = (
    ("…", "..."), ("‥", "..."), ("——", "-"), ("―", "-"),
    ("〜", "~"), ("～", "~"),
    ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
    ("（", "("), ("）", ")"),
)


def canon(text: str) -> str:
    """比对用归一化（**不改输出**）：去占位符 → NFKC 全半角 → 标点统一 → 折叠空白。"""
    t = strip_placeholders(text or "")
    t = unicodedata.normalize("NFKC", t)
    for a, b in _PUNCT_MAP:
        t = t.replace(a, b)
    return re.sub(r"[\s\u3000]+", "", t)


def similarity(a: str, b: str, metric: str = "ratio") -> float:
    """归一化相似度 [0,1]。metric: ratio | edit | dice。"""
    a, b = a or "", b or ""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if metric == "dice":
        return util.dice_coefficient(a, b)
    if metric == "edit":
        return 1.0 - _levenshtein(a, b) / max(len(a), len(b))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def locate_fragment(ref_text: str, out_text: str) -> dict:
    """在参考纯文本中定位与 out 最匹配的连续片段（plain 化后）。"""
    return _locate(plain(ref_text), plain(out_text))


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于判断参考是否覆盖本段）。"""
    if not a or not b:
        return 0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b)).size


def locate_plain(ref_plain: str, out_plain: str) -> dict:
    """同 locate_fragment，但输入已是 plain/canon 文本（不再处理）。"""
    return _locate(ref_plain, out_plain)


def _locate(rp: str, op: str) -> dict:
    """在参考纯文本 rp 中定位与 op 最匹配的连续片段（锚定 + 邻域微移）。

    返回片段会**扩展到句末边界**（避免截断到句子中间）；score 为原窗口相似度。
    """
    if not op:
        return {"fragment": "", "score": 0.0}
    if op in rp:
        return {"fragment": op, "score": 1.0}
    n = len(op)
    sm = difflib.SequenceMatcher(None, op, rp, autojunk=False)
    blk = sm.find_longest_match(0, len(op), 0, len(rp))
    base = blk.b - blk.a
    hi = max(0, len(rp) - n)
    best = (0.0, "", 0)
    for off in range(-8, 9):
        s = max(0, min(base + off, hi))
        w = rp[s:s + n]
        sc = difflib.SequenceMatcher(None, op, w).ratio()
        if sc > best[0]:
            best = (sc, w, s)
    frag = _expand_sentence(rp, best[2], best[2] + n, cap=len(op) * 4 + 40)
    return {"fragment": frag, "score": best[0]}


_SENT_ENDS = ("\u3002", "\uff01", "\uff1f", "!", "?", "\n")


def _expand_sentence(rp: str, s: int, e: int, cap: int = 0) -> str:
    """把窗口 [s,e) 小幅扩展到句末边界（避免截断在句中），左右最多各 15 字。"""
    margin = 15
    start = s
    for ch in _SENT_ENDS:
        p = rp.rfind(ch, max(0, s - margin), s)
        if p >= 0:
            start = max(start, p + 1)
    right = e
    best = None
    for ch in _SENT_ENDS:
        p = rp.find(ch, e, min(len(rp), e + margin))
        if p >= 0:
            best = (p + 1) if best is None else min(best, p + 1)
    if best is not None:
        right = best
    return rp[start:right]


def diff_text(ref_frag: str, out_text: str) -> str:
    """参考片段 → 现有译文 的字符级内联 diff（供重译 LLM 参考）。

    ``[-...-]`` = 参考有而现有缺失；``[+...+ ]`` = 现有新增/改动。
    """
    a, b = plain(ref_frag), plain(out_text)
    sm = difflib.SequenceMatcher(None, a, b)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(a[i1:i2])
        elif tag == "delete":
            parts.append(f"[-{a[i1:i2]}-]")
        elif tag == "insert":
            parts.append(f"[+{b[j1:j2]}+]")
        else:
            parts.append(f"[-{a[i1:i2]}-][+{b[j1:j2]}+]")
    return "".join(parts)


# ── 参考译文读取 ──────────────────────────────────────────────────────
def _candidates(cfg: Config, rel: str) -> list[str]:
    rel_sep = rel.replace("/", os.sep)
    rel_flat = rel.replace("/", "__")
    names = []
    for base in (
        cfg.get("refs", "store_dir", default="work/refs"),
        cfg.get("refs", "dir", default=""),
    ):
        if not base:
            continue
        names += [
            os.path.join(base, rel_sep),
            os.path.join(base, _swap_ext(rel_sep)),
            os.path.join(base, rel_flat),
            os.path.join(base, rel_flat.rsplit(".", 1)[0] + ".txt"),
        ]
    return names


def _swap_ext(path: str) -> str:
    root, ext = os.path.splitext(path)
    return root + ".txt" if ext else path + ".txt"


def load_page(cfg: Config, rel: str) -> str | None:
    """读取某页的整页人工参考译文；不存在返回 None。"""
    for p in _candidates(cfg, rel):
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    return _normalize_ref(f.read())
            except OSError:
                continue
    return None


def _normalize_ref(text: str) -> str:
    """规范化：去 BOM、CRLF→LF、去首尾空白、压缩多余空行。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ── 校验 ──────────────────────────────────────────────────────────────
def _seg(seg, name: str, default=""):
    val = getattr(seg, name, None)
    if val is None and isinstance(seg, dict):
        val = seg.get(name)
    return default if val is None else val


def kind_weight(cfg: Config, kind: str) -> float:
    """结构段（标题/导航）默认权重 0；可用 `refs.weights` 覆盖。"""
    weights = cfg.get("refs", "weights", default={}) or {}
    if kind in weights:
        try:
            return float(weights[kind])
        except (TypeError, ValueError):
            return 1.0
    return 0.0 if kind in ("head_title", "attr_title", "attr_alt") else 1.0


def _restore_corrections(ref_c: str, out_text: str, corr_list: list[dict]) -> tuple[str, int]:
    """把本段已应用的纠错还原成参考原句（corrected→ref_err），返回 (canon 文本, 处数)。

    仅当该纠错的 `corrected` 出现在本段输出、且 `ref_err` 在参考中存在时才施加
    （即"归属本段"的纠错）；否则视为他段纠错、忽略。
    """
    oc = canon(out_text)
    applied = 0
    for c in corr_list or []:
        if c.get("status") not in (None, "active"):
            continue
        re_ = canon(c.get("ref_err", ""))
        co_ = canon(c.get("corrected", ""))
        if not re_ or not co_:
            continue
        if co_ in oc and re_ in ref_c:
            oc = oc.replace(co_, re_, 1)
            applied += 1
    return oc, applied


def trailing_orphan(cfg: Config, rel: str, segs: list) -> str:
    """参考译文末尾不被任何段译文覆盖的连续块（如页尾彩蛋/注释/额外板块）。

    以"位置"判定覆盖：把参考按行摊平，段译文与参考的最长/公共匹配块所在行即视为
    已覆盖；从参考末尾向前扫描到第一个已覆盖行，返回其后的原始文本块。
    """
    ref = load_page(cfg, rel)
    if not ref:
        return ""
    lines = ref.split("\n")
    rp = ""                      # 参考的 canon 拼接
    line_of: list[int] = []      # rp 每个字符所属行号
    for ln, line in enumerate(lines):
        p = canon(line)
        rp += p
        line_of += [ln] * len(p)
    if not rp:
        return ""
    covered: set[int] = set()
    for s in segs:
        if _seg(s, "kind", "") != "text":
            continue
        op = canon(_seg(s, "translation", "") or "")
        if not op:
            continue
        sm = difflib.SequenceMatcher(None, op, rp, autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            if size < 4:
                continue
            for i in range(b, b + size):
                covered.add(line_of[i])
    tail: list[str] = []
    for ln in range(len(lines) - 1, -1, -1):
        if not lines[ln].strip():
            if tail:
                tail.append(lines[ln])
            continue
        if ln in covered:
            break
        tail.append(lines[ln])
    tail.reverse()
    return "\n".join(tail).strip()


def check_page(cfg: Config, rel: str, segs: list, state=None,
               corrections: dict | None = None) -> list[dict]:
    """对该页已译段做参考一致性校验，返回不达标清单。

    仅处理 ``kind == "text"``、去占位符后长度 ≥ ``min_seg_chars``、权重 > 0 的段。
    比对前：去占位符 + `canon` 归一（全半角/标点），并把"归属本段"的 active 纠错
    还原为参考原句（`corrected→ref_err`）。返回每项
    ``{sid, score, fragment, diff, corrections_applied, covered}``。纯函数（只读）。
    """
    ref = load_page(cfg, rel)
    if not ref:
        return []
    threshold = float(cfg.get("refs", "threshold", default=0.97))
    min_chars = int(cfg.get("refs", "min_seg_chars", default=4))
    ref_c = canon(ref)
    corr_map = corrections or {}
    out = []
    for seg in segs:
        kind = _seg(seg, "kind", "text")
        if kind != "text" or kind_weight(cfg, kind) <= 0:
            continue
        trans = _seg(seg, "translation", "") or ""
        src_plain = plain(_seg(seg, "text", ""))
        if len(src_plain) < min_chars or not trans.strip():
            continue
        sid = str(_seg(seg, "id", ""))
        restored, applied = _restore_corrections(ref_c, trans, corr_map.get(sid, []))
        loc = locate_plain(ref_c, restored)
        if loc["score"] < threshold:
            covered = _lcs_len(restored, ref_c) >= max(4, len(restored) // 3)
            out.append({
                "sid": sid,
                "score": round(loc["score"], 4),
                "fragment": loc["fragment"],
                "diff": diff_text(loc["fragment"], restored),
                "corrections_applied": applied,
                "covered": covered,
            })
    return out


# ── 勘误 sidecar ──────────────────────────────────────────────────────
def corrections_path(cfg: Config) -> str:
    return cfg.get("refs", "corrections_path", default="work/ref_corrections.jsonl")


def _read_all(cfg: Config) -> list[dict]:
    return util.read_jsonl(corrections_path(cfg))


def _write_all(cfg: Config, items: list[dict]) -> None:
    path = corrections_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def active_corrections(cfg: Config, rel: str) -> list[dict]:
    return [c for c in _read_all(cfg)
            if c.get("page") == rel and c.get("status") == "active"]


def all_active(cfg: Config) -> dict[str, list[dict]]:
    """按页分组返回所有 active 勘误。"""
    out: dict[str, list[dict]] = {}
    for c in _read_all(cfg):
        if c.get("status") == "active":
            out.setdefault(c.get("page"), []).append(c)
    return out


def record_corrections(cfg: Config, rel: str, sid, corrections: list[dict],
                        op_id: str = "", model: str = "") -> int:
    """把一段的纠错记录追加到 sidecar（append-only）。返回新增条数。"""
    n = 0
    path = corrections_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for c in corrections or []:
            if not isinstance(c, dict):
                continue
            ref_err = str(c.get("ref_err") or "").strip()
            corrected = str(c.get("corrected") or "").strip()
            if not ref_err or not corrected:
                continue
            rec = {
                "id": uuid.uuid4().hex[:12],
                "page": rel,
                "segment_id": str(sid),
                "ref_err": ref_err,
                "corrected": corrected,
                "reason": str(c.get("reason") or "").strip(),
                "op_id": op_id,
                "model": model,
                "ts": int(time.time() * 1000),
                "status": "active",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def supersede_segment(cfg: Config, rel: str, sid) -> int:
    """把该段既有 active 勘误标为 superseded（保留可追溯）。返回改动条数。"""
    items = _read_all(cfg)
    n = 0
    for it in items:
        if (it.get("page") == rel and str(it.get("segment_id")) == str(sid)
                and it.get("status") == "active"):
            it["status"] = "superseded"
            it["superseded_ts"] = int(time.time() * 1000)
            n += 1
    if n:
        _write_all(cfg, items)
    return n


def inline_annotation(corrected: str, ref_err: str) -> str:
    """可读内联注记（备选渲染）。"""
    return f"{corrected}（原译文有误：{ref_err}）"

