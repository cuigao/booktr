"""术语初筛与语义辨析（terms-scan）。

零词条起步：先用**机械算法**从原文全站抽取"高频重复"候选（脚本串/跨页模板/
n-gram 统计等），再交 **LLM 语义辨析**判定价值并给出建议译名，最终并入词汇表
（status=auto-candidate，待人工确认后才用于翻译）。

设计依据（阶段 0 摸底）：真实语料上 `runs`（最大脚本串频次）召回最优、
`repeat_lines`（跨页重复整行）补充模板；`cvalue/entropy/pmi/bpe` 在本规模语料
噪声偏大，默认关闭但保留可插拔。
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict

from . import crawler
from . import llm as llm_mod
from . import prompts
from . import util
from .config import Config

KAT = r"\u30a1-\u30fa\u30fc"
KAN = r"\u4e00-\u9fff"
LAT = r"A-Za-z"


def _load_texts(cfg: Config, pages: list[str] | None = None) -> dict[str, str]:
    """返回 {rel: 去标签正文}（缺失/解码失败跳过）。"""
    sm = util.read_json(os.path.join(cfg.work_dir, "site_map.json"), {})
    rels = pages if pages else list(sm.get("pages", {}))
    out = {}
    for rel in rels:
        try:
            html, _ = crawler.decode_page(cfg, rel)
        except Exception:
            continue
        s = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        s = re.sub(r"<script.*?</script>", "", s, flags=re.S | re.I)
        s = re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ")
        out[rel] = s
    return out


def _script(t: str) -> str:
    if re.search(r"[\u3040-\u30ff]", t):
        return "kana"
    if re.search(rf"[{LAT}]", t):
        return "latin"
    if re.search(rf"[{KAN}]", t):
        return "han"
    return "other"


# --------------------------------------------------------------------------
# 算法（每个返回 {src: {"count","pages"(list),"score"?}}）
# --------------------------------------------------------------------------
_MIXCH = rf"{KAT}A-Za-z0-9'’&._\-"


def algo_runs(pages: dict[str, str]) -> dict:
    """最大脚本串频次（纯拉丁/片假名/汉字 + 片假名·拉丁混排）。

    混排规则：以片假名/拉丁/数字组成的最大连续串中**同时含片假名与拉丁/数字**
    者（如 `プライベートCD`、`NKホール`）——避免把纯假名语法串误并入。
    """
    pat = re.compile(
        rf"[{LAT}][A-Za-z0-9'’&._\-]*(?:[ ][A-Za-z0-9'’&._\-]+)*"
        rf"|[{KAT}]{{2,}}|[{KAN}]{{2,}}")
    mix = re.compile(rf"[{_MIXCH}]{{2,}}")
    has_kat = re.compile(rf"[{KAT}]")
    has_lat = re.compile(r"[A-Za-z0-9]")
    cnt = Counter()
    pg = defaultdict(set)
    for rel, t in pages.items():
        for m in pat.findall(t):
            m = m.strip()
            if len(m) >= 2:
                cnt[m] += 1
                pg[m].add(rel)
        for m in mix.findall(t):
            if has_kat.search(m) and has_lat.search(m):
                cnt[m] += 1
                pg[m].add(rel)
    return {m: {"count": c, "pages": sorted(pg[m])} for m, c in cnt.items()}


def algo_repeat_lines(pages: dict[str, str]) -> dict:
    cnt = Counter()
    pg = defaultdict(set)
    for rel, t in pages.items():
        for line in re.split(r"[\r\n]+", t):
            line = re.sub(r"\s+", "", line)
            if 2 <= len(line) <= 60 and re.search(rf"[{LAT}{KAT}{KAN}]", line):
                cnt[line] += 1
                pg[line].add(rel)
    return {m: {"count": c, "pages": sorted(pg[m])} for m, c in cnt.items()}


def algo_cvalue(pages: dict[str, str]) -> dict:
    ng = Counter()
    for t in pages.values():
        for r in re.findall(rf"[{KAN}]{{2,}}", t):
            L = len(r)
            for n in range(2, min(L, 6) + 1):
                for i in range(L - n + 1):
                    ng[r[i:i + n]] += 1
    out = {}
    for g in ng:
        if ng[g] < 3:
            continue
        longer = [h for h in ng if len(h) > len(g) and g in h]
        base = math.log2(len(g)) * ng[g]
        cv = base if not longer else \
            math.log2(len(g)) * (ng[g] - sum(ng[h] for h in longer) / len(longer))
        out[g] = {"count": ng[g], "score": round(cv, 2)}
    return out


def algo_bpe(pages: dict[str, str], iters: int = 400, min_count: int = 4) -> dict:
    seq = [ch if re.match(rf"[{KAT}]", ch) else "\x00"
           for t in pages.values() for ch in t]
    for _ in range(iters):
        pairs = Counter()
        for i in range(len(seq) - 1):
            if seq[i] != "\x00" and seq[i + 1] != "\x00":
                pairs[(seq[i], seq[i + 1])] += 1
        if not pairs:
            break
        (a, b), c = pairs.most_common(1)[0]
        if c < min_count:
            break
        new, nseq, i = a + b, [], 0
        while i < len(seq):
            if i < len(seq) - 1 and seq[i] == a and seq[i + 1] == b:
                nseq.append(new); i += 2
            else:
                nseq.append(seq[i]); i += 1
        seq = nseq
    cnt = Counter(tok for tok in seq
                  if tok != "\x00" and re.search(rf"[{KAT}]", tok))
    return {m: {"count": c} for m, c in cnt.items() if len(m) >= 2}


def algo_entropy(pages: dict[str, str], min_count: int = 5) -> dict:
    stream = "".join(ch if re.match(rf"[{KAN}]", ch) else "\x00"
                     for t in pages.values() for ch in t)
    cand = set()
    for r in re.findall(rf"[{KAN}]{{2,}}", "".join(pages.values())):
        for n in range(2, min(len(r), 6) + 1):
            for i in range(len(r) - n + 1):
                cand.add(r[i:i + n])

    def count_ent(sub):
        Lc, Rc = Counter(), Counter()
        s = 0
        while True:
            i = stream.find(sub, s)
            if i < 0:
                break
            Lc[stream[i - 1] if i > 0 else "\x00"] += 1
            j = i + len(sub)
            Rc[stream[j] if j < len(stream) else "\x00"] += 1
            s = i + 1

        def H(c):
            T = sum(c.values())
            return -sum(v / T * math.log2(v / T) for v in c.values()) if T else 0.0
        return H(Lc), H(Rc)

    out = {}
    for g in cand:
        c = sum(1 for _ in re.finditer(re.escape(g), stream))
        if c < min_count:
            continue
        hl, hr = count_ent(g)
        out[g] = {"count": c, "score": round(min(hl, hr), 3)}
    return out


def algo_pmi(pages: dict[str, str], min_count: int = 4) -> dict:
    out = {}
    for name, rgx in (("kana", rf"[{KAT}]"), ("han", rf"[{KAN}]")):
        stream = "".join(ch if re.match(rgx, ch) else "\x00"
                         for t in pages.values() for ch in t)
        uni, bi = Counter(), Counter()
        for ch in stream:
            if ch != "\x00":
                uni[ch] += 1
        for i in range(len(stream) - 1):
            if stream[i] != "\x00" and stream[i + 1] != "\x00":
                bi[stream[i:i + 2]] += 1
        tot, unitot = sum(bi.values()), sum(uni.values())
        for g, c in bi.items():
            if c < min_count:
                continue
            pa = uni[g[0]] / max(unitot, 1)
            pb = uni[g[1]] / max(unitot, 1)
            if pa and pb:
                out[g] = {"count": c,
                          "score": round(math.log2((c / tot) / (pa * pb)), 3),
                          "script": name}
    return out


ALGOS = {
    "runs": algo_runs,
    "repeat_lines": algo_repeat_lines,
    "cvalue": algo_cvalue,
    "bpe": algo_bpe,
    "entropy": algo_entropy,
    "pmi": algo_pmi,
}
DEFAULT_ALGOS = ("runs", "repeat_lines")


def extract(cfg: Config, pages: list[str] | None = None,
            algos=DEFAULT_ALGOS, min_count: int = 3, min_pages: int = 2,
            context_chars: int = 40, max_len: int = 40, top: int = 0,
            texts: dict[str, str] | None = None) -> list[dict]:
    """机械初筛候选。

    返回列表，每项 ``{src, script, count, pages, contexts, algos, score}``。
    ``count`` 为合并后最大出现次数；``pages`` 为出现页列表；``contexts`` 为
    源文窗口片段（最多 3 条，方案 a）。
    """
    if texts is None:
        texts = _load_texts(cfg, pages)
    merged: dict[str, dict] = {}
    for name in algos:
        fn = ALGOS.get(name)
        if not fn:
            continue
        for src, info in fn(texts).items():
            if len(src) > max_len:
                continue
            rec = merged.setdefault(src, {"src": src, "script": _script(src),
                                          "count": 0, "pages": set(),
                                          "score": 0.0, "algos": []})
            rec["count"] = max(rec["count"], info.get("count", 1))
            rec["pages"] |= set(info.get("pages", []))
            rec["score"] = max(rec["score"], info.get("score", 0.0))
            rec["algos"].append(name)
    # 去重：按"去空白归一"合并同一词的不同书写（如 NEXT PAGE / NEXTPAGE）
    by_norm: dict[str, dict] = {}
    for src, rec in merged.items():
        norm = re.sub(r"\s+", "", src).lower()
        prev = by_norm.get(norm)
        if prev is None:
            by_norm[norm] = rec
            continue
        # 保留信息更全者：优先含空格的原形，其次 count/页数更高
        prefer_new = (" " in src and " " not in prev["src"]) or \
            ((" " in src) == (" " in prev["src"]) and
             (rec["count"], len(rec["pages"])) > (prev["count"], len(prev["pages"])))
        if prefer_new:
            rec["pages"] = rec["pages"] | prev["pages"]
            rec["count"] = max(rec["count"], prev["count"])
            rec["algos"] = list(set(rec["algos"]) | set(prev["algos"]))
            by_norm[norm] = rec
        else:
            prev["pages"] = prev["pages"] | rec["pages"]
            prev["count"] = max(prev["count"], rec["count"])
            prev["algos"] = list(set(prev["algos"]) | set(rec["algos"]))

    out = []
    for rec in by_norm.values():
        if rec["count"] < min_count and len(rec["pages"]) < min_pages:
            continue
        rec["pages"] = sorted(rec["pages"])
        rec["contexts"] = _contexts(texts, rec["src"], context_chars, limit=3)
        out.append(rec)
    out.sort(key=lambda x: (x["count"], len(x["pages"])), reverse=True)
    return out[:top] if top else out


def _contexts(texts: dict[str, str], term: str, pad: int, limit: int = 3) -> list[str]:
    """取 term 在源文中的窗口片段（去换行压缩），最多 limit 条。"""
    out = []
    seen_pages = set()
    for rel, t in texts.items():
        if term not in t or rel in seen_pages:
            continue
        i = t.find(term)
        frag = t[max(0, i - pad): i + len(term) + pad]
        frag = re.sub(r"\s+", " ", frag).strip()
        out.append(frag)
        seen_pages.add(rel)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# LLM 语义辨析
# --------------------------------------------------------------------------
def review(cfg: Config, client, candidates: list[dict],
           batch_size: int = 40) -> list[dict]:
    """分批交 LLM 判定候选价值，返回保留项 [{src, dst, note, category, reason}]。

    注入**现行词汇表**与**用户规则**，避免与既有译法冲突（如把已定保留原形的
    「岡崎」误改为「冈崎」）。
    """
    from . import glossary as gl

    existing = gl.load(cfg)
    user_rules = cfg.get("user_rules", default="") or ""
    sysp = prompts.build_term_review_system(cfg)
    base_extra = ""
    if existing:
        base_extra += "\n\n## 现行词汇表（已确认，勿冲突）\n" + "\n".join(
            prompts.term_lines([e for e in existing if e.get("status") == "confirmed"]))
    if user_rules:
        base_extra += f"\n\n## 用户附加规则\n{user_rules}"
    kept: list[dict] = []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        usr = prompts.build_term_review_user(batch) + base_extra
        try:
            resp = client.chat(sysp, usr, temperature=0.2, tag="term_review")
            data = llm_mod.parse_json_response(resp)
        except llm_mod.LLMError:
            continue
        for t in data.get("terms", []) or []:
            if t.get("src") and t.get("keep", True) is not False:
                kept.append({
                    "src": t["src"],
                    "dst": t.get("dst", ""),
                    "note": t.get("note", ""),
                    "category": t.get("category", "term"),
                    "reason": t.get("reason", ""),
                })
    return kept
