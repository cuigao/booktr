# -*- coding: utf-8 -*-
"""通用语言感知编码检测 + fix 命令测试。"""
from __future__ import annotations

import os

import pytest

from booktr import util
from booktr.crawler import decode_page, scan_site
from booktr.pipeline import cmd_fix
from conftest import write_sample_site

UNDECODABLE = b"\x81\x00" * 100  # 无法以任何 ja 候选编码严格解码


# ── detect_encoding：语言候选 ───────────────────────────────────────────


def test_detect_encoding_meta_declared_wins():
    # 声明 utf-8 优先于语言默认（ja→cp932）
    raw = b'<meta charset="utf-8"><TITLE>x</TITLE>'
    assert util.detect_encoding(raw, "utf-8", "ja") == "utf-8"


def test_detect_encoding_japanese_cp932():
    raw = "<TITLE>岡崎律子</TITLE>".encode("cp932")
    assert util.detect_encoding(raw, None, "ja") == "cp932"


def test_detect_encoding_unknown_lang_fallback_utf8():
    # 未预设语言 → 回退列表 utf-8 优先
    raw = "<TITLE>こんにちは</TITLE>".encode("utf-8")
    assert util.detect_encoding(raw, None, "xx-yy") == "utf-8"


def test_detect_encoding_all_fail_none():
    assert util.detect_encoding(UNDECODABLE, None, "ja") is None


def test_encoding_candidates_meta_lang_utf8():
    cands = util._encoding_candidates("big5", "zh-Hant")
    assert cands[0] == "big5"  # meta 声明优先
    assert "big5" in cands
    assert "utf-8" in cands  # 兜底


# ── decode_html 严格 / 宽松 ─────────────────────────────────────────────


def test_decode_html_requires_lang():
    with pytest.raises(TypeError):
        util.decode_html(b"<TITLE>x</TITLE>")


def test_decode_html_undecodable_raises():
    with pytest.raises(util.EncodingError):
        util.decode_html(UNDECODABLE, "ja")


def test_decode_html_loose_repairs():
    text, enc = util.decode_html_loose(UNDECODABLE, "ja")
    assert isinstance(text, str)


# ── decode_page：缓存编码复用 ───────────────────────────────────────────


def test_decode_page_cached_encoding(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    # 先 scan 缓存编码
    scan_site(tmp_cfg, force=True)
    text, enc = decode_page(tmp_cfg, "page1.html")
    assert enc == "cp932"
    assert "こんにちは" in text


def test_decode_page_no_site_map(tmp_cfg, tmp_path):
    # 无 site_map 时走全套探测
    write_sample_site(tmp_path)
    text, enc = decode_page(tmp_cfg, "page1.html")
    assert "こんにちは" in text


def test_decode_page_strict_raises(tmp_cfg, tmp_path):
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "bad.html").write_bytes(UNDECODABLE)
    with pytest.raises(util.EncodingError):
        decode_page(tmp_cfg, "bad.html")


# ── scan 跳过无法解码页并汇总 ──────────────────────────────────────────


def test_scan_skips_undecodable(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    site = tmp_path / "site"
    (site / "bad.html").write_bytes(UNDECODABLE)
    sm = scan_site(tmp_cfg, force=True)
    assert "bad.html" in sm.get("encoding_failed", [])
    assert "page1.html" in sm.get("pages", {})  # 正常页仍被扫描


# ── cmd_fix ─────────────────────────────────────────────────────────────


def _args(**kw):
    import argparse
    base = {"all": False, "out": None, "dry_run": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_fix_outputs_fixed(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    site = tmp_path / "site"
    (site / "bad.html").write_bytes(UNDECODABLE)

    cmd_fix(tmp_cfg, _args())

    fix_out = os.path.join(tmp_cfg.data_dir, "fix")
    assert os.path.exists(os.path.join(fix_out, "bad.html"))  # 修复输出
    # 原始文件未修改
    assert (site / "bad.html").read_bytes() == UNDECODABLE
    # 默认不复制正常页
    assert not os.path.exists(os.path.join(fix_out, "page1.html"))


def test_cmd_fix_all_copies_everything(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    site = tmp_path / "site"
    (site / "bad.html").write_bytes(UNDECODABLE)

    cmd_fix(tmp_cfg, _args(all=True))

    fix_out = os.path.join(tmp_cfg.data_dir, "fix")
    assert os.path.exists(os.path.join(fix_out, "bad.html"))
    assert os.path.exists(os.path.join(fix_out, "page1.html"))  # --all 复制正常页


def test_cmd_fix_dry_run_no_write(tmp_cfg, tmp_path):
    write_sample_site(tmp_path)
    site = tmp_path / "site"
    (site / "bad.html").write_bytes(UNDECODABLE)

    cmd_fix(tmp_cfg, _args(dry_run=True))

    fix_out = os.path.join(tmp_cfg.data_dir, "fix")
    assert not os.path.exists(fix_out)  # dry-run 不写任何文件
