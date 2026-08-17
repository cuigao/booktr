"""管线编排与 CLI。子命令幂等、基于检查点断点续跑。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import annotator, crawler, glossary as gl, llm as llm_mod
from . import planner, prompts, qa, review as review_mod, segments as seg_mod
from . import styles as styles_mod, translate as tr, util
from .config import Config, ensure_dirs, load_config, save_config


def _client(cfg: Config):
    return llm_mod.LLMClient(cfg)


def cmd_scan(cfg: Config, args) -> None:
    sm = crawler.scan_site(cfg, force=args.force)
    print(f"扫描完成：{sm['total_pages']} 个页面，{sm['total_assets']} 个静态资源")
    from collections import Counter
    secs = Counter(p["section"] for p in sm["pages"].values())
    for k, v in secs.most_common():
        print(f"  {k or '(root)'}: {v}")


def _prompt(label: str, default: str = "") -> str:
    """交互式输入，带默认值提示。"""
    if default:
        suffix = f"（默认: {default}）"
    else:
        suffix = ""
    val = input(f"{label}{suffix}: ").strip()
    return val if val else default


def cmd_init(cfg: Config, args) -> None:
    """交互式初始化：从 config.json.template 生成 <data_dir>/config.json。"""
    config_path = os.path.join(cfg.data_dir, "config.json")
    if os.path.exists(config_path) and not args.force:
        print(f"配置文件已存在：{config_path}")
        print("如需重新生成请加 --force（会覆盖现有配置）。")
        return

    template_path = os.path.join(cfg.root, "config.json.template")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(json.dumps(cfg.data))

    print("== booktr 初始化 ==")
    print("(直接回车使用默认值；配置将写入 data_dir/config.json)")
    print(f"数据根目录（data_dir）: {cfg.data_dir}")
    print("站点源目录可填相对路径（相对数据根）或完整绝对路径（如源镜像在 src 之外）。")
    data["source_dir"] = _prompt("站点源目录", str(data.get("source_dir", "love.life.coocan.jp")))
    data["output_dir"] = _prompt("输出镜像目录", str(data.get("output_dir", "out")))
    data["work_dir"] = _prompt("工作目录", str(data.get("work_dir", "work")))
    data.setdefault("lang", {})
    data["lang"]["source"] = _prompt("源语言代码", str(data.get("lang", {}).get("source", "ja")))
    data["lang"]["target"] = _prompt("目标语言代码", str(data.get("lang", {}).get("target", "zh-Hans")))

    llm = data.setdefault("llm", {})
    print("\n-- LLM 配置 --")
    providers = ["mock", "openai-compatible"]
    provider = _prompt("provider (mock=离线测试 / openai-compatible=真实API)", str(llm.get("provider", "mock")))
    if provider not in providers:
        print(f"警告: 未知 provider {provider}，已使用默认 mock")
        provider = "mock"
    llm["provider"] = provider
    if provider == "openai-compatible":
        llm["base_url"] = _prompt("base_url", str(llm.get("base_url", "https://api.openai.com/v1")))
        llm["model"] = _prompt("model", str(llm.get("model", "gpt-4o-mini")))
        llm["api_key_env"] = _prompt("API key 环境变量名", str(llm.get("api_key_env", "BOOKTR_API_KEY")))

    print("\n-- 增强工具（true/false）--")
    planner = data.setdefault("planner", {})
    planner["context_window"] = int(_prompt("前文上下文窗口（日记页数）", str(planner.get("context_window", 5))))
    style = data.setdefault("style", {})
    style["rules_enabled"] = _prompt("风格指南注入", str(style.get("rules_enabled", True))) not in ("false", "False", "0", "")
    style["exemplar_enabled"] = _prompt("风格样例 few-shot", str(style.get("exemplar_enabled", True))) not in ("false", "False", "0", "")
    data.setdefault("tm", {})["enabled"] = _prompt("翻译记忆 TM", "true") not in ("false", "False", "0", "")
    data.setdefault("qa", {})["deep_llm_check"] = _prompt("深度 QA 检查", "true") not in ("false", "False", "0", "")

    ensure_dirs(cfg)
    cfg.data = data
    save_config(cfg)
    style_refs = os.path.join(cfg.data_dir, "style_refs.json")
    if not os.path.exists(style_refs):
        with open(style_refs, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        print(f"已创建空的风格样例文件 {style_refs}")

    print(f"\n配置已写入: {config_path}")
    print("下一步: python booktr-cli.py scan  →  python booktr-cli.py plan  →  python booktr-cli.py translate")


def cmd_plan(cfg: Config, args) -> None:
    survey = None
    if args.survey:
        survey = run_survey(cfg)
    weights = json.loads(args.weights) if args.weights else None
    directions = {}
    if args.direction:
        for item in args.direction:
            if ":" in item:
                k, _, v = item.partition(":")
                directions[k.strip()] = v.strip()
    plan = planner.build_plan(
        cfg, survey,
        weights=weights, directions=directions or None,
        index_threshold=args.index_threshold, reset_order=args.reset_order,
    )
    print(planner.plan_summary(cfg))
    print(f"\n已写入 {os.path.join(cfg.work_dir, 'plan.json')}")


def run_survey(cfg: Config) -> dict:
    client = _client(cfg)
    sm = planner.load_site_map(cfg)
    pages = sm.get("pages", {})
    lines = [f"{rel}\t{p.get('title','')}" for rel, p in pages.items()]
    sysp = prompts.build_survey_system(cfg)
    usr = prompts.build_survey_user("\n".join(lines))
    resp = client.chat(sysp, usr, temperature=0.4, tag="survey")
    try:
        data = llm_mod.parse_json_response(resp)
    except llm_mod.LLMError:
        data = {}
    util.write_json(os.path.join(cfg.work_dir, "survey.json"), data)
    return data


def cmd_extract_terms(cfg: Config, args) -> None:
    client = _client(cfg)
    sm = planner.load_site_map(cfg)
    pages = list(sm.get("pages", {}).keys())
    limit = cfg.get("glossary", "extract_pages_limit", default=0)
    if limit:
        pages = pages[:limit]
    sysp = prompts.build_glossary_extract_system(cfg)
    all_candidates = []
    for rel in pages:
        raw = open(os.path.join(cfg.source_dir, rel.replace("/", os.sep)), "rb").read()
        html, _ = util.decode_html(raw)
        segs = seg_mod.split_segments(html, cfg)
        text = "\n".join(s.text for s in segs if s.kind == "text")
        if not text.strip():
            continue
        resp = client.chat(sysp, prompts.build_glossary_extract_user(text[:6000]),
                           temperature=0.3, tag="glossary_extract")
        try:
            data = llm_mod.parse_json_response(resp)
        except llm_mod.LLMError:
            continue
        for t in data.get("terms", []) or []:
            if t.get("src") and t.get("dst"):
                all_candidates.append(t)
    # 去重
    seen = {}
    for c in all_candidates:
        s = c["src"]
        if s not in seen or c.get("confidence", 0) > seen[s].get("confidence", 0):
            seen[s] = c
    conflicts = gl.merge_candidates(cfg, list(seen.values()))
    print(f"词汇表候选：{len(seen)} 条（自动状态），冲突 {len(conflicts)} 条")
    if args.interactive and conflicts:
        for c in conflicts:
            print(f"  冲突: {c['src']} -> {c.get('dst')} (已存在其他译文)")


def cmd_style_extract(cfg: Config, args) -> None:
    client = _client(cfg)
    refs = styles_mod.load_refs(cfg)
    if not refs:
        print("style_refs.json 为空或不存在，跳过风格抽取（接口已就绪）。")
        return
    guide = styles_mod.extract_style_guide(cfg, client)
    print(f"风格规则已写入 {styles_mod.guide_path(cfg)}")
    print(guide[:800])


def rebuild_processed_output(cfg: Config) -> int:
    """从段索引离线重组已处理页（done/review）的译文并写回 out。

    用于 out 中已处理页文件缺失/被覆盖时恢复译文，无需重新调用 LLM。
    返回重建页数。
    """
    state = tr.State(cfg)
    processed = [
        rel for rel, p in state.data.get("pages", {}).items()
        if p.get("status") in (tr.STATUS["done"], tr.STATUS["review"])
    ]
    rebuilt = 0
    for rel in processed:
        out_path = os.path.join(cfg.output_dir, rel.replace("/", os.sep))
        if os.path.exists(out_path):
            continue
        segs = seg_mod.segments_for_page(cfg, rel)
        if not segs or not any(s.translation for s in segs):
            continue
        from .crawler import resolve_local_path
        raw = open(resolve_local_path(cfg, rel), "rb").read()
        html, _ = util.decode_html(raw)
        out_html = seg_mod.reassemble(html, segs)
        seg_mod.write_page_output(cfg, rel, out_html)
        rebuilt += 1
    if rebuilt:
        print(f"重建已处理页输出：{rebuilt} 个（离线重组，未调 LLM）")
    return rebuilt


def prepopulate_output(cfg: Config) -> None:
    """将源目录全部文件复制到输出目录（幂等：缺失或大小不同才复制）。

    html 保持原样（未译页以源编码展示）；已处理（done/review）的 html 跳过，
    保留译文；若其 out 文件缺失则从段索引离线重组。其余资源照常复制。
    """
    sm = planner.load_site_map(cfg)
    state = tr.State(cfg)
    processed = set(
        rel for rel, p in state.data.get("pages", {}).items()
        if p.get("status") in (tr.STATUS["done"], tr.STATUS["review"])
    )
    files = list(sm.get("pages", {}).keys()) + list(sm.get("assets", []))
    copied = 0
    for rel in files:
        if rel in processed and rel in sm.get("pages", {}):
            continue
        src = os.path.join(cfg.source_dir, rel.replace("/", os.sep))
        dst = os.path.join(cfg.output_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if not os.path.exists(dst) or os.path.getsize(src) != os.path.getsize(dst):
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
            copied += 1
    if copied:
        print(f"预复制输出镜像：{copied} 个文件（跳过已处理 html {len(processed)}）")
    rebuild_processed_output(cfg)


def _make_progress_callback(verbose: bool = True):
    """创建翻译进度回调。verbose=True 时打印详情到控制台。"""
    def callback(event: str, data: dict) -> None:
        if not verbose:
            return
        if event == "plan":
            print(f"  计划: {data['segments']} 段, {data['total_chars']} 字符")
        elif event == "segment_start":
            preview = data["text_preview"][:30].replace("\n", " ")
            print(f"  [{data['seg_idx']}/{data['total']}] 段{data['sid']} "
                  f"({preview}...) [{data['chunks']} chunk]")
        elif event == "chunk_done":
            conf = data["confidence"]
            flag = " ⚠" if data["needs_human"] else ""
            conf_str = f"{conf:.2f}" if conf is not None else "?"
            print(f"    chunk {data['chunk_idx']}/{data['chunks_total']} "
                  f"[conf={conf_str}]{flag}")
        elif event == "segment_done":
            flag = " ⚠需审核" if data["needs_human"] else ""
            conf_str = f"[conf={data['confidence']:.2f}]" if data["confidence"] is not None else ""
            print(f"  → 段{data['sid']} 完成 {conf_str}{flag}")
        elif event == "repair":
            print(f"  ⚠ 段{data['sid']} 占位符修复: {data['missing']}")
        elif event == "summary":
            print(f"  📝 摘要接力: 已翻译 {data['history_count']} 段")
        elif event == "done":
            flag = " ✓" if data["status"] == "done" else " ⚠"
            print(f"  → 完成{flag} ({data['segments_total']} 段, {data['review_count']} 审核)")
    return callback


def cmd_translate(cfg: Config, args) -> None:
    client = _client(cfg)
    ensure_dirs(cfg)
    prepopulate_output(cfg)
    sm = planner.load_site_map(cfg)
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    if not plan:
        print("未找到 plan.json，先运行 `booktr plan`")
        return
    order = plan.get("order", [])
    state = tr.State(cfg)
    review_items = review_mod.load_queue(cfg)

    summary_on = cfg.get("planner", "summarize", default=True)
    pause_on_review = cfg.get("pause_on_review", default=True) and not args.no_pause

    # 计算目标页列表：--next 只处理接下来 N 个未完成页（不含 review 页）
    if args.next is not None and args.next:
        if args.pages:
            print("错误: --pages 与 --next 不能同时使用", file=sys.stderr)
            sys.exit(2)
        pending = [rel for rel in order
                   if state.page(rel).get("status") == tr.STATUS["pending"]]
        if args.dry_run:
            print(f"下一个待译页（pending {len(pending)}，review 其余）:")
            for rel in pending[: args.next]:
                print(f"  {rel}")
            return
        if not pending:
            print("没有待翻译页（可能全部完成或待审核）。")
            return
        targets = pending[: args.next]
    else:
        if args.pages:
            order = [p for p in order if p in args.pages]
        targets = order

    done = 0
    # 确定 verbose 模式
    verbose = getattr(args, "verbose", None)
    if verbose is None:
        verbose = cfg.get("verbose_translation", default=True)
    progress = _make_progress_callback(verbose)

    for i, rel in enumerate(targets, 1):
        pstate = state.page(rel)
        if pstate.get("status") == tr.STATUS["done"]:
            done += 1
            continue
        tr.process_inbox(cfg, state)
        if summary_on:
            try:
                tr.summarize_page(cfg, client, rel)
            except llm_mod.LLMError as e:
                print(f"  摘要失败 {rel}: {e}")
        print(f"[{i}/{len(targets)}] 翻译 {rel} ...", flush=True)
        try:
            r = tr.translate_page(cfg, client, rel, state, sm, plan, review_items,
                                  interactive=args.interactive, progress=progress)
        except llm_mod.LLMError as e:
            print(f"  ✗ {rel}: {e}")
            break
        if r.get("status") == "review":
            print(f"  ⚠ {rel}: {r.get('review_count', 0)} 段需人工审核")
            review_mod.save_queue(cfg, review_items)
            if pause_on_review and args.interactive:
                review_mod.interactive_review(cfg)
        else:
            done += 1
            print(f"  ✓ {rel} 完成（{r.get('segments_total', 0)} 段）")
        state.save()

        # 自动导出对话日志
        auto_export = cfg.get("llm_logs", "auto_export", default=True)
        if auto_export:
            n = cfg.get("llm_logs", "auto_export_sessions", default=1)
            out = export_page_log(cfg, rel, max_sessions=n)
            if out:
                print(f"  📄 对话日志: {out}")

    review_mod.save_queue(cfg, review_items)
    remaining = sum(1 for rel in order
                    if state.page(rel).get("status") == tr.STATUS["pending"])
    print(f"\n完成 {done}/{len(targets)} 页（剩余待译 {remaining}）。LLM 调用统计:")
    for k, v in client.stats_report().items():
        print(f"  {k}: {v}")


def cmd_review(cfg: Config, args) -> None:
    stats = review_mod.queue_stats(cfg)
    print(f"审核队列：open={stats['open']}  按原因={stats['by_reason']}")
    if args.page:
        items = review_mod.load_queue(cfg)
        for it in items:
            if it.get("page") == args.page and it.get("status") == "open":
                print(f"  [{it['segment_id']}] {it['reason']}: {it['src'][:80]}")
        return
    review_mod.interactive_review(cfg, max_items=args.max_items)


def cmd_qa(cfg: Config, args) -> None:
    client = _client(cfg)
    state = tr.State(cfg)
    done_pages = state.data.get("done_pages", [])
    if args.pages:
        done_pages = [p for p in args.pages if p in done_pages] or args.pages
    report = qa.qa_report(cfg, client, done_pages)
    print(f"QA 报告: 总问题 {report['total_issues']}，高危 {report['high']}，见 qa_report.json")
    for rel, issues in report["pages"].items():
        print(f"  {rel}: {len(issues)} 问题")
        for i in issues[:3]:
            print(f"    段{i['segment_id']} [{i['severity']}] {i['problem']}")
    # 将问题入审核队列
    queue = review_mod.load_queue(cfg)
    for rel, issues in report["pages"].items():
        for i in issues:
            queue.append({"page": rel, "segment_id": i["segment_id"], "src": "",
                          "reason": "qa_" + i["severity"], "detail": i["problem"],
                          "status": "open"})
    review_mod.save_queue(cfg, queue)


def cmd_annotate(cfg: Config, args) -> None:
    client = _client(cfg)
    state = tr.State(cfg)
    done_pages = state.data.get("done_pages", [])
    if args.pages:
        done_pages = args.pages
    total = 0
    for rel in done_pages:
        out_path = os.path.join(cfg.output_dir, rel.replace("/", os.sep))
        if not os.path.exists(out_path):
            continue
        with open(out_path, "r", encoding="utf-8") as f:
            content = f.read()
        n = annotator.generate_for_page(cfg, client, rel, content)
        total += n
        print(f"  {rel}: +{n} 条译者注")
    print(f"译者注总计：{len(annotator.load(cfg))} 条（新增 {total}）")


def cmd_status(cfg: Config, args) -> None:
    state = tr.State(cfg)
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    order = plan.get("order", [])
    done = state.data.get("done_pages", [])
    review_pages = [r for r, p in state.data.get("pages", {}).items() if p.get("status") == "review"]
    print(f"计划 {len(order)} 页 | 已完成 {len(done)} | 待审核 {len(review_pages)}")
    stats = review_mod.queue_stats(cfg)
    print(f"审核队列: open={stats['open']}")
    qs = os.path.join(cfg.work_dir, "qa_report.json")
    if os.path.exists(qs):
        q = util.read_json(qs, {})
        print(f"QA 报告: 问题 {q.get('total_issues', 0)}（高危 {q.get('high', 0)}）")
    tn = os.path.join(cfg.work_dir, "translators_notes.json")
    if os.path.exists(tn):
        print(f"译者注: {len(util.read_json(tn, []))} 条")
    gpath = cfg.get("glossary", "path", default="")
    if os.path.exists(gpath):
        gl_ = util.read_json(gpath, [])
        print(f"词汇表: {len(gl_)} 条（候选 {sum(1 for g in gl_ if g.get('status')=='auto-candidate')}）")


def cmd_export(cfg: Config, args) -> None:
    """将 work 产物导出/确认到输出目录。"""
    ensure_dirs(cfg)
    # 复制静态资源
    sm = planner.load_site_map(cfg)
    copied = 0
    for a in sm.get("assets", []):
        src = os.path.join(cfg.source_dir, a.replace("/", os.sep))
        dst = os.path.join(cfg.output_dir, a.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if not os.path.exists(dst) or os.path.getsize(src) != os.path.getsize(dst):
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
            copied += 1
    print(f"静态资源已复制（新增/更新 {copied}）。")
    # 汇总提示
    out_json = os.path.join(cfg.work_dir, "export_manifest.json")
    util.write_json(out_json, {
        "output_dir": cfg.output_dir,
        "translators_notes": annotator._notes_path(cfg),
        "notes": cfg.get("notes", "path", default=""),
        "glossary": cfg.get("glossary", "path", default=""),
        "segments_dir": cfg.get("segments_dir", default=""),
    })
    print(f"输出清单: {out_json}")


# ── clean 命令 ──────────────────────────────────────────────────────────


class _CleanItem:
    """可清理项的描述。"""

    def __init__(self, key: str, label: str, path: str, kind: str,
                 count_fn=None, default: bool = True):
        self.key = key
        self.label = label
        self.path = path          # 相对于 work_dir 的路径（或绝对）
        self.kind = kind          # file | dir | json_reset
        self.count_fn = count_fn  # callable(cfg) -> str 显示条目数
        self.default = default
        self.count_str = ""

    def load_count(self, cfg: Config) -> None:
        if self.count_fn:
            self.count_str = self.count_fn(cfg)

    def clean(self, cfg: Config) -> None:
        if self.kind == "dir":
            d = self._abs(cfg)
            if os.path.isdir(d):
                shutil.rmtree(d)
                os.makedirs(d, exist_ok=True)
        elif self.kind == "file":
            p = self._abs(cfg)
            if os.path.exists(p):
                os.remove(p)
        elif self.kind == "json_reset":
            p = self._abs(cfg)
            util.write_json(p, {} if "state" not in self.key else self._reset_state(cfg))
        elif self.kind == "json_empty":
            p = self._abs(cfg)
            util.write_json(p, [])
        elif self.kind == "truncate":
            p = self._abs(cfg)
            with open(p, "w", encoding="utf-8") as f:
                pass  # 清空

    def _abs(self, cfg: Config) -> str:
        if os.path.isabs(self.path):
            return self.path
        return os.path.join(cfg.work_dir, self.path)

    def _reset_state(self, cfg: Config) -> dict:
        state_path = os.path.join(cfg.work_dir, "state.json")
        data = util.read_json(state_path, {})
        for p in data.get("pages", {}).values():
            p["status"] = "pending"
            p["segments"] = {}
        data["done_pages"] = []
        return data


def _count_dir_files(cfg: Config, subdir: str) -> str:
    d = os.path.join(cfg.work_dir, subdir)
    if not os.path.isdir(d):
        return "0 文件"
    n = sum(1 for _ in os.scandir(d) if _.is_file())
    return f"{n} 文件"


def _count_jsonl(cfg: Config, filename: str) -> str:
    p = os.path.join(cfg.work_dir, filename)
    if not os.path.exists(p):
        return "0 条"
    with open(p, encoding="utf-8") as f:
        n = sum(1 for line in f if line.strip())
    return f"{n} 条"


def _count_json_list(cfg: Config, filename: str) -> str:
    p = os.path.join(cfg.work_dir, filename)
    if not os.path.exists(p):
        return "0 条"
    data = util.read_json(p, [])
    return f"{len(data)} 条"


def _count_state(cfg: Config) -> str:
    p = os.path.join(cfg.work_dir, "state.json")
    data = util.read_json(p, {})
    pages = data.get("pages", {})
    done = len(data.get("done_pages", []))
    return f"{len(pages)} 页 ({done} 已完成)"


def _count_output(cfg: Config) -> str:
    d = cfg.output_dir
    if not os.path.isdir(d):
        return "0 文件"
    n = sum(1 for r, _, fs in os.walk(d) for f in fs if f.endswith(".html"))
    return f"{n} 文件"


def _build_clean_items(cfg: Config, args) -> list[_CleanItem]:
    items = [
        _CleanItem("phrase_memory", "短语记忆", "phrase_memory.json", "json_empty",
                   lambda c: _count_json_list(c, "phrase_memory.json")),
        _CleanItem("tm", "翻译记忆 TM", "tm.jsonl", "truncate",
                   lambda c: _count_jsonl(c, "tm.jsonl")),
        _CleanItem("segments", "段缓存", "segments", "dir",
                   lambda c: _count_dir_files(c, "segments")),
        _CleanItem("summaries", "页面摘要", "summaries", "dir",
                   lambda c: _count_dir_files(c, "summaries")),
        _CleanItem("llm_logs", "LLM 日志", "llm_logs", "dir",
                   lambda c: _count_dir_files(c, "llm_logs")),
        _CleanItem("state", "翻译状态", "state.json", "json_reset",
                   lambda c: _count_state(c)),
        _CleanItem("review", "审核队列", "review_queue.json", "json_empty",
                   lambda c: _count_json_list(c, "review_queue.json")),
        _CleanItem("notes", "翻译笔记", "notes.jsonl", "truncate",
                   lambda c: _count_jsonl(c, "notes.jsonl")),
        _CleanItem("output", "输出目录", cfg.output_dir, "dir",
                   lambda c: _count_output(c), default=False),
        _CleanItem("plan", "计划", "plan.json", "file",
                   lambda c: "存在" if os.path.exists(os.path.join(c.work_dir, "plan.json")) else "不存在",
                   default=False),
        _CleanItem("site_map", "站点地图", "site_map.json", "file",
                   lambda c: "存在" if os.path.exists(os.path.join(c.work_dir, "site_map.json")) else "不存在",
                   default=False),
    ]
    # --all: 选中默认项 + output（但不含 plan/site_map，需 --reset）
    if args.all:
        for it in items:
            if it.key in ("plan", "site_map"):
                it.default = False
            elif it.key == "output":
                it.default = True
            else:
                it.default = True
    # --reset: 额外选中 plan + site_map
    if getattr(args, "reset", False):
        for it in items:
            if it.key in ("plan", "site_map"):
                it.default = True
    # 单独选项: 强制选中
    for it in items:
        if getattr(args, it.key.replace("-", "_"), False):
            it.default = True
    return items


def _interactive_select(items: list[_CleanItem]) -> list[_CleanItem]:
    """交互式选择要清理的项。返回选中的列表。"""
    print("\n将清理以下自动产物：\n")
    for i, it in enumerate(items, 1):
        mark = "[x]" if it.default else "[ ]"
        print(f"  {mark} {it.label} ({it.count_str})")
    print()
    val = input("确认清理？[y/N] ").strip().lower()
    if val in ("y", "yes"):
        return [it for it in items if it.default]
    print("已取消。")
    return []


def cmd_clean(cfg: Config, args) -> None:
    """清理翻译缓存，从全新状态开始。"""
    items = _build_clean_items(cfg, args)
    for it in items:
        it.load_count(cfg)

    if args.yes:
        selected = [it for it in items if it.default]
    else:
        selected = _interactive_select(items)

    if not selected:
        return

    for it in selected:
        it.clean(cfg)
        print(f"  已清理: {it.label} ({it.count_str})")

    print(f"\n完成，共清理 {len(selected)} 项。")


def _find_page_logs(cfg: Config, page: str) -> list[dict]:
    """查找指定页面的所有 LLM 日志。"""
    import glob as _glob
    import re as _re

    log_dir = cfg.get("llm_logs", "dir", default="work/llm_logs")
    if not os.path.isdir(log_dir):
        return []

    all_logs = []
    page_key = page.replace("/", "_").replace("\\", "_").replace(".html", "")
    for f in sorted(_glob.glob(os.path.join(log_dir, "*.json"))):
        d = util.read_json(f, {})
        tag = d.get("tag", "")
        base = tag
        for prefix in ("translate_", "repair_", "summarize_conv_", "summarize_"):
            if base.startswith(prefix):
                base = base[len(prefix):]
                break
        base = _re.sub(r"_seg\d+$", "", base)
        base_key = base.replace(".html", "")
        if base_key == page_key or base == page:
            all_logs.append(d)
    return all_logs


def _group_logs_by_task(logs: list[dict]) -> list[list[dict]]:
    """按 task_id 分组日志为翻译任务。无 task_id 的旧日志按消息数分组。"""
    import re as _re

    # 按时间排序
    logs.sort(key=lambda x: x.get("ts", ""))

    # 检查是否有 task_id（新日志）
    has_task_id = any(log.get("task_id") for log in logs)

    if has_task_id:
        # 按 task_id 分组
        task_groups: dict[str, list[dict]] = {}
        for log in logs:
            tid = log.get("task_id", f"old_{log.get('ts', '')}")
            task_groups.setdefault(tid, []).append(log)
        return list(task_groups.values())
    else:
        # 旧日志：按消息数分组
        sessions = []
        current_session = []
        prev_msg_count = 0
        for log in logs:
            user_field = log.get("user", "")
            m = _re.match(r"\[(\d+) msgs\]", user_field)
            msg_count = int(m.group(1)) if m else 0
            if msg_count > 0 and msg_count <= prev_msg_count:
                if current_session:
                    sessions.append(current_session)
                current_session = []
            current_session.append(log)
            if msg_count > 0:
                prev_msg_count = msg_count
        if current_session:
            sessions.append(current_session)
        return sessions


def _group_logs_by_context(task_logs: list[dict]) -> list[list[dict]]:
    """将一个任务的日志按 context_id 分组为多轮对话。"""
    import re as _re

    ctx_groups: dict[str, list[dict]] = {}
    for log in task_logs:
        cid = log.get("context_id", "")
        if not cid:
            # 旧日志或非对话日志：用消息数推断
            user_field = log.get("user", "")
            m = _re.match(r"\[(\d+) msgs\]", user_field)
            msg_count = int(m.group(1)) if m else 0
            if msg_count <= 2:
                cid = f"ctx_inferred_{log.get('ts', '')}"
            else:
                # 继续上一个 context
                if ctx_groups:
                    cid = list(ctx_groups.keys())[-1]
                else:
                    cid = f"ctx_inferred_{log.get('ts', '')}"
        ctx_groups.setdefault(cid, []).append(log)
    return list(ctx_groups.values())


def _format_call_markdown(call_idx: int, log: dict, prev_msg_len: int) -> tuple[list[str], int]:
    """格式化单次调用为 Markdown 行，返回 (lines, new_msg_len)。"""
    import re as _re

    tag = log.get("tag", "")
    ts = log.get("ts", "")[:19]
    ok = log.get("ok", True)
    error = log.get("error", "")
    usage = log.get("usage", {})
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)

    if "repair" in tag:
        call_type = "repair"
    elif "summarize_conv" in tag:
        call_type = "摘要接力"
    elif "summarize" in tag:
        call_type = "摘要"
    elif "translate" in tag:
        call_type = "翻译"
    else:
        call_type = "其他"

    user_field = log.get("user", "")
    m = _re.match(r"\[(\d+) msgs\]", user_field)
    msg_count = int(m.group(1)) if m else 0
    msg_str = f"[{msg_count} msgs]" if msg_count else ""

    lines = [
        f"### 调用 #{call_idx} | {call_type} | {msg_str} | {ts}",
    ]
    if not ok:
        lines.append(f"**失败**: {error}")
    if tokens:
        lines.append(f"tokens: {tokens}")
    lines.append("")

    messages = log.get("messages", [])
    response = log.get("response", "")
    new_msg_len = prev_msg_len

    if messages:
        new_msgs = messages[prev_msg_len:]
        for msg in new_msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system" and prev_msg_len > 0 and call_type != "摘要接力":
                lines.append(f"### system（同上）")
            else:
                lines.append(f"### {role}")
                lines.append(content)
            lines.append("")
        new_msg_len = len(messages)
    else:
        system = log.get("system", "")
        user_raw = log.get("user", "")
        user_clean = _re.sub(r"^\[\d+ msgs\]\s*", "", user_raw)
        if call_idx == 1:
            if system:
                lines.append("### system")
                lines.append(system)
                lines.append("")
            lines.append("### user")
            lines.append(user_clean)
            lines.append("")
        else:
            lines.append("### user")
            lines.append(user_clean)
            lines.append("")

    if response:
        lines.append(f"### assistant")
        try:
            parsed = json.loads(response) if response.startswith("{") else None
            if parsed:
                lines.append("```json")
                lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
                lines.append("```")
            else:
                lines.append(response)
        except (json.JSONDecodeError, ValueError):
            lines.append(response)
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines, new_msg_len


def export_page_log(cfg: Config, page: str, max_sessions: int | None = None,
                    output_path: str | None = None) -> str | None:
    """导出指定页面的 LLM 对话日志为 Markdown。

    Args:
        page: 页面路径
        max_sessions: 最多导出最近 N 个翻译任务（None=全部）
        output_path: 输出路径（None=自动）

    Returns:
        输出文件路径，无日志时返回 None
    """
    all_logs = _find_page_logs(cfg, page)
    if not all_logs:
        return None

    task_groups = _group_logs_by_task(all_logs)

    # 取最近 N 个任务
    if max_sessions and len(task_groups) > max_sessions:
        task_groups = task_groups[-max_sessions:]

    lines = [
        f"# LLM 对话日志：{page}",
        f"共 {len(task_groups)} 次翻译任务，{len(all_logs)} 条调用记录",
        "",
    ]

    for task_idx, task_logs in enumerate(task_groups, 1):
        task_id = task_logs[0].get("task_id", "")
        ts_start = task_logs[0].get("ts", "")[:19]
        ts_end = task_logs[-1].get("ts", "")[:19]
        ctx_groups = _group_logs_by_context(task_logs)

        lines.append(f"---")
        lines.append(f"## 任务 #{task_idx}（{task_id}）")
        lines.append(f"{ts_start} ~ {ts_end} | {len(ctx_groups)} 轮对话")
        lines.append("")

        for ctx_idx, ctx_logs in enumerate(ctx_groups, 1):
            ctx_id = ctx_logs[0].get("context_id", "")
            lines.append(f"### 对话 #{ctx_idx}（{ctx_id}）")
            lines.append("")

            prev_msg_len = 0
            for call_idx, log in enumerate(ctx_logs, 1):
                call_lines, prev_msg_len = _format_call_markdown(call_idx, log, prev_msg_len)
                lines.extend(call_lines)

    # 写入文件
    if output_path:
        out_path = output_path
    else:
        logs_dir = os.path.join(cfg.work_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        safe_name = page.replace("/", "__").replace("\\", "__")
        out_path = os.path.join(logs_dir, f"{safe_name}.md")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def cmd_export_log(cfg: Config, args) -> None:
    """导出指定页面的完整 LLM 对话日志为人类可读的 Markdown。"""
    out_path = export_page_log(cfg, args.page, max_sessions=args.sessions,
                               output_path=args.output)
    if out_path:
        print(f"对话日志已导出: {out_path}")
    else:
        print(f"未找到 {args.page} 的日志")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="booktr", description="古早网站本地化翻译 agent")

    # 公共参数父解析器：data-dir 既能放子命令前也能放子命令后
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--data-dir", default=None,
        help="数据根目录（绝对或相对项目根；决定 config.json 位置，所有相对路径据此解析，"
             "支持多站点独立工作区）",
    )
    p.add_argument(
        "--data-dir", default=None,
        help=argparse.SUPPRESS,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp) -> None:
        for act in parent._actions:
            if act.dest != "help":
                sp.add_argument(
                    *act.option_strings, dest=act.dest, default=argparse.SUPPRESS,
                    help=argparse.SUPPRESS,
                )

    def mk(name, **kw):
        sp = sub.add_parser(name, **kw)
        add_common(sp)
        return sp

    sp = mk("init", help="交互式初始化配置（从模板生成 data/config.json）")
    sp.add_argument("--force", action="store_true", help="覆盖现有配置")
    sp.set_defaults(func=cmd_init)

    sp = mk("scan", help="扫描站点镜像")
    sp.add_argument("--force", action="store_true", help="强制重新扫描")
    sp.set_defaults(func=cmd_scan)

    sp = mk("plan", help="生成翻译顺序计划")
    sp.add_argument("--survey", action="store_true", help="启用 LLM 语义分析层（默认关闭）")
    sp.add_argument("--weights", default=None,
                    help="JSON 权重覆盖，如 '{\"semantic\":0.4,\"hotness\":0.1}'")
    sp.add_argument("--index-threshold", type=float, default=None,
                    help="索引判定覆盖比例阈值（默认 0.6）")
    sp.add_argument("--direction", action="append", default=None,
                    help="方向覆盖，如 semantic:reverse（可多次）")
    sp.add_argument("--reset-order", action="store_true",
                    help="用自动顺序重置 order（丢弃手动调整）")
    sp.set_defaults(func=cmd_plan)

    sp = mk("extract-terms", help="从语料抽取词汇表候选")
    sp.add_argument("--interactive", action="store_true", help="交互式确认冲突")
    sp.set_defaults(func=cmd_extract_terms)

    sp = mk("style-extract", help="从 style_refs 提炼风格规则")
    sp.set_defaults(func=cmd_style_extract)

    sp = mk("translate", help="逐页翻译")
    sp.add_argument("--pages", nargs="*", help="限定翻译的页面")
    sp.add_argument("--next", nargs="?", type=int, const=1, default=None,
                    help="翻译接下来 N 个未完成页（不含 review 页；缺省 1）")
    sp.add_argument("--dry-run", action="store_true", help="仅显示下一个/批待译页，不翻译")
    sp.add_argument("--interactive", action="store_true", help="交互模式")
    sp.add_argument("--no-pause", action="store_true", help="遇到审核项不暂停")
    sp.add_argument("-v", "--verbose", action="store_true", default=None,
                    help="显示翻译进度详情（默认跟随 config verbose_translation）")
    sp.set_defaults(func=cmd_translate)

    sp = mk("review", help="处理审核队列")
    sp.add_argument("--page", default=None, help="只看某页的审核项")
    sp.add_argument("--max-items", type=int, default=0, help="最多处理条数")
    sp.set_defaults(func=cmd_review)

    sp = mk("qa", help="一致性 QA pass")
    sp.add_argument("--pages", nargs="*", help="限定检查页面")
    sp.set_defaults(func=cmd_qa)

    sp = mk("annotate", help="生成译者注")
    sp.add_argument("--pages", nargs="*", help="限定页面")
    sp.set_defaults(func=cmd_annotate)

    sp = mk("status", help="查看进度")
    sp.set_defaults(func=cmd_status)

    sp = mk("export", help="导出输出镜像与静态资源")
    sp.set_defaults(func=cmd_export)

    sp = mk("clean", help="清理翻译缓存，从全新状态开始")
    sp.add_argument("--all", action="store_true", help="清理所有自动产物（含 output，不含 plan/site_map）")
    sp.add_argument("--phrase-memory", action="store_true", help="清理短语记忆")
    sp.add_argument("--tm", action="store_true", help="清理翻译记忆")
    sp.add_argument("--segments", action="store_true", help="清理段缓存")
    sp.add_argument("--summaries", action="store_true", help="清理页面摘要")
    sp.add_argument("--llm-logs", action="store_true", help="清理 LLM 日志")
    sp.add_argument("--state", action="store_true", help="重置翻译状态")
    sp.add_argument("--review", action="store_true", help="清理审核队列")
    sp.add_argument("--notes", action="store_true", help="清理翻译笔记")
    sp.add_argument("--output", action="store_true", help="清理输出目录")
    sp.add_argument("--reset", action="store_true", help="额外清理 plan.json 和 site_map.json")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过交互确认")
    sp.set_defaults(func=cmd_clean)

    sp = mk("export-log", help="导出指定页面的完整 LLM 对话日志为 Markdown")
    sp.add_argument("page", help="页面路径，如 today/today6.html")
    sp.add_argument("-o", "--output", default=None, help="输出文件路径（默认 work/logs/<page>.md）")
    sp.add_argument("-s", "--sessions", type=int, default=None,
                    help="最多导出最近 N 个翻译任务（默认全部）")
    sp.set_defaults(func=cmd_export_log)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(data_dir=args.data_dir)
    ensure_dirs(cfg)
    try:
        args.func(cfg, args)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0
