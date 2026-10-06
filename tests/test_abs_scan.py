"""abs_scan 审计脚本 + 相对路径工具测试。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os

from booktr import pipeline


def _load_abs_scan():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "tools", "abs_scan.py")
    spec = importlib.util.spec_from_file_location("abs_scan", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_rel_data_relative_to_data_dir(tmp_cfg):
    p = os.path.join(tmp_cfg.work_dir, "qa_auto_runs", "x.log")
    assert pipeline._rel_data(tmp_cfg, p) == "work/qa_auto_runs/x.log"


def test_abs_scan_flags_drive_and_instance(tmp_path):
    m = _load_abs_scan()
    d = tmp_path
    (d / "work").mkdir()
    # JSON 里带 Windows 绝对路径（应命中；且不被 \n 转义误报）
    (d / "work" / "a.json").write_text(
        json.dumps({"source_dir": r"D:\proj\love.life.coocan.jp",
                    "notes": ["x\ny", "e:\n1. foo"]}, ensure_ascii=False),
        encoding="utf-8")
    # 含实例名
    (d / "work" / "b.log").write_text(
        r'--data-dir "..\instance\data-deepseek-v4.1-flash"', encoding="utf-8")
    # 干净文件
    (d / "work" / "c.txt").write_text("hello\nworld\n", encoding="utf-8")

    hits = m.scan(str(d))
    rels = {h["rel"] for h in hits}
    assert any("a.json" in r for r in rels)
    assert any("b.log" in r for r in rels)
    assert not any("c.txt" in r for r in rels)
    # a.json 只报真实盘符，不报 e:\n 误报
    a = next(h for h in hits if "a.json" in h["rel"])
    assert all("proj" in x for x in a["drive"])


def test_abs_scan_main_writes_out(tmp_path):
    m = _load_abs_scan()
    d = tmp_path / "inst"
    d.mkdir()
    (d / "x.txt").write_text("D:\\foo\\bar", encoding="utf-8")
    out = tmp_path / "res.txt"
    rc = m.main(["--data-dir", str(d), "--out", str(out)])
    assert rc == 0 and out.exists()
    assert "abs_scan" in out.read_text(encoding="utf-8")


def test_rel_data_cross_drive_fallback(monkeypatch, tmp_cfg):
    # 模拟跨盘符 relpath 抛 ValueError → 回退原值
    monkeypatch.setattr(pipeline.os.path, "relpath",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError()))
    assert pipeline._rel_data(tmp_cfg, r"Z:\other\x") == r"Z:\other\x"
