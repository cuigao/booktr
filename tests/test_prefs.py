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


def _make_clone_source(root, monkeypatch):
    """构造一个 --clone 源实例（完整 config + glossary + style_refs）。"""
    clone = root / "cloneinst"
    (clone / "work").mkdir(parents=True, exist_ok=True)
    cfg = {
        "source_dir": "../../love.life.coocan.jp",
        "output_dir": "out", "work_dir": "work",
        "lang": {"source": "ja", "target": "zh-Hans"},
        "llm": {"provider": "openai-compatible",
                "base_url": "https://ollama.com/v1",
                "model": "deepseek-v4.1-flash:cloud",
                "api_key": "SECRET", "api_key_required": True},
        "user_rules": "基础规则CLONE",
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
    answers = ["", "", "", "", "", "2", "", "", "", "", "", "", "", "", "", ""]
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
    # 输入顺序：源目录/输出/工作/源语言/目标语言/风格(2=上海话)/provider(1=mock)... 
    answers = ["", "", "", "", "", "2", "1"]
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert "偏好基础规则" in saved["user_rules"]
    assert "## 翻译风格：上海话" in saved["user_rules"]
    assert saved["user_rules"].count("## 翻译风格：上海话") == 1
    # 模板默认 D 版规则不应出现（被 pref 基础规则取代）
    assert "避免仅保留日文原形" not in saved["user_rules"]


def test_init_prefs_with_dialect_no_duplicate(tmp_path, monkeypatch):
    """init --prefs(已含方言) + 上海话 → 方言块不重复。"""
    pref = tmp_path / "p2.json"
    pref.write_text(json.dumps({
        "version": 1,
        "user_rules": "基础规则\n\n## 翻译风格：上海话\n- 旧方言块",
        "glossary": [], "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    answers = ["", "", "", "", "", "2", "1"]
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert saved["user_rules"].count("## 翻译风格：上海话") == 1
    assert "旧方言块" in saved["user_rules"]


def test_init_prefs_standard_keeps_rules(tmp_path, monkeypatch):
    """init --prefs(含方言) + 标准 → 保持 pref 原样（标准=不改动）。"""
    pref = tmp_path / "p3.json"
    rules = "基础规则\n\n## 翻译风格：上海话\n- 旧方言块"
    pref.write_text(json.dumps({
        "version": 1, "user_rules": rules, "glossary": [], "style_refs": [],
    }, ensure_ascii=False), encoding="utf-8")
    answers = ["", "", "", "", "", "1", "1"]  # 1 = 标准
    saved = _run_init(tmp_path, monkeypatch, answers, pref)
    assert saved["user_rules"] == rules

