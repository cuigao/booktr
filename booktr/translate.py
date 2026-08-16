"""翻译引擎：逐段翻译、TM 查命、风格注入、拼接回写、检查点。"""
from __future__ import annotations

import json
import os
import re

from . import glossary as gl
from . import llm as llm_mod
from . import notes as notes_mod
from . import prompts
from . import segments as seg_mod
from . import styles as styles_mod
from . import tm as tm_mod
from . import util
from .config import Config

STATUS = {"pending": "pending", "done": "done", "review": "review", "skipped": "skipped"}


class State:
    """基于 state.json 的检查点。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.get("state", "path", default="work/state.json")
        self.data = util.read_json(self.path, {"pages": {}, "done_pages": []})
        self.data.setdefault("pages", {})
        self.data.setdefault("done_pages", [])
        self.data.setdefault("llm_stats", {})

    def page(self, rel: str) -> dict:
        p = self.data["pages"].setdefault(rel, {"status": "pending", "segments": {}})
        return p

    def save(self) -> None:
        util.write_json(self.path, self.data)

    def llm_stats(self) -> dict:
        return self.data.setdefault("llm_stats", {})


def _page_summary(cfg: Config, rel: str) -> str:
    p = os.path.join(cfg.get("summaries", "dir", default=""), rel.replace("/", "__") + ".json")
    data = util.read_json(p, {})
    return data.get("summary", "")


def process_inbox(cfg: Config, state: State) -> None:
    """处理用户注入目录 inbox/：其中的 .txt/.md 会转为笔记并入状态，随后清除。

    用户可在翻译过程中随时向 <数据根>/work/inbox/ 写入提示信息，下次检查点生效。
    """
    inbox = os.path.join(cfg.work_dir, "inbox")
    if not os.path.isdir(inbox):
        return
    for fn in sorted(os.listdir(inbox)):
        if not fn.endswith((".txt", ".md", ".json")):
            continue
        p = os.path.join(inbox, fn)
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
        except OSError:
            continue
        if not content:
            os.remove(p)
            continue
        if fn.endswith(".json"):
            import json as _json

            try:
                data = _json.loads(content)
                notes_mod.add(
                    cfg, data.get("page", ""), data.get("segment_id"),
                    data.get("quote", ""), data.get("summary", content[:1000]),
                    kind="用户注入", created_by="user",
                )
            except ValueError:
                notes_mod.add_user(cfg, content)
        else:
            notes_mod.add_user(cfg, content)
        os.remove(p)
    state.save()


def build_context(cfg: Config, site_map: dict, plan: dict, rel: str) -> tuple[str, str, str]:
    """构建 (页面上下文, 前文上下文, 用户规则)。"""
    page_meta = site_map.get("pages", {}).get(rel, {})
    page_ctx = ""
    if page_meta:
        bits = []
        if page_meta.get("title"):
            bits.append(f"标题：{page_meta['title']}")
        if page_meta.get("date"):
            bits.append(f"日期：{page_meta['date']}")
        if page_meta.get("section"):
            bits.append(f"栏目：{page_meta['section']}")
        if page_meta.get("kind"):
            bits.append(f"类型：{page_meta['kind']}")
        page_ctx = "；".join(bits)

    prior_ctx = ""
    window = cfg.get("planner", "context_window", default=5)
    if plan and window > 0:
        order = plan.get("order", [])
        if rel in order:
            idx = order.index(rel)
            prevs = order[max(0, idx - window) : idx]
            lines = []
            for pr in prevs:
                s = _page_summary(cfg, pr)
                if s:
                    lines.append(f"- {pr}: {s}")
            prior_ctx = "\n".join(lines)

    user_rules = cfg.get("user_rules", default="") or ""
    # 用户注入的笔记并入用户规则（优先）
    user_notes = [
        n for n in notes_mod.all_notes(cfg) if n.get("created_by") == "user"
    ]
    if user_notes:
        note_lines = [f"- {n.get('summary', '')}" for n in user_notes[-10:]]
        user_rules = (user_rules + "\n\n## 用户注入信息\n" + "\n".join(note_lines)).strip()
    return page_ctx, prior_ctx, user_rules


def build_translation_context(cfg: Config, rel: str, chk: str, page_ctx: str) -> str:
    """构建注入翻译 prompt 的相关笔记（重要信息/存疑/决策等历史笔记）。"""
    rel_notes = notes_mod.relevant(cfg, chk + " " + page_ctx, limit=6)
    if not rel_notes:
        return ""
    lines = []
    for n in rel_notes:
        q = n.get("quote", "").strip()
        s = n.get("summary", "").strip()
        if s and s != q:
            lines.append(f"- [{n.get('kind','')}] 引文「{q[:60]}」→ {s}")
        elif q:
            lines.append(f"- [{n.get('kind','')}] {q[:80]}")
    return "\n".join(lines)


def summarize_page(cfg: Config, client, rel: str) -> dict:
    """生成页面摘要供上下文包使用。"""
    raw = open(_src_path(cfg, rel), "rb").read()
    html, _ = util.decode_html(raw)
    segs = seg_mod.split_segments(html, cfg)
    text = "\n".join(s.text for s in segs if s.kind == "text")
    if not text.strip():
        return {"summary": "", "entities": [], "content_type": ""}
    sysp = prompts.build_summary_system(cfg)
    usr = prompts.build_summary_user(text[:6000])
    resp = client.chat(sysp, usr, temperature=0.3)
    try:
        data = llm_mod.parse_json_response(resp)
    except llm_mod.LLMError:
        data = {"summary": resp.strip()[:300], "entities": [], "content_type": "其他"}
    out_path = os.path.join(
        cfg.get("summaries", "dir", default=""), rel.replace("/", "__") + ".json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    util.write_json(out_path, data)
    return data


def translate_page(
    cfg: Config,
    client,
    rel: str,
    state: State,
    site_map: dict,
    plan: dict,
    review_queue: list[dict],
    interactive: bool = False,
) -> dict:
    """翻译单个页面。返回 {status, segments_total, review_count}。"""
    pstate = state.page(rel)
    if pstate.get("status") == STATUS["done"]:
        return {"status": "done", "skipped": True, "segments_total": len(pstate.get("segments", {}))}

    raw = open(_src_path(cfg, rel), "rb").read()
    html, _ = util.decode_html(raw)
    segs = seg_mod.segments_for_page(cfg, rel)

    page_ctx, prior_ctx, user_rules = build_context(cfg, site_map, plan, rel)
    guide = styles_mod.load_guide(cfg) if cfg.get("style", "rules_enabled", default=True) else ""
    focus = cfg.get("translators_notes", "focus", default="")
    tm_on = cfg.get("tm", "enabled", default=True)
    exemplar_on = cfg.get("style", "exemplar_enabled", default=True)

    result = {"status": "done", "skipped": False, "segments_total": len(segs), "review_count": 0}

    for seg in segs:
        sid = str(seg.id)
        done_seg = pstate.get("segments", {}).get(sid)
        if done_seg and done_seg.get("translation") is not None:
            seg.translation = done_seg["translation"]
            seg.confidence = done_seg.get("confidence")
            seg.needs_human = done_seg.get("needs_human", False)
            continue

        # 按 chunk_size 切分过长文本
        chunks = _chunk_text(seg.text, cfg.get("chunk_size", default=600))
        translated_chunks = []
        confidences = []
        needs_human = False
        collected_notes = []
        for chk in chunks:
            gl_items = gl.relevant(cfg, chk)
            if tm_on:
                tm_hits = tm_mod.lookup(cfg, chk)
            else:
                tm_hits = []
            if exemplar_on:
                ex_refs = styles_mod.retrieve_exemplars(cfg, chk)
            else:
                ex_refs = []
            notes_ctx = build_translation_context(cfg, rel, chk, page_ctx)
            chk_prior = prior_ctx
            if notes_ctx:
                chk_prior = (chk_prior + "\n\n## 相关笔记\n" + notes_ctx).strip()
            sysp = prompts.build_translate_system(
                cfg, gl_items, guide, user_rules, focus
            )
            usr = prompts.build_translate_user(
                cfg, chk, page_ctx, chk_prior, ex_refs, tm_hits
            )
            resp = client.chat(sysp, usr, temperature=cfg.get("llm", "temperature", default=0.3))
            client.log_call(f"translate_{rel.replace('/','_')}", sysp, usr, resp)
            try:
                data = llm_mod.parse_json_response(resp)
            except llm_mod.LLMError:
                data = {"translation": resp.strip(), "confidence": 0.3,
                        "needs_human": True, "glossary_conflicts": [], "notes": []}
            t = (data.get("translation") or "").strip()
            translated_chunks.append(t)
            confidences.append(float(data.get("confidence", 0.7)))
            if data.get("needs_human"):
                needs_human = True
            for note in data.get("notes", []) or []:
                if isinstance(note, str) and note.strip():
                    collected_notes.append(note)
            for c in data.get("glossary_conflicts", []) or []:
                if isinstance(c, str) and c.strip():
                    review_queue.append(
                        _make_review(cfg, rel, sid, seg.text, "glossary_conflict", c)
                    )
            if data.get("needs_human"):
                review_queue.append(
                    _make_review(cfg, rel, sid, seg.text, "low_confidence", t)
                )
            # 写入翻译记忆
            if tm_on and t and t != chk:
                tm_mod.add(cfg, chk, t, rel, seg.id)

        translation = "".join(translated_chunks)
        confidence = min(confidences) if confidences else None
        seg.translation = translation
        seg.confidence = confidence
        seg.needs_human = needs_human
        for note in collected_notes:
            notes_mod.add(cfg, rel, seg.id, seg.text[:500], note, kind="翻译说明", created_by="llm")

        pstate.setdefault("segments", {})[sid] = {
            "translation": translation,
            "confidence": confidence,
            "needs_human": needs_human,
        }
        if needs_human:
            result["review_count"] += 1
            pstate["status"] = STATUS["review"]

    # 保存段索引
    _save_segment_index(cfg, rel, segs)

    # 拼接回写
    out_html = seg_mod.reassemble(html, segs)
    seg_mod.write_page_output(cfg, rel, out_html)

    if pstate.get("status") != STATUS["review"]:
        pstate["status"] = STATUS["done"]
        result["status"] = "done"
    else:
        result["status"] = "review"
    if rel not in state.data["done_pages"] and result["status"] == "done":
        state.data["done_pages"].append(rel)
    state.save()
    return result


def _make_review(cfg, rel, sid, src_text, reason, detail) -> dict:
    return {
        "page": rel,
        "segment_id": sid,
        "src": src_text,
        "reason": reason,
        "detail": detail,
        "status": "open",
    }


def _chunk_text(text: str, size: int) -> list[str]:
    """将过长文本按自然边界切分为多个 chunk（保留占位符完整）。"""
    if len(text) <= size:
        return [text]
    chunks = []
    cur = ""
    # 按换行切分，尽量在句末（。！？）断开
    units = re.split(r"(\n|(?<=[。！？!?]))", text)
    for u in units:
        if not u:
            continue
        if len(cur) + len(u) > size and cur:
            chunks.append(cur)
            cur = u
        else:
            cur += u
    if cur:
        chunks.append(cur)
    return chunks


def _save_segment_index(cfg: Config, rel: str, segs) -> None:
    path = os.path.join(
        cfg.get("segments_dir", default=""), rel.replace("/", "__") + ".json"
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = []
    for s in segs:
        d = s.to_dict()
        d["page"] = rel
        data.append(d)
    util.write_json(path, data)


def _src_path(cfg: Config, rel: str) -> str:
    return os.path.join(cfg.source_dir, rel.replace("/", os.sep))
