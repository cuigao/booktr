"""翻译引擎：逐段翻译、TM 查命、风格注入、拼接回写、检查点。"""
from __future__ import annotations

import json
import os
import re
import time

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
                s = _get_page_summary(cfg, pr)
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
        cfg.get("summaries", "dir", default="work/summaries"), rel.replace("/", "__") + ".json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    util.write_json(out_path, data)
    return data


def _cleanup_fallback(resp: str) -> str:
    """兜底清理：去除 prompt 标记与 JSON 残渣，尽力还原纯译文文本。"""
    t = resp.strip()
    t = t.replace("|TEXT|", "").replace("|DST|", "")
    # 去除 LLM 可能输出的多余 prompt 片段（仅去除标记行本身）
    for marker in ("## 待翻译文本", "### 待翻译文本",
                   "## 翻译记忆命中", "### 翻译记忆命中",
                   "## 风格参照样例", "### 风格参照样例",
                   "## 当前页面上下文", "### 当前页面上下文",
                   "## 前文上下文", "### 前文上下文",
                   "## 词汇表", "### 词汇表",
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
    """检查译文占位符与原文的一致性，返回异常列表。

    检测两类异常：
    - 缺失：原文有但译文缺的 [[Px]]
    - 多余：译文凭空多出的 [[Px]]（原文无）
    """
    src_ph = set(re.findall(r'\[\[P\d+\]\]', src_text))
    dst_ph = set(re.findall(r'\[\[P\d+\]\]', translation))
    missing = src_ph - dst_ph
    extra = dst_ph - src_ph
    return sorted(missing | extra)


def _restore_placeholders_from_src(src_text: str, plain_translation: str) -> str:
    """根据原始文本的占位符结构，将占位符恢复到纯文本译文中。

    策略：按原始文本中占位符的相对位置（开头/结尾），在译文对应位置插入。
    """
    parts = re.split(r'(\[\[P\d+\]\])', src_text)
    if len(parts) <= 1:
        return plain_translation

    # 分离占位符和文本
    placeholders = [p for p in parts if re.match(r'\[\[P\d+\]\]', p)]
    if not placeholders:
        return plain_translation

    # 找到第一个非空文本的位置，区分 leading/trailing 占位符
    leading = []
    trailing = []
    found_text = False
    for p in parts:
        if re.match(r'\[\[P\d+\]\]', p):
            if not found_text:
                leading.append(p)
            else:
                trailing.append(p)
        elif p.strip():
            found_text = True

    # 在译文首尾插入占位符
    result = plain_translation
    for p in leading:
        result = p + result
    for p in trailing:
        result = result + p
    return result


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
                f"上次输出开头：{(resp or '')[:300]}\n"
                "请重新输出严格合法的 JSON（不要任何多余文字、前后缀或换行包裹）。"
            )
            resp = client.chat(sysp, usr + repair_hint,
                               temperature=cfg.get("llm", "temperature", default=0.3),
                               tag=f"repair_{rel.replace('/','_')}_seg{sid}")
    # 兜底：修复耗尽，清理后作为译文，标记不可信
    cleaned = _cleanup_fallback(resp or "")
    notes_mod.add(cfg, rel, int(sid), chk[:500],
                  f"JSON 解析失败 {max_repair} 次后兜底清理", kind="存疑", created_by="llm")
    return {"translation": cleaned, "confidence": 0.1, "needs_human": True,
            "untrusted": True, "glossary_conflicts": [], "notes": []}


# ── 多轮对话翻译 ────────────────────────────────────────────────────────


def _summarize_conversation(
    cfg: Config, client, translations: list[str], rel: str,
) -> str:
    """对已翻译的多个段落生成摘要，用于摘要接力。"""
    sysp = prompts.build_conversation_summary(cfg)
    usr = prompts.build_conversation_summary_user(translations)
    try:
        resp = client.chat(sysp, usr,
                           temperature=cfg.get("llm", "temperature", default=0.3),
                           tag=f"summarize_conv_{rel.replace('/','_')}")
        return resp.strip()
    except llm_mod.LLMError:
        return ""


def _translate_chunk_with_repair_multi(
    cfg: Config, client, messages: list[dict], rel: str, sid: str, chk: str,
    task_id: str = "", context_id: str = "",
) -> dict:
    """多轮对话模式翻译单个 chunk，带解析失败自愈与占位符校验。

    messages 是可变列表，会就地追加 assistant/repair 消息。
    """
    max_repair = cfg.get("llm", "max_repair", default=3)
    temperature = cfg.get("llm", "temperature", default=0.3)
    tag = f"translate_{rel.replace('/', '_')}"

    resp = ""
    for attempt in range(max_repair + 1):
        try:
            if not resp:
                resp = client.chat_multi(messages, temperature=temperature, tag=tag,
                                         task_id=task_id, context_id=context_id)
            data = llm_mod.parse_json_response(resp)
            messages.append({"role": "assistant", "content": resp or ""})
            return {**data, "untrusted": False}
        except llm_mod.LLMError as e:
            if attempt >= max_repair:
                break
            repair_hint = (
                f"\n\n上次输出无法解析为合法 JSON。错误：{e}\n"
                f"上次输出开头：{(resp or '')[:300]}\n"
                "请重新输出严格合法的 JSON（不要任何多余文字、前后缀或换行包裹）。"
            )
            messages.append({"role": "user", "content": repair_hint})
            resp = client.chat_multi(messages, temperature=temperature,
                                     tag=f"repair_{rel.replace('/','_')}_seg{sid}",
                                     task_id=task_id, context_id=context_id)

    # 兜底
    cleaned = _cleanup_fallback(resp or "")
    messages.append({"role": "assistant", "content": resp or ""})
    notes_mod.add(cfg, rel, int(sid), chk[:500],
                  f"JSON 解析失败 {max_repair} 次后兜底清理", kind="存疑", created_by="llm")
    return {"translation": cleaned, "confidence": 0.1, "needs_human": True,
            "untrusted": True, "glossary_conflicts": [], "notes": []}


def _get_page_summary(cfg: Config, rel: str) -> str:
    """获取页面摘要。"""
    summary_path = os.path.join(cfg.get("summaries", "dir", default="work/summaries"),
                                rel.replace("/", "__") + ".json")
    if not os.path.exists(summary_path):
        return ""
    data = util.read_json(summary_path, {})
    return data.get("summary", "")


def _get_adjacent_translations(cfg: Config, seg, segs: list, state, rel: str,
                               max_chars: int) -> tuple[str, str]:
    """获取待翻译段落的前后文已翻译内容。"""
    idx = segs.index(seg)
    pstate = state.page(rel)

    # 前文
    before = []
    chars = 0
    for i in range(idx - 1, -1, -1):
        prev = segs[i]
        tr = pstate.get("segments", {}).get(str(prev.id), {}).get("translation", "")
        if tr:
            if chars + len(tr) > max_chars:
                before.insert(0, tr[-(max_chars - chars):])
                break
            before.insert(0, tr)
            chars += len(tr)

    # 后文
    after = []
    chars = 0
    for i in range(idx + 1, len(segs)):
        nxt = segs[i]
        tr = pstate.get("segments", {}).get(str(nxt.id), {}).get("translation", "")
        if tr:
            if chars + len(tr) > max_chars:
                after.append(tr[:max_chars - chars])
                break
            after.append(tr)
            chars += len(tr)

    return "\n".join(before), "\n".join(after)


def _build_retranslate_context(cfg: Config, rel: str, seg, segs: list,
                               state, site_map: dict, plan: dict) -> dict:
    """构建重新翻译的上下文。"""
    page_ctx, _, _ = build_context(cfg, site_map, plan, rel)

    summary = ""
    if cfg.get("llm", "retranslate_use_summary", default=True):
        summary = _get_page_summary(cfg, rel)

    max_chars = cfg.get("llm", "retranslate_context_chars", default=1000)
    before_text, after_text = _get_adjacent_translations(
        cfg, seg, segs, state, rel, max_chars // 2
    )

    return {
        "page_ctx": page_ctx,
        "summary": summary,
        "context_before": before_text,
        "context_after": after_text,
    }


def translate_page(
    cfg: Config,
    client,
    rel: str,
    state: State,
    site_map: dict,
    plan: dict,
    review_queue: list[dict],
    interactive: bool = False,
    progress=None,
) -> dict:
    """翻译单个页面（多轮对话模式）。返回 {status, segments_total, review_count}。"""
    _ev = lambda event, data: progress(event, data) if progress else None

    pstate = state.page(rel)
    if pstate.get("status") == STATUS["done"]:
        return {"status": "done", "skipped": True, "segments_total": len(pstate.get("segments", {}))}

    raw = open(_src_path(cfg, rel), "rb").read()
    html, _ = util.decode_html(raw)
    segs = seg_mod.segments_for_page(cfg, rel)
    text_segs = [s for s in segs if s.kind == "text"]
    total_chars = sum(len(s.text) for s in text_segs)
    _ev("plan", {"segments": len(text_segs), "total_chars": total_chars, "rel": rel})

    page_ctx, prior_ctx, user_rules = build_context(cfg, site_map, plan, rel)
    guide = styles_mod.load_guide(cfg) if cfg.get("style", "rules_enabled", default=True) else ""
    focus = cfg.get("translators_notes", "focus", default="")
    tm_on = cfg.get("tm", "enabled", default=True)
    exemplar_on = cfg.get("style", "exemplar_enabled", default=True)
    max_history = cfg.get("llm", "max_history_segments", default=50)
    summary_on = cfg.get("llm", "summary_enabled", default=True)

    # 构建 system prompt（整页共享，仅含全局规则；词条推荐译法注入 user prompt）
    sysp = prompts.build_translate_system(cfg, [], guide, user_rules, focus)

    result = {"status": "done", "skipped": False, "segments_total": len(segs), "review_count": 0}

    # Session ID: task_id = 一次完整页面翻译，context_id = 一轮多轮对话
    _epoch_ms = lambda: int(time.time() * 1000)
    page_key = rel.replace("/", "_").replace(".html", "")
    task_id = f"tsk_{_epoch_ms()}_{page_key}"
    context_seq = 0
    context_id = f"ctx_{_epoch_ms()}_{page_key}_{context_seq}"

    # 多轮对话状态
    conversation: list[dict] = [{"role": "system", "content": sysp}]
    conversation_summary = ""  # 摘要接力的摘要
    history_count = 0  # 当前对话中的翻译轮数
    pending_translations: list[str] = []  # 用于摘要的已译段落
    flagged = []  # 需要页面级重翻译的 chunk

    for seg_idx, seg in enumerate(segs, 1):
        sid = str(seg.id)
        done_seg = pstate.get("segments", {}).get(sid)

        # 检测是否需要重新翻译：
        # - 全新段（segments 无该 sid，done_seg is None）→ 正常首次翻译
        # - 被 review 删除的段（segments 存在但 translation is None）→ 重翻译
        needs_retranslate = (
            done_seg is not None and done_seg.get("translation") is None
        )

        if done_seg and done_seg.get("translation") is not None and not needs_retranslate:
            seg.translation = done_seg["translation"]
            seg.confidence = done_seg.get("confidence")
            seg.needs_human = done_seg.get("needs_human", False)
            continue

        chunks = _chunk_text(seg.text, cfg.get("chunk_size", default=600))
        _ev("segment_start", {"sid": sid, "seg_idx": seg_idx, "total": len(segs),
                              "text_preview": seg.text[:60], "chunks": len(chunks)})
        translated_chunks = []
        confidences = []
        needs_human = False
        untrusted = False
        collected_notes = []
        skipped_phrases = []  # 短语记忆跳过的翻译，注入到下一条 user message

        # 重新翻译模式：构建上下文窗口 + 新对话（仅 review 删除的段）
        retranslate_context = None
        if needs_retranslate:
            retranslate_context = _build_retranslate_context(
                cfg, rel, seg, segs, state, site_map, plan
            )
            # 创建新对话（fresh start），生成新 context_id
            context_seq += 1
            context_id = f"ctx_{_epoch_ms()}_{page_key}_{context_seq}"
            conversation = [{"role": "system", "content": sysp}]
            history_count = 0
            pending_translations = []
            _ev("retranslate", {"sid": sid, "has_context": bool(retranslate_context)})

        for chk in chunks:
            chk_plain = re.sub(r"\[\[P\d+\]\]", "", chk).strip()

            # 1. 词汇表 read_only 条目 → 机械替换
            gl_hit = gl.lookup_read_only(cfg, chk_plain)
            if gl_hit:
                restored = _restore_placeholders_from_src(chk, gl_hit)
                translated_chunks.append(restored)
                confidences.append(1.0)
                pending_translations.append(restored)
                skipped_phrases.append(f"{chk_plain} → {gl_hit} [词汇表]")
                history_count += 1
                continue

            # 2. 短语记忆 → 机械替换
            ph_hit = phrases_mod.lookup(cfg, chk_plain)
            if ph_hit:
                restored = _restore_placeholders_from_src(chk, ph_hit)
                translated_chunks.append(restored)
                confidences.append(1.0)
                pending_translations.append(restored)
                skipped_phrases.append(f"{chk_plain} → {ph_hit} [短语记忆]")
                history_count += 1
                continue

            # 3. 检索相关条目 + 构建推荐译法（注入 user prompt，而非 system）
            gl_items = gl.relevant(cfg, chk)  # 词汇表相关（含 read_only）
            ph_items = phrases_mod.relevant(cfg, chk)  # 短语记忆相关
            all_injections = gl_items + ph_items
            term_hints = prompts.format_term_hints(all_injections) if all_injections else ""

            # 重新翻译模式标记（system prompt 仅追加重翻译规则，不含词条）
            is_retranslation = retranslate_context is not None

            tm_hits = tm_mod.lookup(cfg, chk) if tm_on else []
            ex_refs = styles_mod.retrieve_exemplars(cfg, chk) if exemplar_on else []
            notes_ctx = build_translation_context(cfg, rel, chk, page_ctx)
            chk_prior = prior_ctx
            if notes_ctx:
                chk_prior = (chk_prior + "\n\n## 相关笔记\n" + notes_ctx).strip()

            # 首条消息或摘要接力后：携带 page_ctx
            is_first = (len(conversation) == 1) or (history_count == 0)
            if is_first:
                if retranslate_context:
                    # 重新翻译模式：使用带上下文窗口的消息
                    usr = prompts.build_retranslate_user(cfg, chk, retranslate_context)
                else:
                    usr = prompts.build_translate_user_first(
                        cfg, chk, page_ctx, chk_prior, ex_refs, tm_hits,
                        summary=conversation_summary,
                        term_hints=term_hints,
                    )
            else:
                usr = prompts.build_translate_user_subsequent(cfg, chk, term_hints=term_hints)

            # 注入短语记忆跳过的翻译到待翻译文本之前
            if skipped_phrases:
                inject = "\n\n".join(skipped_phrases)
                usr = f"## 前文自动处理的短语（供参考，保持一致）\n{inject}\n\n{usr}"
                skipped_phrases = []

            conversation.append({"role": "user", "content": usr})
            data = _translate_chunk_with_repair_multi(cfg, client, conversation, rel, sid, chk,
                                                      task_id=task_id, context_id=context_id)
            t = (data.get("translation") or "").strip()

            # 占位符完整性校验
            missing = _check_placeholders(chk, t)
            if missing:
                _ev("repair", {"sid": sid, "missing": missing})
                repair_usr = (
                    f"⚠️ 你丢失了占位符 {', '.join(missing)}，"
                    "请重新翻译，必须在译文中保留所有 [[Px]] 占位符。"
                )
                conversation.append({"role": "user", "content": repair_usr})
                data = _translate_chunk_with_repair_multi(cfg, client, conversation, rel, sid, chk,
                                                          task_id=task_id, context_id=context_id)
                t = (data.get("translation") or "").strip()
                still_missing = _check_placeholders(chk, t)
                if still_missing:
                    untrusted = True
                    notes_mod.add(cfg, rel, int(sid), chk[:500],
                                  f"占位符丢失: {', '.join(still_missing)}", kind="存疑", created_by="llm")

            # 收集需要 review 的 chunk（页面级统一重翻译）
            if data.get("needs_human") or untrusted:
                flagged.append({
                    "seg": seg, "sid": sid, "chk": chk,
                    "chunk_idx": len(translated_chunks),
                    "translated_chunks": translated_chunks,
                    "confidences": confidences,
                    "all_injections": all_injections,
                })

            translated_chunks.append(t)
            confidences.append(float(data.get("confidence") or 0.7))
            pending_translations.append(t)
            history_count += 1
            _ev("chunk_done", {"sid": sid, "chunk_idx": len(translated_chunks),
                               "chunks_total": len(chunks), "confidence": data.get("confidence", 0.7),
                               "needs_human": data.get("needs_human", False),
                               "translation_preview": t[:40]})

            if data.get("needs_human") or data.get("untrusted") or untrusted:
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
            if tm_on and t and t != chk:
                tm_mod.add(cfg, chk, t, rel, seg.id)
            if not data.get("untrusted") and t and chk_plain:
                # 占位符只在首或尾的 chunk 才记录短语记忆
                ph_positions = [m.start() for m in re.finditer(r'\[\[P\d+\]\]', chk)]
                if not ph_positions:
                    save = True
                else:
                    last_end = max(p + len('[[P0]]') for p in ph_positions)
                    all_at_edges = all(
                        p == 0 or p + len('[[P0]]') >= len(chk) - 1
                        for p in ph_positions
                    )
                    save = all_at_edges
                if save:
                    t_plain = re.sub(r"\[\[P\d+\]\]", "", t).strip()
                    if t_plain and "|TEXT|" not in t_plain and "|DST|" not in t_plain:
                        phrases_mod.add(cfg, chk_plain, t_plain)

            # 摘要接力：达到轮次上限时触发
            if summary_on and history_count >= max_history:
                _ev("summary", {"history_count": history_count})
                summary = _summarize_conversation(cfg, client, pending_translations, rel)
                if summary:
                    conversation_summary = summary
                # 重建对话，生成新 context_id
                conversation = [{"role": "system", "content": sysp}]
                history_count = 0
                pending_translations = []
                retranslate_context = None  # 防止残留，导致下一正常段误判为重翻译段
                context_seq += 1
                context_id = f"ctx_{_epoch_ms()}_{page_key}_{context_seq}"

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
        _ev("segment_done", {"sid": sid, "confidence": confidence,
                             "needs_human": needs_human, "review": needs_human})

    # ── 页面级 Auto-Retranslate：主翻译结束后统一重翻译需要 review 的 chunk ──
    auto_rt = cfg.get("llm", "auto_retranslate", default=True)
    max_rt = cfg.get("llm", "auto_retranslate_attempts", default=1)
    seg_failed: dict[str, bool] = {}
    if flagged:
        if auto_rt and max_rt > 0:
            _ev("auto_retranslate", {"count": len(flagged)})
            for item in flagged:
                rt_context = _build_retranslate_context(
                    cfg, rel, item["seg"], segs, state, site_map, plan
                )
                # 每次 auto-retranslate 使用独立递增的 context_id（利于日志归组）
                context_seq += 1
                rt_context_id = f"ctx_{_epoch_ms()}_{page_key}_{context_seq}"
                rt_still_bad = True
                for _ in range(max_rt):
                    rt_sysp = prompts.build_translate_system(
                        cfg, [], guide, user_rules, focus,
                        is_retranslation=True
                    )
                    rt_conv = [{"role": "system", "content": rt_sysp}]
                    rt_conv.append({"role": "user",
                                    "content": prompts.build_retranslate_user(
                                        cfg, item["chk"], rt_context)})
                    rt_data = _translate_chunk_with_repair_multi(
                        cfg, client, rt_conv, rel, item["sid"], item["chk"],
                        task_id=task_id, context_id=rt_context_id
                    )
                    rt_t = (rt_data.get("translation") or "").strip()
                    rt_still_bad = (
                        rt_data.get("needs_human")
                        or rt_data.get("untrusted")
                        or bool(_check_placeholders(item["chk"], rt_t))
                    )
                    if not rt_still_bad:
                        # 成功：替换该 chunk 译文 + confidence
                        item["translated_chunks"][item["chunk_idx"]] = rt_t
                        item["confidences"][item["chunk_idx"]] = float(rt_data.get("confidence") or 0.9)
                        # 清理该段已入队的 glossary_conflict 条目（重翻译解决了冲突）
                        review_queue[:] = [
                            rq for rq in review_queue
                            if not (rq.get("page") == rel and rq.get("segment_id") == item["sid"]
                                    and rq.get("reason") == "glossary_conflict")
                        ]
                        break
                if rt_still_bad:
                    # 方案 A：保留重翻译结果 + 入 review_queue
                    seg_failed[item["sid"]] = True
                    item["translated_chunks"][item["chunk_idx"]] = rt_t
                    item["confidences"][item["chunk_idx"]] = float(rt_data.get("confidence") or 0.1)
                    review_queue.append(
                        _make_review(cfg, rel, item["sid"], item["seg"].text,
                                     "low_confidence", rt_t)
                    )
                else:
                    _ev("chunk_done", {"sid": item["sid"], "chunk_idx": item["chunk_idx"] + 1,
                                       "chunks_total": len(item["translated_chunks"]),
                                       "confidence": float(rt_data.get("confidence") or 0.9),
                                       "needs_human": False,
                                       "translation_preview": item["translated_chunks"][item["chunk_idx"]][:40]})
        else:
            # auto_retranslate 关闭：所有 flagged chunk 直接入 review_queue
            _ev("auto_retranslate", {"count": len(flagged), "skipped": True})
            for item in flagged:
                seg_failed[item["sid"]] = True
                review_queue.append(
                    _make_review(cfg, rel, item["sid"], item["seg"].text,
                                 "low_confidence",
                                 item["translated_chunks"][item["chunk_idx"]])
                )

        # 更新受影响段的译文与状态
        for item in flagged:
            sid = item["sid"]
            seg = item["seg"]
            seg.translation = "".join(item["translated_chunks"])
            seg.confidence = min(item["confidences"]) if item["confidences"] else None
            seg_state = pstate.setdefault("segments", {})[sid]
            seg_state["translation"] = seg.translation
            seg_state["confidence"] = seg.confidence
            if sid not in seg_failed:
                # 该段所有 flagged chunk 均成功
                seg_state["needs_human"] = False
                seg_state["untrusted"] = False
                seg.needs_human = False
            else:
                # 该段有重翻译失败的 chunk → 显式保持 needs_human=True（页面 review）
                seg_state["needs_human"] = True
                seg.needs_human = True

        # 重新计算 review_count 和页面状态
        result["review_count"] = sum(
            1 for sv in pstate.get("segments", {}).values() if sv.get("needs_human")
        )
        if result["review_count"] > 0:
            pstate["status"] = STATUS["review"]
        else:
            pstate["status"] = STATUS["done"]

    _save_segment_index(cfg, rel, segs)
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
    _ev("done", {"status": result["status"], "segments_total": result["segments_total"],
                 "review_count": result["review_count"]})
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
