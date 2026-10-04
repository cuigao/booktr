"""监督式自动 QA 的判官（supervisor）。

对 QA 队列中某页的问题逐条裁定【采纳 / 拒绝 / 跳过】：以**重语境**（全站摘要 +
当前页完整原文/译文 + 词汇表 + 风格规则 + 用户规则）为 system，多轮对话逐条询问，
历史累积的历次裁决为同页一致性提供锚点。

设计依据见工作区报告 `instance/report/qa_auto_probe_report.md`：
- 多轮逐条（第 1 条 user 列出全部并要求"只裁决第 1 条"，其后逐条）避免一次性多判决漂移；
- 判官以**当前译文**为依据，故评估应以"问题是否仍复现"为准；
- 若问题涉及 HTML/标签，判官须核对源文本本身（见 prompts.build_supervisor_system）。
"""
from __future__ import annotations

import os
import time

from . import glossary as gl
from . import llm as llm_mod
from . import prompts
from . import segments as seg_mod
from . import styles as styles_mod
from . import translate as tr
from . import util
from .config import Config

VERDICT_ADOPT = "adopt"
VERDICT_REJECT = "reject"
VERDICT_SKIP = "skip"


def build_context(cfg: Config, rel: str) -> dict:
    """组装判官语境（重语境）。

    返回 {page_ctx, summaries(dict), glossary_lines(list), style_guide,
    user_rules, page_src, page_dst}。
    """
    sm = util.read_json(os.path.join(cfg.work_dir, "site_map.json"), {})
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    page_ctx, _, user_rules = tr.build_context(cfg, sm, plan, rel)

    segs = seg_mod.segments_for_page(cfg, rel)
    text_segs = [s for s in segs if s.kind == "text" and s.translation]
    page_src = "\n".join(s.text for s in text_segs)
    page_dst = "\n".join(s.translation or "" for s in text_segs)

    # 全站摘要（可关闭 / 设总量上限）
    summaries: dict[str, str] = {}
    if cfg.get("qa", "supervisor", "include_all_summaries", default=True):
        cap = int(cfg.get("qa", "supervisor", "all_summaries_max_chars",
                          default=65536) or 0)
        sdir = cfg.get("summaries", "dir", default="")
        if sdir and os.path.isdir(sdir):
            total = 0
            for fn in sorted(os.listdir(sdir)):
                if not fn.endswith(".json"):
                    continue
                data = util.read_json(os.path.join(sdir, fn), {})
                s = data.get("summary", "")
                if not s:
                    continue
                if cap and total + len(s) > cap and summaries:
                    break
                summaries[fn[:-5].replace("__", "/")] = s
                total += len(s)

    gl_lines = prompts.term_lines(gl.all_confirmed(cfg))
    guide = styles_mod.load_guide(cfg) \
        if cfg.get("style", "rules_enabled", default=True) else ""
    history_block = ""
    if cfg.get("qa", "inject_history", default=False):
        from . import qa as qa_mod
        history_block = prompts.build_qa_history_block(qa_mod.build_history(cfg, rel))
    return {
        "page_ctx": page_ctx,
        "summaries": summaries,
        "glossary_lines": gl_lines,
        "style_guide": guide,
        "user_rules": user_rules,
        "history_block": history_block,
        "page_src": page_src,
        "page_dst": page_dst,
    }


def _extract_verdict_dict(resp: str) -> dict | None:
    """从判官输出提取 verdict JSON，容忍值字符串内未转义的引号。

    parse_json_response 的机械修复以 `translation` key 为门控（翻译管线专用），
    判官响应含 `verdict` 而无 `translation`，故此处补充同款修复兜底。
    """
    # 1) 标准解析
    try:
        d = llm_mod.parse_json_response(resp)
        if "verdict" in d:
            return d
    except llm_mod.LLMError:
        pass
    # 2) 平衡提取 + 值字符串转义修复（覆盖 suggestion 内裸引号）
    text = (resp or "").strip()
    block = llm_mod._extract_balanced_json(text) or text
    for cand in (block, llm_mod.repair_value_strings(block)):
        if not cand:
            continue
        try:
            d = __import__("json").loads(cand)
        except ValueError:
            continue
        if isinstance(d, dict) and "verdict" in d:
            return d
    return None


def parse_verdict(resp: str) -> dict:
    """解析判官输出为 {verdict, reason, suggestion}。非法则 skip。"""
    d = _extract_verdict_dict(resp)
    if d is None:
        return {"verdict": VERDICT_SKIP, "reason": "解析失败",
                "suggestion": "", "_raw": (resp or "")[:300]}
    v = (d.get("verdict") or "").strip().lower()
    if v not in (VERDICT_ADOPT, VERDICT_REJECT, VERDICT_SKIP):
        v = VERDICT_SKIP
    out = {"verdict": v, "reason": d.get("reason", ""),
           "suggestion": d.get("suggestion", "")}
    if "index" in d:
        try:
            out["index"] = int(d["index"])
        except (TypeError, ValueError):
            pass
    return out


def _call(client, messages, tag, on_delta=None):
    return client.chat_multi(messages, temperature=0.1, tag=tag, on_delta=on_delta)


def adjudicate_page(cfg: Config, client, rel: str,
                    items: list[dict]) -> tuple[list[dict], list[dict]]:
    """对一页的 QA 条目逐条裁定。返回 (verdicts, turns)。

    - 多轮：system 携带重语境；第 1 条 user 列出全部并要求只裁决第 1 条；
      其后逐条询问。历史累积的历次裁决为同页一致性提供锚点。
    - **提问现场拼装**：每条的请求 = `已提交历史 + 当前条目 usr`，其中 seed
      （"只裁决第 1 条"）仅用于开篇，**不作为重试基底**。单条失败时不提交历史、
      直接进入下一条，避免"seed(第 1 条) + 第 i 条"并存导致答错条目。
    - **索引回显校验**：要求判官返回 `index`；回显不等于当前序号则记 skip。
    - 逐条容错：单条 LLM 失败记 skip 并继续（网络抖动场景不中断整页）。
    """
    if not items:
        return [], []
    ctx = build_context(cfg, rel)
    system = prompts.build_supervisor_system(cfg, ctx)
    tag = f"judge_{rel.replace('/', '_')}"
    n = len(items)
    # 已提交的多轮历史（仅成功条目追加 assistant）
    history: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompts.build_supervisor_seed_user(items)},
    ]
    verdicts: list[dict] = []
    turns: list[dict] = []

    def _record(it, v, extra=None):
        rec = {**v, "item_id": it.get("id"), "segments": it.get("segments"),
               "severity": it.get("severity")}
        if extra:
            rec.update(extra)
        verdicts.append(rec)

    for i, it in enumerate(items, 1):
        t0 = time.monotonic()
        first = {"t": None}

        def on_delta(kind, text):
            if first["t"] is None:
                first["t"] = round(time.monotonic() - t0, 1)

        ask = history + [{"role": "user",
                          "content": prompts.build_supervisor_item_user(i, n, it)}]
        try:
            resp = _call(client, ask, tag, on_delta=on_delta)
        except llm_mod.LLMError as e:
            v = {"verdict": VERDICT_SKIP,
                 "reason": f"网络错误: {str(e)[:120]}", "suggestion": ""}
            _record(it, v)
            turns.append({"turn": i, "error": str(e)[:200]})
            # 不提交历史、不注入 seed；下一条 ask 自然只指向该条
            continue
        v = parse_verdict(resp)
        # 索引回显校验：不符则判 skip（防答错条目）
        idx = v.get("index")
        if idx is not None and idx != i:
            v = {"verdict": VERDICT_SKIP,
                 "reason": f"索引回显不符（返回 {idx}，应为 {i}）", "suggestion": ""}
        # 相关性守门：响应明显指向同页其它条目 → 判 skip（留人工），不覆盖建议
        elif not relevance_ok((v.get("reason", "") + " " + v.get("suggestion", "")),
                              i - 1, items):
            v = {"verdict": VERDICT_SKIP,
                 "reason": "响应与本条相关性不足（疑似漂移到其它条目）",
                 "suggestion": "", "guard_failed": True}
        verdicts.append({**v, "item_id": it.get("id"),
                         "segments": it.get("segments"),
                         "severity": it.get("severity")})
        turns.append({"turn": i, "assistant": resp, "parsed": v,
                      "first_token_s": first["t"],
                      "duration_s": round(time.monotonic() - t0, 1)})
        # 仅把成功的（未被判 skip）回答作为历史锚点；但仍提交 assistant 以保持对话连续
        history.append({"role": "assistant", "content": resp})
    return verdicts, turns


def _lcs(a: str, b: str) -> int:
    a, b = a or "", b or ""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _profile(it: dict) -> str:
    return " ".join([it.get("reason", "") or "", it.get("suggestion", "") or "",
                     it.get("src_quote", "") or "", it.get("dst_quote", "") or ""])


def relevance_ok(resp: str, idx: int, items: list[dict], margin: int = 6) -> bool:
    """判官响应是否确实针对第 ``idx`` 条（0 起），而非漂移到同页其它条目。

    以"响应文本与该条 profile（reason+suggestion+src/dst 引文）的 LCS 相似度"
    为准：若某**其它**条目的相似度明显高于被问条目（差距 ≥ margin），判为漂移。
    ``idx`` 越界或无法判断时返回 True（不阻断）。
    """
    resp = (resp or "").strip()
    if not resp or not (0 <= idx < len(items)):
        return True
    scores = [_lcs(resp, _profile(it)) for it in items]
    asked = scores[idx]
    other = max([s for k, s in enumerate(scores) if k != idx], default=0)
    return not (asked < other and other - asked >= margin)


def apply_verdicts(items: list[dict], verdicts: list[dict],
                   adopted_status: str = "adopted",
                   rejected_status: str = "rejected") -> dict:
    """把裁决写回条目（原地修改），返回统计。

    - adopt：默认沿用 QA 原 suggestion；若判官给出不同且非空的 suggestion，则
      归档原值到 llm_suggestion/llm_reason 并采用判官版本，标记 source="supervisor"。
    - reject：置 rejected。
    - skip：保持/置回 open（不动建议）。
    """
    stats = {"adopt": 0, "reject": 0, "skip": 0}
    for it, v in zip(items, verdicts):
        verdict = v.get("verdict")
        if verdict == VERDICT_ADOPT:
            sug = (v.get("suggestion") or "").strip()
            if sug and sug != (it.get("suggestion") or "").strip():
                it.setdefault("llm_suggestion", it.get("suggestion", ""))
                it.setdefault("llm_reason", it.get("reason", ""))
                it["suggestion"] = sug
                if v.get("reason"):
                    it["reason"] = v["reason"]
                it["source"] = "supervisor"
            it["status"] = adopted_status
            stats["adopt"] += 1
        elif verdict == VERDICT_REJECT:
            it["status"] = rejected_status
            stats["reject"] += 1
        else:
            # skip：保持 open 留人工处理（不覆盖建议）
            stats["skip"] += 1
    return stats
