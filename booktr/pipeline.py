"""管线编排与 CLI。子命令幂等、基于检查点断点续跑。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

from . import annotator, crawler, glossary as gl, llm as llm_mod
from . import history as hist_mod, locate as locate_mod
from . import planner, prefs as prefs_mod, prompts, qa, residual as residual_mod
from . import review as review_mod, segments as seg_mod
from . import styles as styles_mod, translate as tr, util
from .config import DEFAULTS, Config, ensure_dirs, load_config, save_config


def _client(cfg: Config):
    return llm_mod.LLMClient(cfg)


def cmd_scan(cfg: Config, args) -> None:
    sm = crawler.scan_site(cfg, force=args.force)
    print(f"扫描完成：{sm['total_pages']} 个页面，{sm['total_assets']} 个静态资源")
    from collections import Counter
    secs = Counter(p["section"] for p in sm["pages"].values())
    for k, v in secs.most_common():
        print(f"  {k or '(root)'}: {v}")
    failed = sm.get("encoding_failed", [])
    if failed:
        print(f"\n⚠ 无法解码的页面（{len(failed)} 个），已跳过扫描：")
        for rel in failed:
            print(f"  - {rel}")
        print("请运行 `booktr fix` 查看/修复这些文件（输出到 fix 目录，不改原始文件）。")


def _prompt(label: str, default: str = "") -> str:
    """交互式输入，带默认值提示。"""
    if default:
        suffix = f"（默认: {default}）"
    else:
        suffix = ""
    val = input(f"{label}{suffix}: ").strip()
    return val if val else default


def _select(label: str, options: list[str], default: str = "") -> str:
    """交互式选择，显示编号选项。"""
    print(f"{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  [{i}] {opt}")
    val = input(f"选择 (1-{len(options)}，默认: {default}): ").strip()
    if not val:
        return default
    try:
        idx = int(val) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        pass
    if val in options:
        return val
    print(f"  无效选择，使用默认: {default}")
    return default


def _select_lang(label: str, default: str = "") -> str:
    """交互式选择语言，支持自定义输入。"""
    from .prompts import LANG_OPTIONS
    print(f"{label}:")
    for i, (code, name) in enumerate(LANG_OPTIONS, 1):
        print(f"  [{i}] {name} ({code})")
    print(f"  [{len(LANG_OPTIONS)+1}] 自定义（输入语言代码或全名）")
    val = input(f"选择 (1-{len(LANG_OPTIONS)+1}，默认: {default}): ").strip()
    if not val:
        return default
    try:
        idx = int(val) - 1
        if 0 <= idx < len(LANG_OPTIONS):
            return LANG_OPTIONS[idx][0]
        if idx == len(LANG_OPTIONS):
            custom = input("  输入语言代码或全名: ").strip()
            return custom if custom else default
    except ValueError:
        pass
    # 直接输入（可能是语言代码或全名）
    return val


_SHANGHAI_STYLE_RULES = (
    "## 翻译风格：上海话\n"
    "- 本页译文以简体中文（上海话）风格呈现\n"
    "- 使用上海话（沪语）表达，保留口语特征（如「阿拉」「侬」「伊」「蛮好」「勿要」「哪能」等）\n"
    "- 语气自然口语化，可适当使用上海话语气词\n"
    "- 全角写法、专名处理、占位符与 JSON 格式等前述要求不变"
)

# 翻译风格预设：(key, label, 追加到 user_rules 的规则块)。standard 为空表示不改动。
TRANSLATION_STYLES = [
    ("standard", "标准", ""),
    ("shanghai", "上海话", _SHANGHAI_STYLE_RULES),
]

# API key 提供方式预设：(key, label)。plain=明文写入 config；env=从环境变量读取；none=无需 key。
KEY_MODES = [
    ("plain", "明文 key（写入 config）"),
    ("env", "环境变量"),
    ("none", "无需 key（本地服务）"),
]


def _key_mode_default(llm: dict) -> str:
    """由当前 llm 层派生默认 key 模式：非空 api_key→plain；required→env；否则 none。"""
    if str(llm.get("api_key") or ""):
        return "plain"
    return "env" if bool(llm.get("api_key_required", True)) else "none"


def apply_style_preset(user_rules: str, key: str) -> str:
    """把指定风格预设追加到 user_rules 末尾；standard 或未知 key 原样返回。

    幂等：若 user_rules 已含该预设块（以块首行 marker 判定），不重复追加。
    """
    rules = next((r for k, _, r in TRANSLATION_STYLES if k == key), "")
    if not rules:
        return user_rules
    marker = rules.splitlines()[0]
    if marker and marker in user_rules:
        return user_rules
    if not user_rules:
        return rules
    return user_rules + "\n\n" + rules


def _resolve_clone_dir(root: str, clone: str) -> str:
    """把 init --clone 解析为绝对数据根（与 --data-dir 同规则：相对项目根）。"""
    if os.path.isabs(clone):
        return os.path.normpath(clone)
    return os.path.normpath(os.path.join(root, clone))


def cmd_init(cfg: Config, args) -> None:
    """交互式初始化：从 config.json.template 或 ``--clone`` 实例生成 <data_dir>/config.json。

    ``--clone <data_dir>``：以已存在实例的 config.json 作为**默认配置层**（替代模板，
    仍深合并 DEFAULTS 兜底），逐项提示时直接回车即沿用其值，主动输入才覆盖。
    同时继承该实例的 glossary 与 style_refs（`--prefs` 若给出则随后覆盖）。
    """
    config_path = os.path.join(cfg.data_dir, "config.json")
    if os.path.exists(config_path) and not args.force:
        print(f"配置文件已存在：{config_path}")
        print("如需重新生成请加 --force（会覆盖现有配置）。")
        return

    clone = getattr(args, "clone", None)
    clone_dir = _resolve_clone_dir(cfg.root, clone) if clone else None

    if clone_dir:
        clone_cfg_path = os.path.join(clone_dir, "config.json")
        if not os.path.exists(clone_cfg_path):
            print(f"⚠ --clone 实例缺少 config.json：{clone_cfg_path}（回退到模板默认值）")
            clone_dir = None

    if clone_dir:
        # 以 clone 实例的完整配置作为默认层（深合并到 DEFAULTS 之上兜底）
        with open(os.path.join(clone_dir, "config.json"), "r", encoding="utf-8") as f:
            clone_data = json.load(f)
        data = json.loads(json.dumps(DEFAULTS))
        Config._deep_merge(data, clone_data)
    else:
        template_path = os.path.join(cfg.root, "config.json.template")
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(json.dumps(cfg.data))

    print("== booktr 初始化 ==")
    print("(直接回车使用默认值；配置将写入 data_dir/config.json)")
    print(f"数据根目录（data_dir）: {cfg.data_dir}")
    if clone_dir:
        print(f"默认配置层: --clone {clone_dir}")
    print("站点源目录可填相对路径（相对数据根）或完整绝对路径（如源镜像在 src 之外）。")
    # C：仅 source_dir 相对路径按新数据根重算（指向同一站点）；output/work 保持相对实例本地。
    src_default = str(data.get("source_dir", "love.life.coocan.jp"))
    if clone_dir and not os.path.isabs(src_default):
        try:
            abs_site = os.path.normpath(os.path.join(clone_dir, src_default))
            src_default = os.path.relpath(abs_site, cfg.data_dir).replace(os.sep, "/")
        except ValueError:
            pass  # 跨盘符等无法计算相对路径时，保留原值
    data["source_dir"] = _prompt("站点源目录", src_default)
    data["output_dir"] = _prompt("输出镜像目录", str(data.get("output_dir", "out")))
    data["work_dir"] = _prompt("工作目录", str(data.get("work_dir", "work")))
    data.setdefault("lang", {})
    data["lang"]["source"] = _select_lang("源语言", str(data.get("lang", {}).get("source", "ja")))
    data["lang"]["target"] = _select_lang("目标语言", str(data.get("lang", {}).get("target", "zh-Hans")))

    print("\n-- 翻译风格 --")
    print("  （写入 user_rules 实现风格化译文；预设可扩展）")
    # 偏好文件先于风格载入：其 user_rules 作为风格追加的基础
    prefs_data = None
    if getattr(args, "prefs", None):
        try:
            prefs_data = prefs_mod.load(args.prefs)
        except ValueError as e:
            print(f"⚠ 偏好导入失败: {e}")
    base_rules = (prefs_data.get("user_rules") if prefs_data
                  else data.get("user_rules", ""))
    style_labels = [label for _, label, _ in TRANSLATION_STYLES]
    chosen = _select("翻译风格", style_labels, "标准")
    style_key = next((k for k, label, _ in TRANSLATION_STYLES if label == chosen), "standard")
    data["user_rules"] = apply_style_preset(str(base_rules or ""), style_key)

    llm = data.setdefault("llm", {})
    print("\n-- LLM 配置 --")
    print("  （mock：离线测试，恒等翻译；openai-compatible：接入真实 API 服务）")
    providers = ["mock", "openai-compatible"]
    llm["provider"] = _select("LLM provider", providers, str(llm.get("provider", "mock")))
    if llm["provider"] not in providers:
        print(f"  警告: 未知 provider {llm['provider']}，已使用默认 mock")
        llm["provider"] = "mock"
    if llm["provider"] == "openai-compatible":
        llm["base_url"] = _prompt("base_url", str(llm.get("base_url", "https://api.openai.com/v1")))
        llm["model"] = _prompt("model", str(llm.get("model", "gpt-4o-mini")))
        mode_labels = [label for _, label in KEY_MODES]
        # 默认：直接 init → 明文；--clone → 镜像源实例（源为 env 则默认 env）
        default_mode = _key_mode_default(llm) if clone_dir else "plain"
        default_label = next(label for k, label in KEY_MODES if k == default_mode)
        chosen_mode = _select("API key 提供方式", mode_labels, default_label)
        mode = next((k for k, label in KEY_MODES if label == chosen_mode), default_mode)
        if mode == "plain":
            existing_key = str(llm.get("api_key") or "")
            hint = "（回车保留现有 key；输入 '-' 清空）" if existing_key else "（留空则无需 key；否则直接写入 config）"
            raw_key = input(f"API key{hint}: ").strip()
            if not raw_key:
                raw_key = existing_key  # 回车保留（--clone 时可继承）
            elif raw_key == "-":
                raw_key = ""
            llm["api_key"] = raw_key
            llm["api_key_required"] = bool(raw_key)
        elif mode == "env":
            env_name = _prompt("API key 环境变量名",
                               str(llm.get("api_key_env", "BOOKTR_API_KEY") or "BOOKTR_API_KEY"))
            llm["api_key"] = ""
            llm["api_key_env"] = env_name
            llm["api_key_required"] = True
        else:  # none
            llm["api_key"] = ""
            llm["api_key_required"] = False

    print("\n-- 增强工具（true/false）--")
    planner = data.setdefault("planner", {})
    ctx = planner.setdefault("context", {})
    ctx["plan_predecessors"] = int(_prompt("前文上下文：plan 前导数量（N1）", str(ctx.get("plan_predecessors", 5))))
    ctx["time_predecessors"] = int(_prompt("时间前导数量（N2）", str(ctx.get("time_predecessors", 3))))
    ctx["link_predecessors"] = int(_prompt("链接前导数量（N3）", str(ctx.get("link_predecessors", 3))))
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

    # --clone：继承该实例的 glossary 与 style_refs（后续 --prefs 会覆盖）
    if clone_dir:
        from . import glossary as gl_mod
        with open(os.path.join(clone_dir, "config.json"), "r", encoding="utf-8") as f:
            clone_inst_data = json.load(f)
        clone_cfg = Config(root=cfg.root, data=clone_inst_data, data_dir=clone_dir)
        clone_gl = gl_mod.load(clone_cfg)
        if clone_gl:
            gl_mod.save(cfg, clone_gl)
        clone_refs_path = clone_cfg.get("style", "refs_path", default="style_refs.json")
        clone_refs = util.read_json(clone_refs_path, [])
        if clone_refs:
            util.write_json(cfg.get("style", "refs_path", default="style_refs.json"), clone_refs)
        print(f"已继承 --clone 实例数据: glossary {len(clone_gl)} 条 / style_refs {len(clone_refs)} 条")

    # 偏好导入（显式 --prefs）：glossary / style_refs 落盘（覆盖 --clone 继承）
    if prefs_data is not None:
        summary = prefs_mod.apply_data_files(cfg, prefs_data)
        print(f"已导入偏好: {args.prefs}")
        print(f"  user_rules: {'已设置' if prefs_data.get('user_rules') else '未包含'}")
        print(f"  glossary: {summary['glossary']} 条")
        print(f"  style_refs: {summary['style_refs']} 条")

    print(f"\n配置已写入: {config_path}")
    print("下一步: python booktr-cli.py scan  →  python booktr-cli.py plan  →  python booktr-cli.py translate")


def cmd_export_prefs(cfg: Config, args) -> None:
    """将当前实例偏好导出到指定文件。"""
    try:
        path = prefs_mod.export(cfg, args.path)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    data = prefs_mod.collect(cfg)
    print(f"已导出偏好: {path}")
    print(f"  user_rules: {'已设置' if data['user_rules'] else '空'}")
    print(f"  glossary: {len(data['glossary'])} 条")
    print(f"  style_refs: {len(data['style_refs'])} 条")


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
        html, _ = crawler.decode_page(cfg, rel)
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
        html, _ = crawler.decode_page(cfg, rel)
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
        elif event == "retranslate":
            print(f"  🔄 重新翻译: 段{data['sid']}（{'有上下文' if data['has_context'] else '无上下文'}）")
        elif event == "auto_retranslate":
            if data.get("skipped"):
                print(f"  🚫 自动重翻译已关闭，{data['count']} 个 chunk 进入 review 队列")
            else:
                print(f"  🔄 自动重翻译: {data['count']} 个 chunk")
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


def cmd_regenerate(cfg: Config, args) -> None:
    """重新生成指定页面的 out 文件（从段索引离线重组）。"""
    if args.all:
        state = tr.State(cfg)
        pages = [rel for rel, p in state.data.get("pages", {}).items()
                 if p.get("status") in (tr.STATUS["done"], tr.STATUS["review"])]
    else:
        pages = args.pages

    if not pages:
        print("未指定页面")
        return

    ok_count = 0
    for page in pages:
        success = review_mod._regenerate_page(cfg, page)
        if success:
            print(f"  ✓ 已重生成: {page}")
            ok_count += 1
        else:
            print(f"  ✗ 无法重生成: {page}")

    print(f"\n重生成完成: {ok_count}/{len(pages)}")


def _segment_source_map(cfg: Config, rel: str) -> dict:
    seg_cache_path = os.path.join(cfg.get("segments_dir", default=""),
                                  rel.replace("/", "__") + ".json")
    raw = util.read_json(seg_cache_path, {}) if os.path.exists(seg_cache_path) else {}
    scache = raw.get("segments", raw) if isinstance(raw, dict) else raw
    return {str(it.get("id")): it.get("text", "")
            for it in scache if isinstance(it, dict)}


def _purge_phrases_for_segments(cfg: Config, rel: str, sids, segs: dict,
                                dry_run: bool = False) -> int:
    """按 reset 语义（to="" 译文被清除）清理目标段的短语记忆。"""
    from . import phrases as phrases_mod

    src_map = _segment_source_map(cfg, rel)
    total = 0
    for sid in sids:
        src = src_map.get(str(sid), "")
        cur = (segs.get(str(sid), {}) or {}).get("translation") or ""
        if not src or not cur:
            continue
        if dry_run:
            total += len(phrases_mod.segment_removals(cfg, src, cur, ""))
        else:
            total += phrases_mod.purge_for_segment(cfg, src, cur, "")
    return total


def cmd_reset(cfg: Config, args) -> None:
    """重置指定页面或段，使下次 translate 重新翻译。

    - 整页重置：segments 清空、status=pending、删除段缓存（全新翻译）
    - 指定段重置：仅该段 translation=None，保留段结构（只重译该段）

    同时清理对应范围的翻译记忆（TM）与翻译笔记（notes），避免重译时旧译文/旧
    说明经检索注入形成自我锚定；`--keep-tm` / `--keep-notes` 可分别关闭。
    """
    from . import notes as notes_mod
    from . import tm as tm_mod

    state = tr.State(cfg)

    if args.all:
        pages = list(state.data.get("pages", {}).keys())
    else:
        pages = args.pages
    if not pages:
        print("未指定页面")
        return

    keep_tm = getattr(args, "keep_tm", False)
    keep_notes = getattr(args, "keep_notes", False)

    # 逐页解析目标段，并预览将清理的 TM/notes 条数
    plans = []
    print(f"将重置 {len(pages)} 页：")
    for rel in pages:
        pstate = state.page(rel)
        segs = pstate.get("segments", {})
        if args.all_segments:
            target = list(segs.keys())
        elif args.segments:
            target = [str(s) for s in args.segments]
        else:
            target = None  # 整页

        if target is None:
            n_tm = 0 if keep_tm else tm_mod.purge_page(cfg, rel, dry_run=True)
            n_notes = 0 if keep_notes else notes_mod.purge_page(cfg, rel, dry_run=True)
            n_ph = _purge_phrases_for_segments(cfg, rel, list(segs.keys()), segs,
                                               dry_run=True)
            desc = "（整页）"
        else:
            n_tm = 0 if keep_tm else tm_mod.purge_segments(cfg, rel, target, dry_run=True)
            n_notes = 0 if keep_notes else notes_mod.purge_segments(cfg, rel, target, dry_run=True)
            n_ph = _purge_phrases_for_segments(cfg, rel, target, segs, dry_run=True)
            desc = f" 段 {target}"
        tm_txt = "保留" if keep_tm else f"{n_tm} 条"
        notes_txt = "保留" if keep_notes else f"{n_notes} 条"
        print(f"  {rel}{desc}  翻译记忆 {tm_txt}、翻译笔记 {notes_txt}、短语记忆 {n_ph} 条")
        plans.append((rel, target, n_tm, n_notes, n_ph))

    if not args.yes:
        if input(f"确认重置 {len(pages)} 页？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return

    reset_op = hist_mod.new_op_id("reset")
    total_tm = total_notes = total_ph = 0
    for rel, target, _n_tm, _n_notes, _n_ph in plans:
        pstate = state.page(rel)
        segs = pstate.get("segments", {})

        # 清理 TM / notes / 短语记忆（预览已算过条数，实际执行）
        if target is None:
            if not keep_tm:
                total_tm += tm_mod.purge_page(cfg, rel)
            if not keep_notes:
                total_notes += notes_mod.purge_page(cfg, rel)
            total_ph += _purge_phrases_for_segments(cfg, rel, list(segs.keys()), segs)
        else:
            if not keep_tm:
                total_tm += tm_mod.purge_segments(cfg, rel, target)
            if not keep_notes:
                total_notes += notes_mod.purge_segments(cfg, rel, target)
            total_ph += _purge_phrases_for_segments(cfg, rel, target, segs)

        # 覆盖前先提交各段当前版本（提交即版本，供回滚）
        if target is None:
            for sid in list(segs.keys()):
                hist_mod.commit(cfg, rel, sid, "reset", reset_op, state=state)
        else:
            for sid in target:
                if sid in segs:
                    hist_mod.commit(cfg, rel, sid, "reset", reset_op, state=state)

        if target is None:
            # 方式 A：整页重置
            pstate["status"] = "pending"
            pstate["segments"] = {}
        else:
            # 方式 B：只重置指定段（保留段结构）
            for sid in target:
                if sid in segs:
                    segs[sid]["translation"] = None
                    segs[sid]["needs_human"] = False
                    segs[sid]["untrusted"] = False
            if pstate.get("status") in (tr.STATUS["done"], tr.STATUS["review"]):
                pstate["status"] = "pending"

        # 从 done_pages 移除
        if rel in state.data.get("done_pages", []):
            state.data["done_pages"].remove(rel)

        # 方式 A 删除段缓存（重新切分）
        if target is None:
            seg_path = os.path.join(cfg.get("segments_dir", default=""),
                                    rel.replace("/", "__") + ".json")
            if os.path.exists(seg_path):
                os.remove(seg_path)

        desc = f" 段 {target}" if target else "（整页）"
        print(f"  已重置: {rel}{desc}")

    state.save()
    if total_tm or total_notes or total_ph:
        print(f"已清理翻译记忆: {total_tm} 条 / 翻译笔记: {total_notes} 条 / "
              f"短语记忆: {total_ph} 条")
    print(f"\n重置完成，下次 translate 将重译指定内容")


def cmd_add_term(cfg: Config, args) -> None:
    """向词汇表添加条目，并清理对应的短语记忆/翻译记忆/笔记。"""
    if args.file:
        # 批量导入：从 JSON 文件读取条目列表
        entries = util.read_json(args.file, [])
        for e in entries:
            src = e.get("src", "")
            dst = e.get("dst", "")
            if src and dst:
                gl.add_term(cfg, src, dst,
                            category=e.get("category", "term"),
                            note=e.get("note", ""),
                            author="user",
                            purge_keywords=e.get("purge_keywords"))
        print(f"\n批量导入完成: {len(entries)} 条")
    elif args.src and args.dst:
        gl.add_term(cfg, args.src, args.dst,
                    category=args.category or "term",
                    note=args.note or "",
                    author="user",
                    purge_keywords=getattr(args, "purge_keyword", None))
    else:
        print("请指定 src 和 dst，或使用 --file 批量导入")


def rebuild_translation(source: str, translation: str, lookup: dict) -> tuple[str, bool]:
    """基于词汇表/短语记忆重建段译文。

    按占位符拆分原文和译文，逐块处理：
    - 原文文本块精确匹配词汇表 → 用 dst + 原文空白重建
    - 原文文本块不匹配 → 照抄译文对应块

    返回 (新译文, 是否有变更)。
    """
    import re as _re

    # 拆分原文和译文为 token 序列
    src_tokens = _re.split(r'(\[\[P\d+\]\])', source)
    tr_tokens = _re.split(r'(\[\[P\d+\]\])', translation)

    # 校验占位符顺序一致
    src_ph = [t for t in src_tokens if _re.match(r'\[\[P\d+\]\]', t)]
    tr_ph = [t for t in tr_tokens if _re.match(r'\[\[P\d+\]\]', t)]
    if src_ph != tr_ph:
        return translation, False  # 占位符不对齐，无法处理

    new_translation = ""
    src_i, tr_i = 0, 0

    while src_i < len(src_tokens) or tr_i < len(tr_tokens):
        s = src_tokens[src_i] if src_i < len(src_tokens) else None
        t = tr_tokens[tr_i] if tr_i < len(tr_tokens) else None

        # 占位符：两边照抄
        if s and _re.match(r'\[\[P\d+\]\]', s):
            new_translation += s
            src_i += 1
            tr_i += 1
            continue

        if s is None and t is None:
            break

        # 文本块：检查原文是否精确匹配词汇表
        if s is not None:
            s_core = s.strip()
            if s_core in lookup:
                # 命中：用 dst + 原文空白重建
                s_lead_ws = s[:len(s) - len(s.lstrip())]
                s_trail_ws = s[len(s.rstrip()):]
                new_translation += s_lead_ws + lookup[s_core] + s_trail_ws
                src_i += 1
                tr_i += 1
                continue

        # 未命中：照抄译文对应块
        if t is not None:
            new_translation += t
            tr_i += 1
        # 原文块推进
        src_i += 1

    changed = new_translation != translation
    return new_translation, changed


def cmd_audit_terms(cfg: Config, args) -> None:
    """审计已翻译段落，用新词汇表/短语记忆替换精确匹配的部分。"""
    state = tr.State(cfg)
    pages = args.pages or [rel for rel, p in state.data.get("pages", {}).items()
                           if p.get("status") in ("done", "review")]

    # 构建查找表 src -> dst
    lookup = {}
    for e in gl.load(cfg):
        if e.get("read_only"):
            lookup[e["src"]] = e["dst"]
    from . import phrases as phrases_mod
    for ph_src, ph_info in phrases_mod.load(cfg).items():
        if ph_src not in lookup:
            lookup[ph_src] = ph_info["dst"]

    if not lookup:
        print("词汇表和短语记忆为空，无需审计")
        return

    audit_op = hist_mod.new_op_id("audit")
    updated_count = 0
    updated_pages = set()

    for rel in pages:
        pstate = state.page(rel)
        segs = pstate.get("segments", {})

        # 读取段缓存（获取源文本 + 同步更新译文）
        seg_cache_path = os.path.join(cfg.get("segments_dir", default=""),
                                      rel.replace("/", "__") + ".json")
        raw_cache = util.read_json(seg_cache_path, []) if os.path.exists(seg_cache_path) else []
        seg_cache = raw_cache.get("segments", raw_cache) if isinstance(raw_cache, dict) else raw_cache
        # 构建 seg_id -> source text 映射
        seg_source_map = {str(item.get("id")): item.get("text", "") for item in seg_cache}

        changed = False

        for sid, seg_data in segs.items():
            source = seg_source_map.get(sid, "")
            translation = seg_data.get("translation")
            if not source or not translation:
                continue

            new_translation, changed_seg = rebuild_translation(source, translation, lookup)
            if not changed_seg:
                continue

            if not args.dry_run:
                # 覆盖前提交当前版本（供回滚）
                hist_mod.commit(cfg, rel, sid, "audit", audit_op, state=state)
                # 更新 state.json
                seg_data["translation"] = new_translation
                # 不更新 needs_human，留给用户 review

                # 同步更新段缓存（按 id 匹配）
                for item in seg_cache:
                    if str(item.get("id")) == sid:
                        item["translation"] = new_translation
                        break

                updated_count += 1
                changed = True
                print(f"  {rel} 段{sid}: 已更新")
            else:
                updated_count += 1
                changed = True
                print(f"  {rel} 段{sid}: 将更新")

        # 写回段缓存（保留 dict 包装的 encoding 字段）
        if not args.dry_run and changed:
            util.write_json(seg_cache_path, raw_cache if isinstance(raw_cache, dict) else seg_cache)

    if not args.dry_run:
        state.save()
        if not args.no_regenerate:
            for rel in pages:
                success = review_mod._regenerate_page(cfg, rel)
                if success:
                    print(f"  已重生成: {rel}")

    print(f"\n完成: {updated_count} 个段落{'将被' if args.dry_run else '已'}更新")


def cmd_qa(cfg: Config, args) -> None:
    client = _client(cfg)
    # 覆盖 deep_llm_check 配置开关（仅内存，不持久化）
    if args.no_deep:
        cfg.set(False, "qa", "deep_llm_check")
    elif args.with_deep:
        cfg.set(True, "qa", "deep_llm_check")
    state = tr.State(cfg)
    done_pages = state.data.get("done_pages", [])
    done_set = set(done_pages)

    # 选择待检查页：--pages 显式指定 | --start/--count 按 plan.order 取区间 | 缺省全部已译页
    start = getattr(args, "start", None)
    count = getattr(args, "count", None)
    if args.pages:
        if start is not None or count is not None:
            print("错误: --pages 与 --start/--count 不能同时使用", file=sys.stderr)
            sys.exit(2)
        targets = [p for p in args.pages if p in done_set] or args.pages
        scope = "显式指定"
    elif start is not None or count is not None:
        plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
        order = plan.get("order", [])
        if not order:
            print("未找到 plan.json，先运行 `booktr plan`", file=sys.stderr)
            sys.exit(2)
        s = start if start is not None else 1
        if s < 1:
            print("错误: --start 从 1 开始", file=sys.stderr)
            sys.exit(2)
        n = count if count is not None else 1
        window = order[s - 1: s - 1 + n] if n > 0 else order[s - 1:]
        targets = [p for p in window if p in done_set]
        skipped = [p for p in window if p not in done_set]
        scope = f"plan.order[{s}..{s - 1 + len(window)}]"
        print(f"范围 {scope}：命中 {len(targets)} 页"
              + (f"，跳过未翻译 {len(skipped)} 页" if skipped else ""), flush=True)
        if not targets:
            print("该范围内没有已翻译页，结束。")
            return
    else:
        targets = done_pages
        scope = "全部已译页"

    deep = cfg.get("qa", "deep_llm_check", default=True)
    print(f"QA 开始：{scope}，共 {len(targets)} 页（深度检查={'开' if deep else '关'}）", flush=True)

    # 时间戳报告（总是留存），并刷新 qa_report.json 别名
    report_dir = cfg.get("qa", "report_dir", default="work/qa_reports")
    os.makedirs(report_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(report_dir, f"qa_{ts}.json")
    report = qa.qa_report(cfg, client, targets, out=out_path, ts=ts, scope=scope)

    print(f"QA 报告: 总问题 {report['total_issues']}（未定位 {report.get('unresolved', 0)}），"
          f"高危 {report['high']}，见 {out_path}")
    for rel, issues in report["pages"].items():
        print(f"  {rel}: {len(issues)} 问题")
        for i in issues[:3]:
            loc = f"段{i['segments']}" if i.get("segments") else "（未定位）"
            print(f"    {loc} [{i['severity']}] {i['reason']}")

    # 写入专用 QA 队列（标准化条目，机械定位段号；按 id 去重）
    from . import qa_queue as qa_queue_mod
    origin = {"ts": ts, "scope": scope,
              "start": start, "count": count}
    new_items = [qa_queue_mod.make_item(rel, i, origin)
                 for rel, issues in report["pages"].items() for i in issues]
    added = qa_queue_mod.append_items(cfg, new_items)
    print(f"已写入 QA 队列: 新增 {added} 条（work/qa_queue.json）；用 `qa-review` 裁定，"
          f"`qa-apply` 应用采纳项。")


def cmd_qa_review(cfg: Config, args) -> None:
    """交互式裁定 QA 队列条目。"""
    from . import qa_queue as qa_queue_mod
    stats = qa_queue_mod.stats(cfg)
    print(f"QA 队列：总 {stats['total']} | open {stats['open']} | 采纳 {stats['adopted']}"
          f" | 按严重度(open)={stats['by_severity']}")
    qa_queue_mod.interactive_qa_review(cfg, max_items=args.max_items)


def cmd_qa_apply(cfg: Config, args) -> None:
    """对已采纳（adopted）的 QA 意见批量定点重译。"""
    from . import qa_queue as qa_queue_mod

    client = _client(cfg)
    state = tr.State(cfg)
    # site_map/plan 缺失时用空值（QA 修正不强依赖跨页上下文）
    sm = util.read_json(os.path.join(cfg.work_dir, "site_map.json"), {})
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})

    queue = qa_queue_mod.load(cfg)
    adopted = [it for it in queue if it.get("status") == qa_queue_mod.STATUS_ADOPTED]
    if args.page:
        adopted = [it for it in adopted if it.get("page") == args.page]
    if not adopted:
        print("没有已采纳的 QA 条目（先用 `qa-review` 采纳）。")
        return

    # 按 (page, segment) 分组，合并同一段的多个意见
    grouped: dict[tuple, list[dict]] = {}
    for it in adopted:
        for sid in it.get("segments", []) or []:
            grouped.setdefault((it["page"], str(sid)), []).append(it)

    print(f"将应用 {len(adopted)} 条已采纳意见，涉及 {len(grouped)} 个段。")
    if args.dry_run:
        for (rel, sid), ops in grouped.items():
            print(f"  {rel} 段{sid}: {len(ops)} 条意见")
        return

    guide = styles_mod.load_guide(cfg) if cfg.get("style", "rules_enabled", default=True) else ""
    focus = cfg.get("translators_notes", "focus", default="")
    _, _, user_rules = tr.build_context(cfg, sm, plan, next(iter(grouped))[0]) \
        if grouped else ("", "", "")

    ok = 0
    pages_done = set()
    for (rel, sid), ops in grouped.items():
        opinions = [{"severity": o.get("severity", ""), "reason": o.get("reason", ""),
                     "src_quote": o.get("src_quote", ""), "dst_quote": o.get("dst_quote", ""),
                     "suggestion": o.get("suggestion", "")} for o in ops]
        r = tr.apply_qa_fix(cfg, client, rel, sid, opinions, state, sm, plan,
                            guide=guide, user_rules=user_rules, focus=focus)
        if r.get("ok"):
            ok += 1
            pages_done.add(rel)
            for o in ops:
                o["status"] = qa_queue_mod.STATUS_APPLIED
            print(f"  ✓ {rel} 段{sid}: 已修正（{len(ops)} 条意见）")
        else:
            print(f"  ✗ {rel} 段{sid}: 修正失败（占位符/空译文），保留待处理")

    state.save()
    qa_queue_mod.save(cfg, queue)
    print(f"\n完成: {ok}/{len(grouped)} 段已修正；涉及 {len(pages_done)} 页已重生成。")


def cmd_qa_status(cfg: Config, args) -> None:
    """聚合各页最近一次 QA 状态（只读；扫描 qa_reports/*.json）。"""
    from . import qa_queue as qa_queue_mod

    latest = qa.collect_page_status(cfg)
    plan = util.read_json(os.path.join(cfg.work_dir, "plan.json"), {})
    order = plan.get("order", [])
    if not order:
        # 无 plan 时退回所有已译页
        state = tr.State(cfg)
        order = [rel for rel, p in state.data.get("pages", {}).items()
                 if p.get("status") in (tr.STATUS["done"], tr.STATUS["review"])]

    # 队列 open 条数（按页）
    queue = qa_queue_mod.load(cfg)
    open_by_page: dict[str, int] = {}
    for it in queue:
        if it.get("status") == qa_queue_mod.STATUS_OPEN:
            open_by_page[it.get("page", "")] = open_by_page.get(it.get("page", ""), 0) + 1

    checked = [rel for rel in order if rel in latest]
    unchecked = [rel for rel in order if rel not in latest]
    print(f"QA 覆盖 {len(checked)}/{len(order)}（未 QA {len(unchecked)}）")

    def _row(rel):
        info = latest.get(rel)
        n_open = open_by_page.get(rel, 0)
        if info is None:
            return f"  {rel:36} 未QA" + (f"  open {n_open}" if n_open else "")
        ts = str(info.get("ts", "")) or "-"
        tot = info.get("total", "?")
        hi = info.get("high", "?")
        open_s = f"  open {n_open}" if n_open else ""
        return (f"  {rel:36} {ts:16} 问题 {tot}(high {hi}){open_s}")

    only_pending = getattr(args, "pending_only", False)
    only_issues = getattr(args, "issues", False)

    if only_pending:
        rows = unchecked
    elif only_issues:
        rows = [rel for rel in order
                if (latest.get(rel, {}).get("total", 0) or open_by_page.get(rel, 0))]
    else:
        rows = list(order)

    for rel in rows:
        print(_row(rel))

    if not only_pending and not only_issues:
        print(f"\n已 QA {len(checked)} 页；未 QA {len(unchecked)} 页；"
              f"有未决(open)条目的页 {len(open_by_page)} 个。")


def cmd_locate(cfg: Config, args) -> None:
    """按原文/译文片段定位页面内的翻译段落（通用查询）。"""
    if not args.src and not args.dst:
        print("请用 --src 或 --dst 指定要定位的片段。", file=sys.stderr)
        sys.exit(2)
    res = locate_mod.locate_for_page(
        cfg, args.page, src_frag=args.src or "", dst_frag=args.dst or "",
        include_untranslated=args.all, top=args.top)
    if args.json:
        print(json.dumps({"page": args.page, "results": res},
                         ensure_ascii=False, indent=2))
        return
    if not res:
        print(f"未定位到段落（{args.page}）。可尝试更长的片段或用 --all。")
        return
    print(f"页面 {args.page} 命中 {len(res)} 段：")
    for r in res:
        print(f"  段{r['sid']}  [{r['method']} {r['score']}]")
        print(f"    原文: {r['src'][:100]}")
        print(f"    译文: {r['dst'][:100]}")


def cmd_rollback(cfg: Config, args) -> None:
    """段颗粒度回滚：从历史版本恢复（默认先预览再确认）。"""
    # 维护子命令
    if args.purge:
        page = None if args.all else args.page
        keep = args.keep_last
        info = hist_mod.purge(cfg, page=page, keep_last=keep, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] 将处理 {info['files']} 个历史文件"
                  + (f"，每段保留最近 {keep} 版" if keep is not None else "（整页/全部删除）"))
        else:
            print(f"历史清理：处理 {info['files']} 文件，"
                  f"删除版本 {info['removed_versions']}，删除文件 {info['removed_files']}")
        return
    if args.backfill:
        n = hist_mod.backfill(cfg, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] 将为 {n} 段补录 v1 历史版本")
        else:
            print(f"已补录 {n} 段历史版本")
        return

    if not args.page:
        print("请用 --page 指定页面。", file=sys.stderr)
        sys.exit(2)

    # --list / --list-ops
    if args.list:
        _rollback_list(cfg, args.page, args.json)
        return
    if args.list_ops:
        for e in hist_mod.ops(cfg, args.page):
            print(f"  {e['op_id']}  ts={e['ts']}  {e['op']}  段 {e['sids']}")
        return

    # 交互模式
    if args.interactive:
        _rollback_interactive(cfg, args.page)
        return

    # 解析目标
    targets = hist_mod.resolve_targets(
        cfg, args.page, sids=args.segments,
        src_frag=args.src or "", dst_frag=args.dst or "",
        op=args.op, include_untranslated=False)
    if not targets:
        print("未找到可回滚的目标（检查 --segments/--src/--op，或用 --list）。")
        return

    # 指定版本（仅单段选择器）
    if args.version and len(targets) == 1:
        sid = str(targets[0]["sid"])
        v = _pick_version(cfg, args.page, sid, args.version)
        if v is None:
            print(f"版本不存在: {args.version}", file=sys.stderr)
            sys.exit(2)
        targets[0]["to_version"] = v["id"]

    plan = hist_mod.plan_restore(cfg, args.page, targets)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.dry_run:
            return
    else:
        print(hist_mod.format_plan(plan))
        print()

    if args.dry_run:
        print("dry-run：未做任何修改。")
        return
    if plan["totals"]["segments"] == 0:
        print("无可恢复段（全部跳过）。")
        return
    if not args.yes:
        if input(f"确认回滚 {plan['totals']['segments']} 段？[y/N] ").strip().lower() \
                not in ("y", "yes"):
            print("已取消")
            return
    res = hist_mod.apply_plan(cfg, plan)
    print(f"\n已恢复 {res['restored']} 段；"
          + ("out 已重生成。" if res["out_regenerated"] else "⚠ out 重生成失败（段缓存缺失？）。"))


def _pick_version(cfg: Config, page: str, sid: str, selector: str) -> dict | None:
    """按版本 id 或序号（1 起，负数为从末尾倒数）选取版本。"""
    vs = hist_mod.versions(cfg, page, sid)
    if not vs:
        return None
    if selector.lstrip("-").isdigit():
        i = int(selector)
        if i < 0:
            i = len(vs) + i + 1
        if 1 <= i <= len(vs):
            return vs[i - 1]
        return None
    return hist_mod.get_version(cfg, page, sid, selector)


def _rollback_list(cfg: Config, page: str, as_json: bool) -> None:
    data = hist_mod._load(cfg, page)
    segs = data.get("segments", {})
    if as_json:
        print(json.dumps(segs, ensure_ascii=False, indent=2))
        return
    if not segs:
        print(f"{page} 无历史版本。")
        return
    state_raw = util.read_json(cfg.get("state", "path", default="work/state.json"), {})
    pseg = (state_raw.get("pages", {}).get(page, {}) or {}).get("segments", {})
    for sid in sorted(segs, key=lambda s: (len(s), s)):
        cur = (pseg.get(sid, {}) or {}).get("translation")
        print(f"段{sid}  当前: {(cur or '（空/未译）')[:60]}")
        for i, v in enumerate(segs[sid], 1):
            t = (v.get("state", {}).get("translation") or "（空）")
            mark = " *" if t == cur else ""
            print(f"  [{i:>3}] {v.get('id')}  {v.get('ts')}  "
                  f"{v.get('op'):<12} {v.get('author'):<5} "
                  f"{(t or '')[:50]}{mark}")
    print("（* 为与当前一致；用 `--segments N --version <id|序号>` 恢复）")


def _rollback_interactive(cfg: Config, page: str) -> None:
    """逐段查看历史并选择恢复（n/p 翻页，r 恢复，q 退出）。"""
    data = hist_mod._load(cfg, page)
    segs = data.get("segments", {})
    if not segs:
        print(f"{page} 无历史版本。")
        return
    state_raw = util.read_json(cfg.get("state", "path", default="work/state.json"), {})
    pseg = (state_raw.get("pages", {}).get(page, {}) or {}).get("segments", {})
    sid_list = sorted(segs, key=lambda s: (len(s), s))
    for sid in sid_list:
        vs = segs[sid]
        cur = (pseg.get(sid, {}) or {}).get("translation") or ""
        print("\n" + "=" * 70)
        print(f"段{sid}  历史 {len(vs)} 版")
        print("  当前译文: " + (cur[:160] or "（空/未译）"))
        i = 0
        while True:
            v = vs[i]
            t = v.get("state", {}).get("translation") or ""
            meta = (f"  [{i + 1}/{len(vs)}] {v.get('id')} {v.get('ts')} "
                    f"{v.get('op')}/{v.get('author')}")
            print(meta)
            print("    " + hist_mod.inline_diff(cur, t))
            act = input("  [n]下一版 [p]上一版 [r]恢复到该版 [q]退出 > ").strip().lower()
            if act == "q":
                return
            if act == "n":
                i = min(i + 1, len(vs) - 1)
            elif act == "p":
                i = max(i - 1, 0)
            elif act == "r":
                targets = [{"sid": sid, "to_version": v["id"], "note": "interactive"}]
                plan = hist_mod.plan_restore(cfg, page, targets)
                res = hist_mod.apply_plan(cfg, plan)
                print(f"  已恢复 {res['restored']} 段；"
                      + ("out 已重生成。" if res["out_regenerated"] else "⚠ out 重生成失败。"))
                break


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


def cmd_fix(cfg: Config, args) -> None:
    """修复无法解码的输入文件，输出到 fix 目录（不改原始文件）。

    遍历源目录所有文件：
    - 无法严格解码的 html → 用 errors='replace' 修复为 UTF-8 写入 fix 目录
    - 其余文件：默认不复制；`--all` 时全部复制（含资源与正常 html），使 fix 目录可直接作源
    """
    import shutil

    from . import util as _util
    from .crawler import resolve_local_path
    from .segments import _ensure_charset

    out_dir = args.out or os.path.join(cfg.data_dir, "fix")
    lang = cfg.get("lang", "source", default="ja")
    html_re = re.compile(r"\.(html?|htm)$", re.IGNORECASE)

    # 遍历源目录全部文件
    files = []
    for root, _dirs, fnames in os.walk(cfg.source_dir):
        for fn in fnames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, cfg.source_dir).replace("\\", "/")
            files.append(rel)
    files.sort()

    needs_fix = []
    for rel in files:
        if not html_re.search(rel):
            continue
        raw = open(resolve_local_path(cfg, rel), "rb").read()
        try:
            _util.decode_html(raw, lang)  # 严格：能解码则无需修复
        except _util.EncodingError:
            needs_fix.append(rel)

    if args.dry_run:
        if needs_fix:
            print(f"需要修复的文件（{len(needs_fix)} 个）：")
            for rel in needs_fix:
                print(f"  - {rel}")
        else:
            print("没有需要修复的文件。")
        return

    fixed = 0
    copied = 0
    for rel in files:
        src = resolve_local_path(cfg, rel)
        dst = os.path.join(out_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst) or out_dir, exist_ok=True)
        if html_re.search(rel) and rel in needs_fix:
            # 修复：宽松解码为 UTF-8 文本并写入
            raw = open(src, "rb").read()
            text, _enc = _util.decode_html_loose(raw, lang)
            fixed_text = _ensure_charset(text)
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(fixed_text)
            fixed += 1
        elif args.all:
            # 复制原样（含正常 html 与全部资源）
            shutil.copyfile(src, dst)
            copied += 1

    print(f"修复 {fixed} 个文件 → {out_dir}")
    if args.all:
        print(f"复制其他 {copied} 个文件（fix 目录可直接作为新源）")
    else:
        print("（用 --all 可复制全部文件，使 fix 目录直接作为新源）")
    if needs_fix:
        print("请审核 fix 目录中的修复结果，确认后再手动合并回源目录（程序不修改原始文件）。")


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


def cmd_check_residual(cfg: Config, args) -> None:
    """列出已译段落中译文残留的源语言片段（当前仅日语·平假名）。

    只读：不修改状态、不触发重译。人工核验每项后，手动执行其下方给出的
    reset 命令，再 translate 即可重译该段。
    """
    src_lang = cfg.get("lang", "source", default="ja")
    if src_lang not in residual_mod.SUPPORTED_SOURCE_LANGS:
        print(f"当前源语言「{prompts.lang_name(src_lang)}」暂无残留检测规则（仅支持日语）。")
        return

    pages = args.pages or None
    results = residual_mod.scan(cfg, pages, data_dir=getattr(args, "data_dir", None))

    if not results:
        print("未发现残留假名。")
    else:
        total_items = sum(len(r["items"]) for r in results)
        print(f"发现残留假名：{total_items} 段 / {len(results)} 页\n")
        idx = 0
        for page in results:
            for it in page["items"]:
                idx += 1
                tokens = "、".join(it["tokens"])
                print(f"[{idx}] {page['page']}  段{it['segment_id']}")
                print(f"    命中: {tokens}")
                for ex in it["excerpts"]:
                    print(f"    上下文: {ex}")
            print(f"    重置: {page['reset']}")
            print()

    if not args.no_report:
        report_path = args.json or os.path.join(cfg.work_dir, "residual_report.json")
        util.write_json(report_path, {
            "source_lang": src_lang,
            "total_segments": sum(len(r["items"]) for r in results),
            "total_pages": len(results),
            "pages": results,
        })
        print(f"报告已写入: {report_path}")


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
        _CleanItem("segment_history", "段历史版本", "segment_history", "dir",
                   lambda c: _count_dir_files(c, "segment_history"), default=False),
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
        _CleanItem("logs", "导出日志", "logs", "dir",
                   lambda c: _count_dir_files(c, "logs")),
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


def _extract_src_text(log: dict, prev_msg_len: int) -> str:
    """从 messages 中提取待翻译文本。"""
    import re as _re
    messages = log.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            m = _re.search(r'### 待翻译文本\s*\n+(.*)', content, _re.S)
            if m:
                return m.group(1).strip()
            m = _re.search(r'## 待翻译文本\s*\n+(.*)', content, _re.S)
            if m:
                return m.group(1).strip()
            return content
    return ""


def _format_call_markdown(call_idx: int, log: dict, prev_msg_len: int) -> tuple[list[str], int]:
    """格式化单次调用为 Markdown 行，返回 (lines, new_msg_len)。"""
    import re as _re
    tag = log.get("tag", "")
    ts = log.get("ts", "")[:19]
    ok = log.get("ok", True)
    error = log.get("error", "")
    usage = log.get("usage", {})
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    response = log.get("response", "")

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

    # 判断是否为 Clean 模式
    is_repair = "repair" in tag
    is_clean = ok and not is_repair
    parsed = None
    # 优先用日志已存的 parsed（可能含机械修复信息），否则裸解析
    stored_parsed = log.get("parsed")
    if is_clean and (stored_parsed or response):
        if isinstance(stored_parsed, dict) and "translation" in stored_parsed:
            parsed = stored_parsed
            is_clean = True
        elif response:
            try:
                parsed = json.loads(response) if response.startswith("{") else None
                is_clean = parsed is not None and "translation" in parsed
            except (json.JSONDecodeError, ValueError):
                is_clean = False

    if is_clean and parsed:
        # ═══ Clean 模式：原文/译文 + JSON ═══
        src_text = _extract_src_text(log, prev_msg_len)
        translation = parsed.get("translation", "")
        confidence = parsed.get("confidence")
        conf_str = f" | conf={confidence:.2f}" if confidence is not None else ""
        repaired_flag = ""
        if parsed.get("repaired"):
            methods = "、".join(parsed.get("repair_methods", []) or [])
            repaired_flag = f" | ⚠ 修复（{methods}）"

        lines.append(f"### 原文{conf_str}{repaired_flag}")
        lines.append("---")
        lines.append(src_text)
        lines.append("")
        lines.append("### 译文")
        lines.append("---")
        lines.append(translation)
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        # ═══ Raw 模式：原始 prompt/response ═══
        messages = log.get("messages", [])
        new_msg_len = prev_msg_len

        if messages:
            new_msgs = messages[prev_msg_len:]
            for msg in new_msgs:
                role = msg.get("role", "")
                if role == "assistant":
                    continue
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
                resp_parsed = json.loads(response) if response.startswith("{") else None
                if resp_parsed:
                    lines.append("```json")
                    lines.append(json.dumps(resp_parsed, ensure_ascii=False, indent=2))
                    lines.append("```")
                else:
                    lines.append(response)
            except (json.JSONDecodeError, ValueError):
                lines.append(response)
            lines.append("")

    lines.append("---")
    lines.append("")
    return lines, new_msg_len if not is_clean else prev_msg_len


def _context_reason(logs: list[dict]) -> str:
    """从 context 的日志 tag 推断该对话段的切换原因。"""
    import re as _re
    for l in logs:
        tag = l.get("tag", "")
        if "summarize_conv" in tag:
            return "摘要接力后新对话"
        if "repair" in tag:
            return "repair 插入"
        if "retranslate" in tag or "is_retranslation" in str(l.get("system", "")):
            return "重译"
    return ""


def _format_assistant_content(response: str, parsed: dict | None = None) -> list[str]:
    """格式化 assistant 内容（附录用）。

    - 若原始响应非法 JSON 但可机械修复：列修复译文 + ⚠修复标注，再列原始 JSON（未修复原文）。
    - 原始响应合法 JSON：列译文 + 格式化 JSON。
    - 完全无法解析：纯文本原样展示。
    """
    import json as _json
    from . import llm as _llm
    lines = []
    fixed = None
    if parsed and isinstance(parsed, dict) and "translation" in parsed:
        fixed = parsed
    elif response:
        try:
            fixed = _llm.parse_json_response(response)  # 含机械修复
        except _llm.LLMError:
            fixed = None
    if fixed and "translation" in fixed:
        repaired = fixed.get("repaired")
        if repaired:
            methods = "、".join(fixed.get("repair_methods", []) or [])
            lines.append(f"译文（⚠ 修复 {methods}）：")
        else:
            lines.append("译文：")
        lines.append(str(fixed.get("translation", "")))
        lines.append("")
        meta = []
        if fixed.get("confidence") is not None:
            meta.append(f"confidence={fixed['confidence']}")
        if fixed.get("needs_human"):
            meta.append("needs_human")
        if meta:
            lines.append(" | ".join(meta))
            lines.append("")
    # 原始响应（完整未更改，便于检视）
    if response:
        lines.append("原始响应：")
        raw_ok = False
        try:
            raw_data = _json.loads(response) if response.strip().startswith("{") else None
            raw_ok = isinstance(raw_data, dict)
        except (ValueError, _json.JSONDecodeError):
            raw_ok = False
        if raw_ok:
            lines.append("```json")
            lines.append(_json.dumps(raw_data, ensure_ascii=False, indent=2))
            lines.append("```")
        else:
            lines.append(response)  # 非法 JSON（如未转义引号）→ 纯文本原样
    return lines


def _format_appendix(task_groups: list[list[dict]]) -> list[str]:
    """渲染全局对话序列附录（按时间顺序，含 repair/重译）。

    每个 context 取 messages 最多的 log（该 context 的最终完整对话），
    渲染其 messages 为 system/user/assistant 序列。多 context 用 --- 分隔并标注原因。
    """
    lines = []
    for task_idx, task_logs in enumerate(task_groups, 1):
        ctx_groups = _group_logs_by_context(task_logs)
        lines.append(f"### 任务 #{task_idx}")
        lines.append("")
        for ctx_idx, ctx_logs in enumerate(ctx_groups, 1):
            ctx_logs.sort(key=lambda x: x.get("ts", ""))
            ctx_id = ctx_logs[0].get("context_id", "")
            reason = _context_reason(ctx_logs)
            label = f"对话段 {ctx_idx}"
            if ctx_id:
                label += f"（{ctx_id}）"
            if reason:
                label += f" — {reason}"
            lines.append(f"#### {label}")
            lines.append("")

            # 取 messages 最多的 log（该 context 最终完整对话）
            max_log = max(ctx_logs, key=lambda x: len(x.get("messages", [])))
            messages = max_log.get("messages", [])
            if messages:
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    lines.append(f"### {role}")
                    if role == "assistant":
                        lines.extend(_format_assistant_content(content))
                    else:
                        lines.append(content)
                    lines.append("")
                # 若 messages 以 user 结尾且该 log 有 response（assistant 回复未入 messages），补上
                if messages[-1].get("role") == "user" and max_log.get("response"):
                    lines.append("### assistant")
                    lines.extend(_format_assistant_content(max_log["response"]))
                    lines.append("")
            else:
                # 旧日志回退：system/user/response
                if max_log.get("system"):
                    lines.append("### system")
                    lines.append(max_log["system"])
                    lines.append("")
                if max_log.get("user"):
                    lines.append("### user")
                    lines.append(max_log["user"])
                    lines.append("")
                if max_log.get("response"):
                    lines.append("### assistant")
                    lines.extend(_format_assistant_content(max_log["response"]))
                    lines.append("")
            lines.append("---")
            lines.append("")
    return lines


def export_page_log(cfg: Config, page: str, max_sessions: int | None = None,
                    output_path: str | None = None,
                    task_id: str | None = None,
                    no_messages: bool = False) -> str | None:
    """导出指定页面的 LLM 对话日志为 Markdown。

    Args:
        page: 页面路径
        max_sessions: 最多导出最近 N 个翻译任务（None=全部）
        output_path: 输出路径（None=自动）
        task_id: 指定 task_id 导出（支持前缀匹配，None=全部）
        no_messages: 关闭末尾的完整对话序列附录（主日志不变）

    Returns:
        输出文件路径，无日志时返回 None
    """
    all_logs = _find_page_logs(cfg, page)
    if not all_logs:
        return None

    # 按 task_id 过滤
    if task_id:
        all_logs = [l for l in all_logs if l.get("task_id", "").startswith(task_id)]

    task_groups = _group_logs_by_task(all_logs)

    # 取最近 N 个任务
    if max_sessions and len(task_groups) > max_sessions:
        task_groups = task_groups[-max_sessions:]

    # 统计机械修复的调用
    repaired_calls = sum(
        1 for l in all_logs
        if isinstance(l.get("parsed"), dict) and l["parsed"].get("repaired")
    )

    lines = [
        f"# LLM 对话日志：{page}",
        f"共 {len(task_groups)} 次翻译任务，{len(all_logs)} 条调用记录",
        "",
    ]
    if repaired_calls:
        lines.append(f"> 其中 {repaired_calls} 条调用经过机械修复（⚠ 修复），见各调用标注。")
        lines.append("")

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

    # 附录：完整对话序列（按时间顺序，含 repair/重译）
    if not no_messages:
        lines.append("")
        lines.append("---")
        lines.append("## 附录：完整对话序列（按时间顺序，含 repair/重译）")
        lines.append("")
        lines.extend(_format_appendix(task_groups))

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
                               output_path=args.output,
                               task_id=args.task,
                               no_messages=args.no_messages)
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
    sp.add_argument("--prefs", default=None, help="初始化时导入偏好文件（user_rules/glossary/style_refs）")
    sp.add_argument("--clone", default=None,
                    help="以已存在实例（其 config.json）作为默认配置层：--clone <data_dir>；"
                         "回车沿用、输入才覆盖，并继承其 glossary/style_refs")
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

    sp = mk("add-term", help="向词汇表添加条目，并清理对应短语记忆/翻译记忆/笔记")
    sp.add_argument("src", nargs="?", help="原文术语，如 HOME")
    sp.add_argument("dst", nargs="?", help="译文，如 首页")
    sp.add_argument("--category", default="term", help="类别（person/song/album/show/place/term/other）")
    sp.add_argument("--note", default="", help="备注说明")
    sp.add_argument("--file", default=None, help="从 JSON 文件批量导入条目列表")
    sp.add_argument("--purge-keyword", action="append", default=None,
                    help="已知错误译法关键词（可重复）；TM 的 dst 或 notes 的 summary 命中即清理")
    sp.set_defaults(func=cmd_add_term)

    sp = mk("audit-terms", help="审计已翻译段落，用新词汇表/短语记忆替换精确匹配的部分")
    sp.add_argument("pages", nargs="*", help="限定审计的页面")
    sp.add_argument("--all", action="store_true", help="审计所有已翻译页面")
    sp.add_argument("--dry-run", action="store_true", help="只显示不修改")
    sp.add_argument("--no-regenerate", action="store_true", help="不重新生成 out")
    sp.set_defaults(func=cmd_audit_terms)

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

    sp = mk("regenerate", help="重新生成指定页面的 out 文件（从段索引离线重组）")
    sp.add_argument("pages", nargs="*", help="页面路径，如 profile/profile.html")
    sp.add_argument("--all", action="store_true", help="重新生成所有已处理页")
    sp.set_defaults(func=cmd_regenerate)

    sp = mk("locate", help="按原文/译文片段定位页面内的翻译段落")
    sp.add_argument("--page", required=True, help="页面路径，如 today/today90.html")
    sp.add_argument("--src", default="", help="原文片段（从页面复制）")
    sp.add_argument("--dst", default="", help="译文片段（从页面复制）")
    sp.add_argument("--top", type=int, default=5, help="模糊匹配返回条数（默认 5）")
    sp.add_argument("--all", action="store_true", help="纳入未翻译段")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(func=cmd_locate)

    sp = mk("rollback", help="段颗粒度回滚（从历史版本恢复，默认先预览再确认）")
    sp.add_argument("--page", default=None, help="页面路径")
    sp.add_argument("--segments", type=int, nargs="*", default=None, help="目标段号")
    sp.add_argument("--src", default="", help="按原文片段定位目标段")
    sp.add_argument("--dst", default="", help="按译文片段定位目标段")
    sp.add_argument("--op", default=None, help="撤销指定 op_id 一次命令的全部改动")
    sp.add_argument("--version", default=None, help="指定版本 id 或序号（仅单段）")
    sp.add_argument("--list", action="store_true", help="列出该页各段历史版本")
    sp.add_argument("--list-ops", action="store_true", help="列出该页命令调用 op_id")
    sp.add_argument("--interactive", action="store_true", help="交互浏览并恢复")
    sp.add_argument("--purge", action="store_true", help="清理历史（配 --page/--all）")
    sp.add_argument("--keep-last", type=int, default=None,
                    help="purge 时每段保留最近 N 版（缺省整页删除）")
    sp.add_argument("--all", action="store_true", help="purge 时处理全部页面")
    sp.add_argument("--backfill", action="store_true", help="为已有译文补录 v1 版本")
    sp.add_argument("--dry-run", action="store_true", help="仅预览，不做修改")
    sp.add_argument("--json", action="store_true", help="以 JSON 输出计划")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_rollback)

    sp = mk("reset", help="重置指定页面或段，使下次 translate 重新翻译（并清理对应 TM/笔记）")
    sp.add_argument("pages", nargs="*", help="页面路径，如 today/today4.html")
    sp.add_argument("--segments", type=int, nargs="*", help="只重置指定段（方式B），如 --segments 16")
    sp.add_argument("--all-segments", action="store_true", help="重置该页所有段（保留段结构）")
    sp.add_argument("--all", action="store_true", help="重置所有页面")
    sp.add_argument("--keep-tm", action="store_true", help="不清理翻译记忆（TM）")
    sp.add_argument("--keep-notes", action="store_true", help="不清理翻译笔记（notes）")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_reset)

    sp = mk("qa", help="一致性 QA pass")
    sp.add_argument("--pages", nargs="*", help="限定检查页面")
    sp.add_argument("--start", type=int, default=None,
                    help="按 plan.order 从第 N 篇（1 起）开始检查")
    sp.add_argument("--count", type=int, default=None,
                    help="与 --start 搭配：检查 N 篇（缺省 1；0 表示到末尾）")
    deep_group = sp.add_mutually_exclusive_group()
    deep_group.add_argument("--no-deep", action="store_true",
                            help="仅本地规则，跳过 LLM 深度检查（覆盖配置）")
    deep_group.add_argument("--with-deep", action="store_true",
                            help="强制启用 LLM 深度检查（覆盖配置）")
    sp.set_defaults(func=cmd_qa)

    sp = mk("qa-review", help="交互式裁定 QA 队列（采纳/拒绝/丢弃）")
    sp.add_argument("--max-items", type=int, default=0, help="最多处理条数")
    sp.set_defaults(func=cmd_qa_review)

    sp = mk("qa-apply", help="对已采纳的 QA 意见批量定点重译")
    sp.add_argument("--page", default=None, help="限定页面")
    sp.add_argument("--dry-run", action="store_true", help="仅列出将修正的段")
    sp.set_defaults(func=cmd_qa_apply)

    sp = mk("qa-status", help="聚合各页最近一次 QA 状态（只读）")
    sp.add_argument("--pending-only", action="store_true", help="只列未 QA 的页")
    sp.add_argument("--issues", action="store_true", help="只列有问题或未决条目的页")
    sp.set_defaults(func=cmd_qa_status)

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

    sp = mk("export-prefs", help="导出当前实例的个人偏好（user_rules/glossary/style_refs）")
    sp.add_argument("path", help="偏好文件路径，如 pref/booktr-prefs.json")
    sp.set_defaults(func=cmd_export_prefs)

    sp = mk("check-residual", help="列出译文残留的源语言片段（当前仅日语·平假名；只读）")
    sp.add_argument("--pages", nargs="*", help="限定检查的页面")
    sp.add_argument("--json", default=None, help="报告输出路径（默认 work/residual_report.json）")
    sp.add_argument("--no-report", action="store_true", help="不写报告文件")
    sp.set_defaults(func=cmd_check_residual)

    sp = mk("export-log", help="导出指定页面的完整 LLM 对话日志为 Markdown")
    sp.add_argument("page", help="页面路径，如 today/today6.html")
    sp.add_argument("-o", "--output", default=None, help="输出文件路径（默认 work/logs/<page>.md）")
    sp.add_argument("-s", "--sessions", type=int, default=None,
                    help="最多导出最近 N 个翻译任务（默认全部）")
    sp.add_argument("-t", "--task", default=None,
                    help="指定 task_id 导出（支持前缀匹配）")
    sp.add_argument("--no-messages", action="store_true",
                    help="关闭末尾的完整对话序列附录（主日志不变）")
    sp.set_defaults(func=cmd_export_log)

    sp = mk("fix", help="修复无法解码的输入文件（输出到 fix 目录，不改原始文件）")
    sp.add_argument("--all", action="store_true",
                    help="复制全部文件（含资源与正常 html）到 fix 目录，可直接作新源")
    sp.add_argument("--out", default=None, help="fix 输出目录（默认 <data_dir>/fix）")
    sp.add_argument("--dry-run", action="store_true", help="仅列出需修复文件，不输出")
    sp.set_defaults(func=cmd_fix)

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
