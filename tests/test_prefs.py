# -*- coding: utf-8 -*-
"""偏好文件测试：导出/导入往返、白名单、init --prefs。"""
from __future__ import annotations

import json

from booktr import glossary as gl
from booktr import prefs


def _seed(cfg):
    gl.upsert(cfg, {"src": "HOME", "dst": "首页", "category": "term", "status": "confirmed"})
    gl.upsert(cfg, {"src": "RETURN", "dst": "返回", "category": "term", "status": "confirmed"})
    cfg.set("人名保留原形", "user_rules")
    from booktr import util
    util.write_json(cfg.get("style", "refs_path", default="style_refs.json"),
                    [{"src": "a", "dst": "甲"}])


def test_collect_whitelist(tmp_cfg):
    _seed(tmp_cfg)
    data = prefs.collect(tmp_cfg)
    assert set(data.keys()) == {"version", "user_rules", "glossary", "style_refs"}
    # 不含实例专属字段
    assert "source_dir" not in data
    assert "llm" not in data
    assert "lang" not in data
    assert data["user_rules"] == "人名保留原形"
    assert len(data["glossary"]) == 2
    assert data["style_refs"] == [{"src": "a", "dst": "甲"}]


def test_export_roundtrip(tmp_cfg, tmp_path):
    _seed(tmp_cfg)
    path = str(tmp_path / "prefs" / "booktr-prefs.json")
    out = prefs.export(tmp_cfg, path)
    assert out == path
    loaded = prefs.load(path)
    assert loaded["user_rules"] == "人名保留原形"
    assert len(loaded["glossary"]) == 2
    assert loaded["style_refs"] == [{"src": "a", "dst": "甲"}]


def test_apply_data_files_writes_instance(tmp_cfg):
    prefs_data = {
        "version": 1,
        "user_rules": "保留原名",
        "glossary": [{"src": "X", "dst": "Y", "category": "term", "status": "confirmed"}],
        "style_refs": [{"src": "s", "dst": "S"}],
    }
    summary = prefs.apply_data_files(tmp_cfg, prefs_data)
    assert summary == {"glossary": 1, "style_refs": 1}
    # glossary/style_refs 落盘
    assert gl.lookup_read_only(tmp_cfg, "X") == "Y"
    from booktr import util
    assert util.read_json(tmp_cfg.get("style", "refs_path", default="style_refs.json")) == [{"src": "s", "dst": "S"}]
    # user_rules 不在 apply_data_files 中处理
    assert tmp_cfg.get("user_rules", default="") != "保留原名"


def test_export_requires_path(tmp_cfg):
    import pytest
    with pytest.raises(ValueError):
        prefs.export(tmp_cfg, "")


def test_load_missing_file(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        prefs.load(str(tmp_path / "nope.json"))


def test_cmd_init_with_prefs(tmp_cfg, tmp_path, monkeypatch):
    """init --prefs 后 config 与 glossary 生效。"""
    from booktr import pipeline

    # 准备偏好文件
    pref_path = tmp_path / "booktr-prefs.json"
    pref_path.write_text(json.dumps({
        "version": 1,
        "user_rules": "偏好规则",
        "glossary": [{"src": "Z", "dst": "Zed", "category": "term", "status": "confirmed"}],
        "style_refs": [{"src": "s", "dst": "S"}],
    }, ensure_ascii=False), encoding="utf-8")

    # 空数据根
    data_dir = tmp_path / "inst"
    from booktr.config import Config, find_project_root
    cfg = Config(root=find_project_root(), data={}, data_dir=str(data_dir))

    answers = iter(["" for _ in range(40)])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers, ""))

    class Args:
        force = False
        prefs = str(pref_path)

    pipeline.cmd_init(cfg, Args())

    saved = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["user_rules"] == "偏好规则"
    gls = json.loads((data_dir / "work" / "glossary.json").read_text(encoding="utf-8"))
    assert any(it["src"] == "Z" for it in gls)
    refs = json.loads((data_dir / "style_refs.json").read_text(encoding="utf-8"))
    assert refs == [{"src": "s", "dst": "S"}]


def test_cmd_init_without_prefs(tmp_cfg, tmp_path, monkeypatch):
    """无 --prefs 时不导入 glossary（新实例无 glossary 文件）。"""
    from booktr import pipeline
    data_dir = tmp_path / "inst2"
    from booktr.config import Config, find_project_root
    cfg = Config(root=find_project_root(), data={}, data_dir=str(data_dir))

    answers = iter(["" for _ in range(40)])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers, ""))

    class Args:
        force = False
        prefs = None

    pipeline.cmd_init(cfg, Args())
    assert not (data_dir / "work" / "glossary.json").exists()


def _run_init(tmp_path, monkeypatch, answers, prefs_path, clone=None):
    from booktr import pipeline
    from booktr.config import Config, find_project_root
    data_dir = tmp_path / ("inst_" + str(abs(hash(str(answers) + str(prefs_path) + str(clone)))))
    cfg = Config(root=find_project_root(), data={}, data_dir=str(data_dir))
    it = iter(answers + [""] * 40)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it, ""))

    clone_val = str(clone) if clone else None

    class Args:
        force = False
        prefs = str(prefs_path) if prefs_path else None
        clone = clone_val

    pipeline.cmd_init(cfg, Args())
    return json.loads((data_dir / "config.json").read_text(encoding="utf-8"))


def _make_clone_source(root, monkeypatch, key_mode="plain", user_rules="基础规则CLONE"):
    """构造一个 --clone 源实例（完整 config + glossary + style_refs）。

    key_mode: plain（明文 SECRET）/ env（BOOKTR_API_KEY 环境变量）。
    """
    clone = root / "cloneinst"
    (clone / "work").mkdir(parents=True, exist_ok=True)
    if key_mode == "env":
        llm = {"provider": "openai-compatible",
               "base_url": "https://ollama.com/v1",
               "model": "deepseek-v4.1-flash:cloud",
               "api_key": "", "api_key_env": "CLONE_KEY_ENV",
               "api_key_required": True}
    else:
        llm = {"provider": "openai-compatible",
               "base_url": "https://ollama.com/v1",
               "model": "deepseek-v4.1-flash:cloud",
               "api_key": "SECRET", "api_key_required": True}
    cfg = {
        "source_dir": "../../love.life.coocan.jp",
        "output_dir": "out", "work_dir": "work",
        "lang": {"source": "ja", "target": "zh-Hans"},
        "llm": llm,
        "user_rules": user_rules,
        "qa": {"deep_llm_check": True},
        "glossary": {"path": "work/glossary.json"},
    }
    (clone / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    (clone / "work" / "glossary.json").write_text(json.dumps(
        [{"src": "Z", "dst": "Zed", "category": "term", "status": "confirmed"}],
        ensure_ascii=False), encoding="utf-8")
    (clone / "style_refs.json").write_text(json.dumps([{"src": "s", "dst": "S"}]),
                                           encoding="utf-8")
    return clone


def test_init_clone_inherits_config_and_data(tmp_path, monkeypatch):
    """--clone：配置作为默认层（model/lang/路径）、继承 glossary/style_refs、复制 api_key。"""
    clone = _make_clone_source(tmp_path, monkeypatch)
    # 源目录/输出/工作/源语言/目标语言/风格(2=上海话)/provider(回车=openai-compatible)
    # base_url/model 回车 + API key 回车（保留）、N1..N3、其余开关回车
    answers = ["", "", "", "", "", "4", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    # 继承自 clone
    assert saved["llm"]["model"] == "deepseek-v4.1-flash:cloud"
    assert saved["llm"]["base_url"] == "https://ollama.com/v1"
    assert saved["llm"]["api_key"] == "SECRET"  # 回车保留
    assert saved["lang"]["target"] == "zh-Hans"
    assert saved["qa"]["deep_llm_check"] is True
    # user_rules = clone 基础 + 上海话块
    assert "基础规则CLONE" in saved["user_rules"]
    assert saved["user_rules"].count("## 翻译风格：上海话") == 1
    # glossary 继承
    inst = clone.parent / ("inst_" + str(abs(hash(str(answers) + str(None) + str(clone)))))
    gls = json.loads((inst / "work" / "glossary.json").read_text(encoding="utf-8"))
    assert any(it["src"] == "Z" for it in gls)


def test_init_clone_source_dir_recomputed(tmp_path, monkeypatch):
    """--clone：仅 source_dir 按新数据根重算（同深度=不变），output/work 保持。"""
    clone = _make_clone_source(tmp_path, monkeypatch)
    answers = ["", "", "", "", "", "1", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    # tmp_path 下 clone 与 inst_* 同深度 → 相对路径不变
    assert saved["source_dir"] == "../../love.life.coocan.jp"
    assert saved["output_dir"] == "out"
    assert saved["work_dir"] == "work"


def test_init_clone_with_prefs_overrides_glossary(tmp_path, monkeypatch):
    """--clone + --prefs：prefs 覆盖继承的 glossary。"""
    clone = _make_clone_source(tmp_path, monkeypatch)
    pref = tmp_path / "ov.json"
    pref.write_text(json.dumps({
        "version": 1, "user_rules": "覆盖规则",
        "glossary": [{"src": "P", "dst": "Pee", "category": "term", "status": "confirmed"}],
        "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    answers = ["", "", "", "", "", "1", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, pref, clone=clone)
    assert saved["user_rules"] == "覆盖规则"  # prefs 的 rules 作为风格基础
    inst = clone.parent / ("inst_" + str(abs(hash(str(answers) + str(pref) + str(clone)))))
    gls = json.loads((inst / "work" / "glossary.json").read_text(encoding="utf-8"))
    assert [it["src"] for it in gls] == ["P"]  # 已被 prefs 覆盖，不含 clone 的 Z


def test_init_prefs_then_style_appends(tmp_path, monkeypatch):
    """init --prefs(基础 rules) + 上海话 → pref 基础 + 方言块（不被覆盖）。"""
    pref = tmp_path / "p.json"
    pref.write_text(json.dumps({
        "version": 1, "user_rules": "偏好基础规则", "glossary": [], "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    # 输入顺序：源目录/输出/工作/源语言/目标语言/风格(4=上海话)/provider(1=mock)... 
    answers = ["", "", "", "", "", "4", "1"]
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert "偏好基础规则" in saved["user_rules"]
    assert "## 翻译风格：上海话" in saved["user_rules"]
    assert saved["user_rules"].count("## 翻译风格：上海话") == 1
    # 模板默认 D 版规则不应出现（被 pref 基础规则取代）
    assert "避免仅保留日文原形" not in saved["user_rules"]


def test_init_prefs_with_dialect_replaced(tmp_path, monkeypatch):
    """init --prefs(已含方言) + 上海话 → 旧自定义块被规范块取代。"""
    pref = tmp_path / "p2.json"
    pref.write_text(json.dumps({
        "version": 1,
        "user_rules": "基础规则\n\n## 翻译风格：上海话\n- 旧方言块",
        "glossary": [], "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    answers = ["", "", "", "", "", "4", "1"]
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert saved["user_rules"].count("## 翻译风格：上海话") == 1
    assert "旧方言块" not in saved["user_rules"]
    assert "基础规则" in saved["user_rules"]


def test_init_prefs_standard_strips_rules(tmp_path, monkeypatch):
    """init --prefs(含方言) + 标准 → 剥离风格块、保留基础规则。"""
    pref = tmp_path / "p3.json"
    rules = "基础规则\n\n## 翻译风格：上海话\n- 旧方言块"
    pref.write_text(json.dumps({
        "version": 1, "user_rules": rules, "glossary": [], "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    answers = ["", "", "", "", "", "1", "1"]  # 1 = 标准
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert saved["user_rules"] == "基础规则"


def test_init_clone_keep_style_default(tmp_path, monkeypatch):
    """--clone 源含风格块 → 默认「保留」，原样沿用（不剥离、不追加）。"""
    clone = _make_clone_source(
        tmp_path, monkeypatch,
        user_rules="基础规则CLONE\n\n## 翻译风格：流畅优先\n- 旧流畅块")
    answers = ["" for _ in range(16)]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    assert "## 翻译风格：流畅优先" in saved["user_rules"]
    assert "旧流畅块" in saved["user_rules"]


def test_init_clone_switch_style_strips_old(tmp_path, monkeypatch):
    """--clone 源含流畅块 + 选忠实 → 旧块剥离，仅余忠实块。"""
    clone = _make_clone_source(
        tmp_path, monkeypatch,
        user_rules="基础规则CLONE\n\n## 翻译风格：流畅优先\n- 旧流畅块")
    answers = ["", "", "", "", "", "2", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    assert "翻译风格：忠实优先" in saved["user_rules"]
    assert "翻译风格：流畅优先" not in saved["user_rules"]


# ── API key 提供方式（init 三选一）────────────────────────────────────
def test_init_key_mode_plain_default_blank(tmp_path, monkeypatch):
    """直接 init：模式默认「明文」，明文留空 → 无需 key。"""
    # 顺序：源/输出/工作/源语言/目标语言/风格(1)/provider(2=openai-compatible)
    #       base_url/model + 模式(回车=plain) + key(留空)
    answers = ["", "", "", "", "", "1", "2", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None)
    assert saved["llm"]["api_key"] == ""
    assert saved["llm"]["api_key_required"] is False


def test_init_key_mode_plain_writes_key(tmp_path, monkeypatch):
    """直接 init：明文模式输入 key → 写入 config 且 required=true。"""
    answers = ["", "", "", "", "", "1", "2", "", "", "", "MYKEY", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None)
    assert saved["llm"]["api_key"] == "MYKEY"
    assert saved["llm"]["api_key_required"] is True


def test_init_key_mode_env_default_name(tmp_path, monkeypatch):
    """直接 init：env 模式 + 名留空 → 默认 BOOKTR_API_KEY。"""
    # 模式选 2（环境变量） + 名留空
    answers = ["", "", "", "", "", "1", "2", "", "", "2", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None)
    assert saved["llm"]["api_key"] == ""
    assert saved["llm"]["api_key_env"] == "BOOKTR_API_KEY"
    assert saved["llm"]["api_key_required"] is True


def test_init_key_mode_env_custom_name(tmp_path, monkeypatch):
    """直接 init：env 模式 + 自定义名。"""
    answers = ["", "", "", "", "", "1", "2", "", "", "2", "MY_KEY_ENV", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None)
    assert saved["llm"]["api_key_env"] == "MY_KEY_ENV"
    assert saved["llm"]["api_key_required"] is True


def test_init_key_mode_none(tmp_path, monkeypatch):
    """直接 init：无需 key 模式 → required=false。"""
    answers = ["", "", "", "", "", "1", "2", "", "", "3", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None)
    assert saved["llm"]["api_key"] == ""
    assert saved["llm"]["api_key_required"] is False


def test_init_clone_key_mode_mirrors_env_source(tmp_path, monkeypatch):
    """--clone 源为 env → 模式回车默认 env，并继承源变量名。"""
    clone = _make_clone_source(tmp_path, monkeypatch, key_mode="env")
    # 源/输出/工作/源语言/目标语言/风格(1)/provider(回车=openai-compatible)
    # base_url/model 回车 + 模式(回车=env) + env 名(回车=继承 CLONE_KEY_ENV)
    answers = ["", "", "", "", "", "1", "", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    assert saved["llm"]["api_key"] == ""
    assert saved["llm"]["api_key_env"] == "CLONE_KEY_ENV"
    assert saved["llm"]["api_key_required"] is True


def test_init_clone_key_mode_mirrors_plain_source(tmp_path, monkeypatch):
    """--clone 源为明文 → 模式回车默认明文，回车保留源 key。"""
    clone = _make_clone_source(tmp_path, monkeypatch, key_mode="plain")
    answers = ["", "", "", "", "", "1", "", "", "", "", "", "", "", "", "", "", ""]
    saved = _run_init(tmp_path, monkeypatch, answers, None, clone=clone)
    assert saved["llm"]["api_key"] == "SECRET"
    assert saved["llm"]["api_key_required"] is True


