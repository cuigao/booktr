"""注本渲染：角标定位、编号顺序、[1,2] 合并、注入兜底、dry-run 无副作用。"""
from __future__ import annotations

import os

from booktr import annotator as ann
from booktr.config import Config


def _cfg():
    return Config(root=".", data={"lang": {"source": "ja", "target": "zh-Hans"}},
                  data_dir=".")


HTML = ("<html><head><title>t</title></head><body>"
        "<p>你好，Libretto很小。</p>"
        "<p>今天天气不错。Ritzberry Fields发售了。</p>"
        "</body></html>")


def _note(dst, **kw):
    d = {"id": "x", "type": "历史考据", "content": "说明。",
         "related_pages": [], "dst_quote": dst}
    d.update(kw)
    return d


def test_render_inserts_sup_and_panel():
    out, diag = ann.render_page(HTML, "a.html", [_note("Libretto")], _cfg())
    assert '<sup class="booktr-ref" data-note="1">[1]</sup>' in out
    assert 'id="booktr-data"' in out and 'id="booktr-js"' in out
    assert "booktr-css" in out
    assert out.index("Libretto") < out.index('[1]')
    assert any(d.lstrip().startswith("OK") for d in diag)


def test_numbering_by_offset():
    notes = [_note("Ritzberry Fields"), _note("Libretto")]
    out, diag = ann.render_page(HTML, "a.html", notes, _cfg())
    # Libretto 在前（更早偏移）应为 [1]，Ritzberry 为 [2]
    assert '[1]' in out and '[2]' in out
    assert out.index('[1]') < out.index('[2]')
    assert 'data-note="1"' in out and 'data-note="2"' in out


def test_same_offset_merges():
    # 两条注命中同一段末尾（同一 dst_quote 的两次出现？改为同段两引文同尾）
    notes = [_note("你好，Libretto很小。"), _note("Libretto很小。")]
    out, _ = ann.render_page(HTML, "a.html", notes, _cfg())
    assert '[1,2]' in out
    assert out.count('<sup class="booktr-ref"') == 1
    # data-note 指向组内第一条
    assert 'data-note="1"' in out


def test_missing_quote_reported_not_injected():
    out, diag = ann.render_page(HTML, "a.html", [_note("不存在的词")], _cfg())
    assert '<sup class="booktr-ref"' not in out
    assert any(d.lstrip().startswith("MISS") for d in diag)


def test_body_missing_inserts_before_html():
    html = "<html><head></head><p>Libretto很小。</p></html>"
    out, _ = ann.render_page(html, "a.html", [_note("Libretto")], _cfg())
    assert out.index("booktr-data") < out.rindex("</html>")
    assert '<sup class="booktr-ref"' in out


def test_header_missing_style_after_body():
    html = "<body><p>Libretto很小。</p></body>"
    out, _ = ann.render_page(html, "a.html", [_note("Libretto")], _cfg())
    assert "<style id=\"booktr-css\">" in out
    assert out.index("booktr-css") > out.index("<body>")


def test_related_pages_prefix_depth():
    import json
    import re
    out, _ = ann.render_page(
        HTML, "today/a.html", [_note("Libretto", related_pages=["b.html"])], _cfg())
    m = re.search(r'id="booktr-data">(.*?)</script>', out, re.S)
    data = json.loads(m.group(1))
    assert data[0]["prefix"] == "../"
    assert data[0]["related_pages"] == ["b.html"]


def test_reader_is_frame_generic():
    out, _ = ann.render_page(HTML, "today/a.html",
                             [_note("Libretto", related_pages=["b.html"])], _cfg())
    # 通用 frame 检测 + 交给顶层宿主（保留 frameset）
    assert "window.self!==window.top" in out
    assert "postMessage" in out and "'ping'" in out and "'pong'" in out
    assert "'render'" in out
    # 无 host 时留在 frame 内降级（不再逃逸）
    assert "window.top.location" not in out
    # 关联页链接 _top（整窗跳转）
    assert 'target="_top"' in out
    # standalone hash 自动打开仍在
    assert "hashchange" in out


def test_host_injected_into_frameset(tmp_path):
    outdir = tmp_path / "out"
    (outdir / "recom").mkdir(parents=True)
    (outdir / "recom" / "rec_old.html").write_text(
        "<html><head><title>t</title></head>"
        "<frameset><frame src='a.html'></frameset></html>", encoding="utf-8")
    (outdir / "index.html").write_text("<html><body>hi</body></html>",
                                       encoding="utf-8")
    data = {"source_dir": "site", "output_dir": "out", "work_dir": "work",
            "llm_logs": {"dir": "work/llm_logs", "auto_export": False}}
    cfg = Config(root=tmp_path, data=data, data_dir=str(tmp_path))
    target = tmp_path / "out_annotated"
    ann.export_annotated(cfg, str(target))
    fs = (target / "recom" / "rec_old.html").read_text(encoding="utf-8")
    assert "booktr-host-js" in fs
    assert "<frameset>" in fs  # 布局未动
    plain = (target / "index.html").read_text(encoding="utf-8")
    assert "booktr-host-js" not in plain


def test_dry_run_no_side_effects(tmp_path):
    # 构造最小 out 与注
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "a.html").write_text(HTML, encoding="utf-8")
    data = {
        "source_dir": "site", "output_dir": "out", "work_dir": "work",
        "lang": {"source": "ja", "target": "zh-Hans"},
        "llm_logs": {"dir": "work/llm_logs", "auto_export": False},
    }
    cfg = Config(root=tmp_path, data=data, data_dir=str(tmp_path))
    ann.add(cfg, {"page": "a.html", "dst_quote": "Libretto", "content": "x",
                  "type": "其他", "related_pages": []})
    target = tmp_path / "out_annotated"
    res = ann.export_annotated(cfg, str(target), dry_run=True)
    assert res["ok"] == 1
    assert not target.exists()
    # 源 out 未变
    assert (outdir / "a.html").read_text(encoding="utf-8") == HTML
