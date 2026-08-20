"""站点扫描：遍历镜像、编码探测、链接图、标题/日期提取。"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import asdict, dataclass, field

from . import util
from .config import Config

_DATE_RE = re.compile(r"(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?")
_SHORT_DATE_RE = re.compile(r"'?(\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?")
_YM_RE = re.compile(r"(\d{4})[年/.-](\d{1,2})月")
_HTML_RE = re.compile(r"\.(html?|htm)$", re.IGNORECASE)


@dataclass
class Page:
    rel: str  # 相对源目录的路径，如 today/today45.html
    size: int
    encoding: str
    title: str = ""
    date: str | None = None  # 归一化日期 YYYY-MM-DD
    date_note: str = ""
    links_out: list[str] = field(default_factory=list)  # 本地 html 相对路径（保序）
    links_ext: list[str] = field(default_factory=list)
    section: str = ""
    kind: str = "static"  # static | diary | photo_diary | index | special
    volume: int | None = None
    jap_chars: int = 0
    text_len: int = 0  # 可翻译文本长度（语言无关，去标签后的非空白字符数）
    in_degree: int = 0  # 反向引用次数（被多少页链接）
    depth: int = 0  # 距根 BFS 深度
    nav_pos: int | None = None  # index.html 导航链接位次（1 起）
    dfs_order: int = 0  # 从根按 links 顺序 DFS pre-order 访问序


def _norm_date(y: str, m: str, d: str) -> str:
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


def _norm_short_date(yy: str, m: str, d: str) -> str:
    """两位年 → 四位年（世纪推断）：yy>70 → 19yy，否则 20yy。"""
    yy = int(yy)
    year = 1900 + yy if yy > 70 else 2000 + yy
    return _norm_date(year, int(m), int(d))


def _parse_date_candidates(text: str) -> tuple[str | None, str]:
    """从注释/正文提取日期，返回 (归一化日期, 原始文本)。

    支持：四位年完整日期、两位年缩写日期（'97.6.17）、仅年月。
    """
    for m in _DATE_RE.finditer(text):
        return _norm_date(m.group(1), m.group(2), m.group(3)), m.group(0)
    for m in _SHORT_DATE_RE.finditer(text):
        return _norm_short_date(m.group(1), m.group(2), m.group(3)), m.group(0)
    for m in _YM_RE.finditer(text):
        return _norm_date(m.group(1), m.group(2), "1"), m.group(0)
    return None, ""


def _extract_title(decoded: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.I | re.S)
    if not m:
        return ""
    return util.normalize_ws(re.sub(r"<[^>]+>", "", m.group(1)))


def _classify(rel: str, decoded: str) -> tuple[str, int | None]:
    """根据路径与内容判断页面种类。"""
    name = os.path.basename(rel).lower()
    if rel.startswith("today/"):
        if name == "today0.html":
            return "index", None
        if name == "1staniv.html":
            return "special", None
        m = re.match(r"today(\d+)\.html$", name)
        if m:
            return "diary", int(m.group(1))
        return "special", None
    if rel.startswith("photo/"):
        if name in ("photo.html", "photo0.html"):
            return "index", None
        m = re.match(r"photo(\d+)\.html$", name)
        if m:
            return "photo_diary", int(m.group(1))
        return "index", None
    if rel.startswith("recom/"):
        return "static", None
    return "static", None


def scan_site(cfg: Config, force: bool = False) -> dict:
    """扫描源目录，构建 site_map.json。"""
    out_path = os.path.join(cfg.work_dir, "site_map.json")
    if not force and os.path.exists(out_path):
        return util.read_json(out_path, {})

    src = cfg.source_dir
    pages: dict[str, Page] = {}
    encoding_failed: list[str] = []
    for f in glob.glob(os.path.join(src, "**", "*"), recursive=True):
        if not os.path.isfile(f):
            continue
        rel = os.path.relpath(f, src).replace("\\", "/")
        if _HTML_RE.search(rel):
            try:
                pages[rel] = _scan_html(cfg, f, rel)
            except util.EncodingError:
                encoding_failed.append(rel)
    _enrich_dates_from_indexes(cfg, pages)
    _compute_derived(pages)
    # 非 html 文件登记（复制用）
    assets = []
    for f in glob.glob(os.path.join(src, "**", "*"), recursive=True):
        if os.path.isfile(f):
            rel = os.path.relpath(f, src).replace("\\", "/")
            if not _HTML_RE.search(rel):
                assets.append(rel)

    result = {
        "source_dir": src,
        "total_pages": len(pages),
        "total_assets": len(assets),
        "assets": sorted(assets),
        "pages": {k: asdict(v) for k, v in pages.items()},
        "encoding_failed": sorted(encoding_failed),
    }
    util.write_json(out_path, result)
    return result


def _scan_html(cfg: Config, path: str, rel: str) -> Page:
    raw = open(path, "rb").read()
    lang = cfg.get("lang", "source", default="ja")
    decoded, enc = util.decode_html(raw, lang)
    title = _extract_title(decoded)
    date, date_note = _date_from_comment(decoded)
    links_out, links_ext = _extract_links(decoded, os.path.dirname(rel))
    kind, volume = _classify(rel, decoded)
    return Page(
        rel=rel,
        size=len(raw),
        encoding=enc,
        title=title,
        date=date,
        date_note=date_note,
        links_out=links_out,
        links_ext=links_ext,
        section=rel.split("/")[0] if "/" in rel else "(root)",
        kind=kind,
        volume=volume,
        jap_chars=util.count_japanese(decoded),
        text_len=_extract_text_len(decoded),
    )


def _date_from_comment(decoded: str) -> tuple[str | None, str]:
    """只从 HTML 注释里提取日期，避免正文误匹配。"""
    for m in re.finditer(r"<!--(.*?)-->", decoded, re.I | re.S):
        d, note = _parse_date_candidates(m.group(1))
        if d:
            return d, note
    return None, ""


def _enrich_dates_from_indexes(cfg: Config, pages: dict[str, Page]) -> None:
    """从索引页（today0.html/photo0.html）的链接文本中提取日期，补全日记页。

    页面注释不一定带日期（尤其早期），但索引页列出了每篇的发布日。
    """
    src = cfg.source_dir
    lang = cfg.get("lang", "source", default="ja")
    for index_rel, subdir in (("today/today0.html", "today/"), ("photo/photo0.html", "photo/")):
        idx = pages.get(index_rel)
        if not idx:
            continue
        path = os.path.join(src, index_rel.replace("/", os.sep))
        try:
            decoded, _ = util.decode_html(open(path, "rb").read(), lang)
        except (OSError, util.EncodingError):
            continue
        for m in re.finditer(
            r'<a[^>]+href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', decoded, re.I | re.S
        ):
            href = m.group(1).split("#")[0]
            if not _HTML_RE.search(href):
                continue
            rel = os.path.normpath(os.path.join(subdir, href)).replace("\\", "/")
            rel = os.path.relpath(os.path.join(src, rel), src).replace("\\", "/")
            target = pages.get(rel)
            if not target or target.date:
                continue
            text = re.sub(r"<[^>]+>", "", m.group(2))
            date, note = _parse_date_candidates(text)
            if date:
                target.date = date
                target.date_note = target.date_note or note


def _compute_derived(pages: dict[str, Page]) -> None:
    """计算派生指标：in_degree、depth(BFS 距根)、nav_pos、dfs_order。"""
    # in_degree
    for p in pages.values():
        p.in_degree = 0
    for p in pages.values():
        for o in p.links_out:
            if o in pages:
                pages[o].in_degree += 1
    # BFS depth from root index.html
    root = "index.html"
    for p in pages.values():
        p.depth = 0
    if root in pages:
        from collections import deque
        q = deque([(root, 0)])
        seen = {root}
        while q:
            rel, d = q.popleft()
            pages[rel].depth = d
            for o in pages[rel].links_out:
                if o in pages and o not in seen:
                    seen.add(o)
                    q.append((o, d + 1))
    # nav_pos：index.html 直接链接的位次（1 起）
    for p in pages.values():
        p.nav_pos = None
    if root in pages:
        for i, o in enumerate(pages[root].links_out, 1):
            if o in pages:
                pages[o].nav_pos = i
    # dfs_order：从 root 保序 DFS pre-order
    for p in pages.values():
        p.dfs_order = 0
    visited: set[str] = set()
    counter = [0]

    def _dfs(rel: str) -> None:
        if rel in visited or rel not in pages:
            return
        visited.add(rel)
        counter[0] += 1
        pages[rel].dfs_order = counter[0]
        for o in pages[rel].links_out:
            _dfs(o)

    _dfs(root)
    for rel in pages:
        _dfs(rel)


def _extract_links(decoded: str, dirpath: str) -> tuple[list[str], list[str]]:
    out: list[str] = []
    ext: list[str] = []
    for m in re.finditer(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']', decoded, re.I):
        href = m.group(1).strip()
        if href.startswith(("http://", "https://")):
            ext.append(href)
            continue
        if href.startswith(("mailto:", "javascript:", "#")):
            continue
        if not _HTML_RE.search(href):
            continue
        # 解析相对链接（去掉锚点与查询），保序去重
        href = href.split("#")[0].split("?")[0]
        full = os.path.normpath(os.path.join(dirpath, href)).replace("\\", "/")
        if full not in out:
            out.append(full)
    return out, sorted(set(ext))


def _extract_text_len(decoded: str) -> int:
    """可翻译文本长度：去标签/注释/脚本后的非空白字符数（语言无关）。"""
    body = re.sub(r"<!--.*?-->", "", decoded, flags=re.S)
    body = re.sub(r"<script.*?</script>", "", body, flags=re.I | re.S)
    body = re.sub(r"<style.*?</style>", "", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return len(re.sub(r"\s", "", body))


def resolve_local_path(cfg: Config, page_rel: str) -> str:
    return os.path.join(cfg.source_dir, page_rel.replace("/", os.sep))


def cached_encoding(cfg: Config, rel: str) -> str | None:
    """从 site_map 取该页缓存的编码；无缓存返回 None。"""
    from . import planner

    try:
        sm = planner.load_site_map(cfg)
    except RuntimeError:
        return None
    page = sm.get("pages", {}).get(rel)
    if not page:
        return None
    return page.get("encoding")


def decode_page(cfg: Config, rel: str, raw: bytes | None = None,
                strict: bool = True) -> tuple[str, str]:
    """统一读取并解码源页面。

    优先用 site_map 缓存的编码解码；缓存失效（解码失败）则回退全套语言感知探测。
    strict=True 无法解码抛 EncodingError；strict=False 用 errors='replace' 修复。
    返回 (解码后文本, 编码)。
    """
    from . import util

    if raw is None:
        with open(resolve_local_path(cfg, rel), "rb") as f:
            raw = f.read()
    lang = cfg.get("lang", "source", default="ja")

    cached = cached_encoding(cfg, rel)
    if cached:
        try:
            return raw.decode(cached), cached
        except (UnicodeDecodeError, LookupError):
            pass  # 缓存编码失效 → 回退全套探测

    if strict:
        return util.decode_html(raw, lang)
    return util.decode_html_loose(raw, lang)
