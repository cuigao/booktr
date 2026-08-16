"""管线编排与 CLI。子命令幂等、基于检查点断点续跑。"""
from __future__ import annotations

import argparse
import json
import os
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
                                  interactive=args.interactive)
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
