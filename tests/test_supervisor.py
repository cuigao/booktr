"""监督判官（supervisor）与 qa-auto 闭环测试。"""
from __future__ import annotations

import json
import os

from booktr import llm as llm_mod
from booktr import qa_queue as qq
from booktr import supervisor as sup
from booktr import translate as tr
from booktr.config import Config
from conftest import FakeLLM, write_sample_site


# --------------------------------------------------------------------------
# parse_verdict / apply_verdicts
# --------------------------------------------------------------------------
def test_parse_verdict_valid():
    assert sup.parse_verdict('{"verdict":"adopt","reason":"r","suggestion":"s"}')["verdict"] == "adopt"
    assert sup.parse_verdict('{"verdict":"REJECT"}')["verdict"] == "reject"
    assert sup.parse_verdict('{"verdict":"skip"}')["verdict"] == "skip"


def test_parse_verdict_invalid_to_skip():
    assert sup.parse_verdict("not json")["verdict"] == "skip"
    assert sup.parse_verdict('{"verdict":"whatever"}')["verdict"] == "skip"


def test_parse_verdict_unescaped_quotes_in_suggestion():
    # suggestion 内裸引号（判官常见）应经值字符串转义修复后成功解析
    raw = ('{"verdict": "adopt", "reason": "r", '
           '"suggestion": "散发出"紧张"的气场"}')
    v = sup.parse_verdict(raw)
    assert v["verdict"] == "adopt"
    assert "紧张" in v["suggestion"]


def test_apply_verdicts_keeps_original_suggestion():
    it = {"id": "x", "reason": "r", "suggestion": "orig", "src_quote": "", "dst_quote": ""}
    st = sup.apply_verdicts([it], [{"verdict": "adopt", "reason": "", "suggestion": "orig"}])
    assert it["status"] == "adopted"
    assert it["suggestion"] == "orig"
    assert "llm_suggestion" not in it
    assert st["adopt"] == 1


def test_apply_verdicts_overrides_and_archives():
    it = {"id": "x", "reason": "r0", "suggestion": "orig", "src_quote": "", "dst_quote": ""}
    sup.apply_verdicts([it], [{"verdict": "adopt", "reason": "better",
                               "suggestion": "new"}])
    assert it["status"] == "adopted"
    assert it["suggestion"] == "new"
    assert it["llm_suggestion"] == "orig"
    assert it["llm_reason"] == "r0"
    assert it["source"] == "supervisor"
    assert it["reason"] == "better"


def test_apply_verdicts_reject_skip():
    a = {"id": "a", "suggestion": "", "reason": ""}
    b = {"id": "b", "suggestion": "", "reason": ""}
    st = sup.apply_verdicts([a, b], [{"verdict": "reject"}, {"verdict": "skip"}])
    assert a["status"] == "rejected"
    assert "status" not in b  # skip 不动
    assert st == {"adopt": 0, "reject": 1, "skip": 1}


# --------------------------------------------------------------------------
# build_context / adjudicate_page
# --------------------------------------------------------------------------
def test_build_context_fields(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 摘要目录
    sdir = tmp_cfg.get("summaries", "dir", default="")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "other__page.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": "其他页摘要"}, f, ensure_ascii=False)
    ctx = sup.build_context(tmp_cfg, "page1.html")
    assert "page_ctx" in ctx and isinstance(ctx["summaries"], dict)
    assert ctx["summaries"].get("other/page") == "其他页摘要"
    assert isinstance(ctx["glossary_lines"], list)


def test_adjudicate_page_multiturn(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    items = [
        {"id": "1", "segments": [1], "severity": "low",
         "reason": "r1", "src_quote": "a", "dst_quote": "b", "suggestion": "s1"},
        {"id": "2", "segments": [2], "severity": "low",
         "reason": "r2", "src_quote": "c", "dst_quote": "d", "suggestion": "s2"},
    ]
    fake = FakeLLM(sequence=['{"verdict":"adopt","reason":"ok","suggestion":"s1"}',
                             '{"verdict":"reject","reason":"no"}'])
    verdicts, turns = sup.adjudicate_page(tmp_cfg, fake, "page1.html", items)
    assert [v["verdict"] for v in verdicts] == ["adopt", "reject"]
    assert len(turns) == 2
    assert fake.calls == 2  # 多轮：每条一次调用


class _Recorder:
    """记录每次 chat_multi 的 messages；按 responder(messages) 返回或抛错。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[list] = []

    def chat_multi(self, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        return self.responder(messages)


def _items(n):
    return [{"id": str(i), "segments": [i], "severity": "low",
             "reason": f"r{i}", "src_quote": f"src{i}", "dst_quote": f"dst{i}",
             "suggestion": f"sug{i}"} for i in range(1, n + 1)]


def test_adjudicate_retry_does_not_reuse_seed_first_prompt(tmp_cfg, tmp_path):
    """单条失败后，下一条的 ask 不得再含 seed 的"第 1 条"指令，且须指向正确条目。"""
    write_sample_site(tmp_path)
    items = _items(3)
    seq = {"n": 0}

    def responder(messages):
        seq["n"] += 1
        if seq["n"] == 2:  # 第 2 条失败
            raise llm_mod.LLMError("boom")
        # 回显当前所问序号（第 1 次→1，第 3 次→3）
        want = 1 if seq["n"] == 1 else 3
        return '{"index": %d, "verdict": "reject", "reason": "ok"}' % want

    rec = _Recorder(responder)
    verdicts, _ = sup.adjudicate_page(tmp_cfg, rec, "page1.html", items)
    assert verdicts[0]["verdict"] == "reject"
    assert verdicts[1]["verdict"] == "skip"   # 第 2 条失败
    assert verdicts[2]["verdict"] == "reject"
    # 第 3 次调用（最后一条）的 user 应指向第 3 条，且不含 seed 的"只裁决第 1 条"
    last = rec.calls[-1]
    last_user = last[-1]["content"]
    assert "现在裁决第 3/3 条" in last_user
    assert "只裁决第 1 条" not in last_user
    # seed（含全量列表）只作为历史出现一次，绝不在最后 user 里与第 3 条混合
    assert sum(1 for m in last if "只裁决第 1 条" in m["content"]) <= 1


def test_adjudicate_index_echo_mismatch_skips(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    items = _items(2)
    fake = FakeLLM(sequence=[
        '{"index": 1, "verdict": "adopt", "reason": "ok", "suggestion": "x"}',
        '{"index": 1, "verdict": "adopt", "reason": "wrong", "suggestion": "y"}',
    ])
    verdicts, _ = sup.adjudicate_page(tmp_cfg, fake, "page1.html", items)
    assert verdicts[0]["verdict"] == "adopt"
    assert verdicts[1]["verdict"] == "skip"   # 索引回显不符


def test_relevance_guard_detects_drift():
    items = [
        {"reason": "AAA BBB 漏译", "suggestion": "补译 AAA 内容",
         "src_quote": "AAA", "dst_quote": "AAA"},
        {"reason": "CCC DDD 用词不当", "suggestion": "改为 CCC 说法",
         "src_quote": "CCC", "dst_quote": "CCC"},
    ]
    # 响应明显是第 1 条的内容 → 判给第 2 条时应被守门拦下
    assert sup.relevance_ok("补译 AAA 内容", 1, items) is False
    assert sup.relevance_ok("改为 CCC 说法", 1, items) is True
    # 越界不阻断
    assert sup.relevance_ok("anything", 9, items) is True


def test_adjudicate_relevance_guard_skips(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    items = [
        {"id": "1", "segments": [1], "severity": "high", "reason": "AAA漏译",
         "src_quote": "AAA", "dst_quote": "AAA", "suggestion": "补译AAA"},
        {"id": "2", "segments": [2], "severity": "low", "reason": "BBB用词",
         "src_quote": "BBB", "dst_quote": "BBB", "suggestion": "改BBB"},
    ]
    # 第 2 问却返回第 1 条的内容，且无 index 字段
    fake = FakeLLM(sequence=[
        '{"index": 1, "verdict": "adopt", "reason": "ok", "suggestion": "补译AAA"}',
        '{"verdict": "adopt", "reason": "AAA漏译", "suggestion": "补译AAA内容"}',
    ])
    verdicts, _ = sup.adjudicate_page(tmp_cfg, fake, "page1.html", items)
    assert verdicts[1]["verdict"] == "skip"


def test_supervisor_max_tokens_override(tmp_cfg):
    cfg = Config(root=tmp_cfg.root, data={
        "llm": {"provider": "openai-compatible", "model": "m", "max_tokens": 4096},
        "qa": {"supervisor": {"max_tokens": 131072}},
    }, data_dir=tmp_cfg.data_dir)
    from booktr import llm as llm_mod2
    cli = llm_mod2.LLMClient(cfg, llm_override=cfg.get("qa", "supervisor", default={}))
    assert cli.max_tokens == 131072


def test_adjudicate_page_network_error_skips(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)

    class BoomLLM:
        calls = 0

        def chat_multi(self, messages, **kw):
            self.calls += 1
            raise llm_mod.LLMError("boom")

    items = [{"id": "1", "segments": [1], "severity": "low",
              "reason": "r", "src_quote": "a", "dst_quote": "b", "suggestion": "s"}]
    verdicts, turns = sup.adjudicate_page(tmp_cfg, BoomLLM(), "page1.html", items)
    assert verdicts[0]["verdict"] == "skip"
    assert "网络错误" in verdicts[0]["reason"]


# --------------------------------------------------------------------------
# LLMClient override
# --------------------------------------------------------------------------
def test_llm_client_override(tmp_cfg):
    cfg = Config(root=tmp_cfg.root, data={
        "llm": {"provider": "openai-compatible", "model": "base-model",
                "base_url": "https://base/v1", "api_key": "K", "max_retries": 3},
    }, data_dir=tmp_cfg.data_dir)
    base = llm_mod.LLMClient(cfg)
    assert base.model == "base-model"
    over = llm_mod.LLMClient(cfg, llm_override={
        "model": "judge-model", "base_url": "", "temperature": 0.1,
        "max_retries": 1, "api_key_required": None})
    assert over.model == "judge-model"
    assert over.base_url == "https://base/v1"  # 空串继承
    assert over.max_retries == 1
    assert over.temperature == 0.1
    assert over.api_key == "K"  # 继承
    assert over.api_key_required is True  # None 继承主配置


# --------------------------------------------------------------------------
# 端到端：qa-auto（dry-run 不改队列；闭环 apply）
# --------------------------------------------------------------------------
def _seed_translated(tmp_cfg, tmp_path):
    """翻译样例页，写入 state/段缓存/out，供 qa-auto 使用。"""
    write_sample_site(tmp_path)
    fake = FakeLLM.default()
    st = tr.State(tmp_cfg)
    sm = {}
    plan = {"order": ["page1.html"]}
    tr.translate_page(tmp_cfg, fake, "page1.html", st, sm, plan, [])
    st.save()
    return st


def test_qa_auto_dry_run_no_queue_change(tmp_cfg, tmp_path, capsys):
    from booktr import pipeline
    _seed_translated(tmp_cfg, tmp_path)
    cfg = tmp_cfg
    cfg.set(True, "qa", "deep_llm_check")
    cfg.set("mock", "qa", "supervisor", "provider")

    args = type("A", (), dict(pages=["page1.html"], start=None, count=None,
                              adjudicate_only=False, no_apply=False,
                              dry_run=True, max_items=0))()
    pipeline.cmd_qa_auto(cfg, args)
    assert qq.load(cfg) == []  # dry-run 不写队列


def test_qa_auto_closed_loop_applies(tmp_cfg, tmp_path, monkeypatch):
    from booktr import pipeline
    st = _seed_translated(tmp_cfg, tmp_path)
    cfg = tmp_cfg
    cfg.set("mock", "qa", "supervisor", "provider")

    # 预置一条 open 且已定位、且裁决将采纳的条目
    item = qq.make_item("page1.html", {
        "severity": "low", "reason": "生硬", "src_quote": "こんにちは",
        "dst_quote": "こんにちは", "suggestion": "你好", "segments": [1],
        "resolved": True})
    qq.append_items(cfg, [item])

    # 判官 mock 恒等返回：需要构造 adopt 响应
    class AdoptLLM(FakeLLM):
        def _next(self, system, user):
            return '{"verdict":"adopt","reason":"ok","suggestion":"你好"}'

    # 让 pipeline 用可控判官：monkeypatch _supervisor_client
    monkeypatch.setattr(pipeline, "_supervisor_client", lambda cfg: AdoptLLM())
    # 翻译 client 用 FakeLLM.default（apply_qa_fix 会调用它）
    monkeypatch.setattr(pipeline, "_client", lambda cfg: FakeLLM.default())

    args = type("A", (), dict(pages=["page1.html"], start=None, count=None,
                              adjudicate_only=True, no_apply=False,
                              dry_run=False, max_items=0))()
    pipeline.cmd_qa_auto(cfg, args)
    queue = qq.load(cfg)
    assert queue[0]["status"] == "applied"
    # state 已更新且与 out 同步（apply_qa_fix 会写 out）
    st2 = tr.State(cfg)
    segs = __import__("booktr.segments", fromlist=["x"]).segments_for_page(cfg, "page1.html")
    assert any(s.translation for s in segs)


def _mkargs(**kw):
    base = dict(pages=None, start=None, count=None, adjudicate_only=False,
                all=False, no_apply=False, dry_run=False, max_items=0)
    base.update(kw)
    return type("A", (), base)()


def test_qa_auto_default_scope_skips_qaed(tmp_cfg, tmp_path):
    from booktr import pipeline, qa as qa_mod
    st = _seed_translated(tmp_cfg, tmp_path)
    # 制造 page1.html 已 QA 的证据（checked）
    rep_dir = tmp_cfg.get("qa", "report_dir", default="")
    os.makedirs(rep_dir, exist_ok=True)
    with open(os.path.join(rep_dir, "qa_20260101_000000.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 2, "checked": {"page1.html": {"total": 0}}}, f)
    # done_pages 需含 page1
    st.save()
    targets, scope = pipeline._qa_auto_targets(tmp_cfg, _mkargs())
    assert "page1.html" not in targets
    assert "未 QA" in scope
    # --all 纳入
    targets_all, scope_all = pipeline._qa_auto_targets(tmp_cfg, _mkargs(all=True))
    assert "page1.html" in targets_all


def test_qa_auto_page_error_continues(tmp_cfg, tmp_path, monkeypatch):
    from booktr import pipeline, qa as qa_mod
    _seed_translated(tmp_cfg, tmp_path)

    calls = {"n": 0}

    def boom(cfg, client, rel):
        calls["n"] += 1
        raise RuntimeError("decode fail")

    monkeypatch.setattr(qa_mod, "run_qa", boom)
    monkeypatch.setattr(pipeline, "_client", lambda cfg: FakeLLM.default())
    monkeypatch.setattr(pipeline, "_supervisor_client", lambda cfg: FakeLLM.default())
    # 目标页显式指定（不依赖 done 状态）
    pipeline.cmd_qa_auto(tmp_cfg, _mkargs(pages=["page1.html"]))
    # 出错被记录到 run json，且未抛出中断
    run_dir = tmp_cfg.get("qa", "auto_log_dir", default="")
    files = [f for f in os.listdir(run_dir) if f.endswith(".json")]
    assert files, "run json should be written"
    data = json.load(open(os.path.join(run_dir, sorted(files)[-1]), encoding="utf-8"))
    assert "error" in data["pages"]["page1.html"]


def test_llm_log_toggle(tmp_cfg):
    cfg = Config(root=tmp_cfg.root, data={
        "llm": {"provider": "mock"},
        "llm_logs": {"dir": "work/llm_logs"},
    }, data_dir=tmp_cfg.data_dir)
    d = cfg.get("llm_logs", "dir", default="")
    os.makedirs(d, exist_ok=True)
    # 关闭日志：不落盘
    off = llm_mod.LLMClient(cfg, log_enabled=False)
    off.chat("sys JSON 只输出 JSON", "|TEXT|\nhi")
    assert os.listdir(d) == []
    # 打开日志：落盘
    on = llm_mod.LLMClient(cfg)
    on.chat("sys JSON 只输出 JSON", "|TEXT|\nhi")
    assert any(f.endswith(".json") for f in os.listdir(d))


def test_qa_auto_disabled(tmp_cfg, tmp_path, capsys):
    from booktr import pipeline
    cfg = tmp_cfg
    cfg.set(False, "qa", "supervisor", "enabled")
    args = type("A", (), dict(pages=["page1.html"], start=None, count=None,
                              adjudicate_only=False, no_apply=False,
                              dry_run=False, max_items=0))()
    pipeline.cmd_qa_auto(cfg, args)
    assert "未启用" in capsys.readouterr().err
