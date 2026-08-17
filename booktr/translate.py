"""翻译引擎：逐段翻译、TM 查命、风格注入、拼接回写、检查点。"""
from __future__ import annotations

import json
import os
import re

from . import glossary as gl
from . import llm as llm_mod
from . import notes as notes_mod
from . import phrases as phrases_mod
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
    resp = client.chat(sysp, usr, temperature=0.3, tag=f"summary_{rel.replace('/','_')}")
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


def _cleanup_fallback(resp: str) -> str:
    """兜底清理：去除 prompt 标记与 JSON 残渣，尽力还原纯译文文本。"""
    t = resp.strip()
    t = t.replace("|TEXT|", "").replace("|DST|", "")
    # 去除 LLM 可能输出的多余 prompt 片段（仅去除标记行本身）
    for marker in ("## 待翻译文本", "## 翻译记忆命中", "## 风格参照样例",
                   "## 当前页面上下文", "## 前文上下文", "## 词汇表",
                   "原文：", "参考译文："):
        idx = t.find(marker)
        if idx >= 0:
            # 找到行尾，去除整行
            eol = t.find("\n", idx)
            if eol >= 0:
                t = t[:idx] + t[eol + 1:]
            else:
                t = t[:idx]
    # 去除行首的 {"translation":" 或 { "translation" : " 前缀
    t = re.sub(r'^\s*\{\s*"translation"\s*:\s*"?', "", t)
    # 去除行尾的 " 或 ",... 或 "} 残渣
    t = re.sub(r'"\s*[,}]?\s*$', "", t)
    t = t.strip()
    # 若还残留其他 JSON 键开头（如 "confidence": ...），截断
    m = re.search(r'"\s*,\s*"', t)
    if m:
        t = t[: m.start()]
    return t


def _check_placeholders(src_text: str, translation: str) -> list[str]:
    """检查译文是否保留了原文的所有 [[Px]] 占位符，返回缺失的列表。"""
    src_ph = set(re.findall(r'\[\[P\d+\]\]', src_text))
    if not src_ph:
        return []
    dst_ph = set(re.findall(r'\[\[P\d+\]\]', translation))
    return sorted(src_ph - dst_ph)


def _translate_chunk_with_repair(
    cfg: Config, client, sysp: str, usr: str, rel: str, sid: str, chk: str,
) -> dict:
    """翻译单个 chunk，带解析失败自愈循环与兜底。

    返回 {translation, confidence, needs_human, untrusted, glossary_conflicts, notes}。
    """
    max_repair = cfg.get("llm", "max_repair", default=3)
    resp = client.chat(sysp, usr,
                       temperature=cfg.get("llm", "temperature", default=0.3),
                       tag=f"translate_{rel.replace('/','_')}")
    for attempt in range(max_repair + 1):
        try:
            data = llm_mod.parse_json_response(resp)
            return {**data, "untrusted": False}
        except llm_mod.LLMError as e:
            if attempt >= max_repair:
                break
            # 自愈：把具体错误与上次输出片段提供给 LLM，要求重新输出
            repair_hint = (
                f"\n\n上次输出无法解析为合法 JSON。错误：{e}\n"
                f"上次输出开头：{resp[:300]}\n"
                "请重新输出严格合法的 JSON（不要任何多余文字、前后缀或换行包裹）。"
            )
            resp = client.chat(sysp, usr + repair_hint,
                               temperature=cfg.get("llm", "temperature", default=0.3),
                               tag=f"repair_{rel.replace('/','_')}_seg{sid}")
    # 兜底：修复耗尽，清理后作为译文，标记不可信
    cleaned = _cleanup_fallback(resp)
    notes_mod.add(cfg, rel, int(sid), chk[:500],
                  f"JSON 解析失败 {max_repair} 次后兜底清理", kind="存疑", created_by="llm")
    return {"translation": cleaned, "confidence": 0.1, "needs_human": True,
            "untrusted": True, "glossary_conflicts": [], "notes": []}


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
        untrusted = False
        collected_notes = []
        for chk in chunks:
            # 短语精确记忆：去占位符后精确命中则直接采用，跳过 LLM
            chk_plain = re.sub(r"\[\[P\d+\]\]", "", chk).strip()
            ph_hit = phrases_mod.lookup(cfg, chk_plain)
            if ph_hit:
                translated_chunks.append(ph_hit)
                confidences.append(1.0)
                continue
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
            data = _translate_chunk_with_repair(cfg, client, sysp, usr, rel, sid, chk)
            t = (data.get("translation") or "").strip()
            # 占位符完整性校验：若译文丢失了 [[Px]]，触发一次 repair
            missing = _check_placeholders(chk, t)
            if missing:
                repair_usr = (
                    prompts.build_translate_user(
                        cfg, chk, page_ctx, chk_prior, ex_refs, tm_hits
                    )
                    + f"\n\n⚠️ 你丢失了占位符 {', '.join(missing)}，"
                    "请重新翻译，必须在译文中保留所有 [[Px]] 占位符。"
                )
                data = _translate_chunk_with_repair(cfg, client, sysp, repair_usr, rel, sid, chk)
                t = (data.get("translation") or "").strip()
                still_missing = _check_placeholders(chk, t)
                if still_missing:
                    untrusted = True
                    notes_mod.add(cfg, rel, int(sid), chk[:500],
                                  f"占位符丢失: {', '.join(still_missing)}", kind="存疑", created_by="llm")
            translated_chunks.append(t)
            confidences.append(float(data.get("confidence", 0.7)))
            if data.get("needs_human") or data.get("untrusted"):
                needs_human = True
            if data.get("untrusted"):
                untrusted = True
            for note in data.get("notes", []) or []:
                if isinstance(note, str) and note.strip():
                    collected_notes.append(note)
            for c in data.get("glossary_conflicts", []) or []:
                if isinstance(c, str) and c.strip():
                    review_queue.append(
                        _make_review(cfg, rel, sid, seg.text, "glossary_conflict", c)
                    )
            if data.get("needs_human") or data.get("untrusted"):
                review_queue.append(
                    _make_review(cfg, rel, sid, seg.text, "low_confidence", t)
                )
            # 写入翻译记忆
            if tm_on and t and t != chk:
                tm_mod.add(cfg, chk, t, rel, seg.id)
            # 短语记忆：成功翻译的短短语记录供全文复用（排除含 prompt 标记的异常译文）
            if not data.get("untrusted") and t and chk_plain:
                t_plain = re.sub(r"\[\[P\d+\]\]", "", t).strip()
                if t_plain and "|TEXT|" not in t_plain and "|DST|" not in t_plain:
                    phrases_mod.add(cfg, chk_plain, t_plain)

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
            "untrusted": untrusted,
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
