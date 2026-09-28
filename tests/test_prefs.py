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


def _run_init(tmp_path, monkeypatch, answers, prefs_path):
    from booktr import pipeline
    from booktr.config import Config, find_project_root
    data_dir = tmp_path / ("inst_" + str(abs(hash(str(answers) + str(prefs_path)))))
    cfg = Config(root=find_project_root(), data={}, data_dir=str(data_dir))
    it = iter(answers + [""] * 40)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it, ""))

    class Args:
        force = False
        prefs = str(prefs_path) if prefs_path else None

    pipeline.cmd_init(cfg, Args())
    return json.loads((data_dir / "config.json").read_text(encoding="utf-8"))


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

