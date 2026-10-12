"""译者注：发现跨页关联与趣味细节，输出外部 JSON；并提供注本渲染导出。"""
from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import shutil
import uuid

from . import glossary as gl_mod
from . import llm as llm_mod
from . import locate as locate_mod
from . import prompts
from . import segments as seg_mod
from . import util
from .config import Config

log = logging.getLogger("booktr.annotator")


def _notes_path(cfg: Config) -> str:
    return cfg.get("translators_notes", "path", default="work/translators_notes.json")


def load(cfg: Config) -> list[dict]:
    return util.read_json(_notes_path(cfg), [])


def add(cfg: Config, entry: dict) -> None:
    items = load(cfg)
    entry["id"] = uuid.uuid4().hex[:12]
    items.append(entry)
    util.write_json(_notes_path(cfg), items)


def _page_segments(cfg: Config, rel: str) -> list[dict]:
    """取该页 text 段的 id/源文/译文（保留占位符）。"""
    segs = seg_mod.segments_for_page(cfg, rel)
    return [{"id": s.id, "src": s.text or "", "dst": s.translation or "",
             "text": s.text or "", "translation": s.translation or "",
             "kind": getattr(s, "kind", "")}
            for s in segs if getattr(s, "kind", "") == "text"]


def _load_summaries(cfg: Config) -> dict:
    summaries = {}
    sdir = cfg.get("summaries", "dir", default="")
    if os.path.isdir(sdir):
        for fn in os.listdir(sdir):
            if fn.endswith(".json"):
                rel_key = fn[:-5].replace("__", "/")
                data = util.read_json(os.path.join(sdir, fn), {})
                if data.get("summary"):
                    summaries[rel_key] = data["summary"]
    return summaries


def _valid_related(cfg: Config, related: list) -> list[str]:
    """仅保留真实存在的页面路径。"""
    src_dir = cfg.source_dir
    out = []
    for p in related or []:
        p = str(p).strip()
        if not p:
            continue
        if os.path.exists(os.path.join(src_dir, p.replace("/", os.sep))):
            out.append(p)
    return out


def generate_for_page(cfg: Config, client, rel: str, translated: str = "") -> int:
    """为单页生成译者注（携带可锚定的 src_quote / dst_quote）。返回新增条数。

    ``translated`` 仅为向后兼容保留（不再用于取片段）；片段改为从段缓存读取。
    """
    segs = _page_segments(cfg, rel)
    if not segs:
        return 0
    summaries = _load_summaries(cfg)
    glossary = gl_mod.load(cfg)
    max_notes = int(cfg.get("translators_notes", "max_notes_per_page", default=0) or 0)
    sysp = prompts.build_translator_note_system(cfg, glossary=glossary,
                                                max_notes=max_notes)
    usr = prompts.build_translator_note_user(rel, segs, summaries,
                                             max_notes=max_notes)
    resp = client.chat(sysp, usr, temperature=0.6,
                       tag=f"annotate_{rel.replace('/', '_')}")
    try:
        data = llm_mod.parse_json_response(resp)
    except llm_mod.LLMError:
        log.warning("译者注解析失败 %s（响应无法解析为 JSON）", rel)
        return 0

    # 定位用段对象（与展示层同口径）
    loc_segs = seg_mod.segments_for_page(cfg, rel)
    notes = [n for n in (data.get("notes", []) or []) if isinstance(n, dict)]
    if max_notes > 0:
        notes = notes[:max_notes]
    count = 0
    for n in notes:
        content = (n.get("content") or "").strip()
        if not content:
            continue
        src_quote = (n.get("src_quote") or "").strip()
        dst_quote = (n.get("dst_quote") or "").strip()
        sid = None
        if dst_quote or src_quote:
            cand = locate_mod.locate(loc_segs, src_frag=src_quote,
                                     dst_frag=dst_quote)
            if cand:
                sid = cand[0]["sid"]
        add(cfg, {
            "page": rel,
            "src_quote": src_quote,
            "dst_quote": dst_quote,
            "segment_id": sid,
            "content": content,
            "related_pages": _valid_related(cfg, n.get("related_pages", [])),
            "type": n.get("type", "其他"),
            "user_focus": cfg.get("translators_notes", "focus", default=""),
            "created_by": "llm",
        })
        count += 1
    return count


# ── 注本渲染（Phase 1） ────────────────────────────────────────────────
# 展示层设计见 instance/report/translator_notes_design.md：
# 正文只插极小 <sup> 角标；注释内容入侧栏浮层（脱离文档流）。

STYLE = """<style id="booktr-css">
.booktr-ref{cursor:pointer;color:#b3402e;font-size:0.72em;vertical-align:super;line-height:0}
#booktr-panel{all:initial;position:fixed;top:0;right:0;display:block;width:min(340px,100vw);height:100%;
  overflow-y:auto;box-sizing:border-box;padding:18px 18px 48px;background:#fffdf5;
  border-left:1px solid #e4d9b8;box-shadow:-8px 0 24px rgba(0,0,0,.18);
  transform:translateX(102%);transition:transform .22s ease;z-index:2147483647;
  font:14px/1.85 "PingFang SC","Microsoft YaHei",sans-serif;color:#2a2416}
#booktr-panel.booktr-open{transform:none}
#booktr-panel .booktr-ttl{display:block;font-weight:700;font-size:15px;color:#7a5a1a;
  margin:0 0 12px;padding-bottom:8px;border-bottom:1px dashed #e0cfa0}
#booktr-panel .booktr-card{display:block;border:1px solid #ead9b0;border-radius:8px;
  padding:10px 12px;margin:0 0 12px;background:#fff}
#booktr-panel .booktr-card.booktr-active{background:#fff6dd;border-color:#e0b84a;
  box-shadow:0 0 0 2px #ffe9a8}
#booktr-panel .booktr-head{display:block;font-weight:700;font-size:13px;color:#8a5a1a;
  margin:0 0 4px}
#booktr-panel .booktr-body{display:block;margin:0 0 6px;white-space:normal}
#booktr-panel .booktr-rel{display:block;font-size:12.5px;color:#6a5a3a}
#booktr-panel .booktr-rel a{color:#1a6fb5;text-decoration:none;border-bottom:1px dotted #9bc}
#booktr-panel .booktr-diff{display:block;margin-top:5px;line-height:1.8;word-break:break-word;white-space:pre-wrap}
#booktr-panel .booktr-del{color:#b3402e;text-decoration:line-through;opacity:.85}
#booktr-panel .booktr-ins{color:#1a7f37;background:#eafff0;border-radius:2px}
#booktr-panel .booktr-close{position:absolute;top:10px;right:12px;cursor:pointer;
  font-size:18px;line-height:1;color:#9a8a60;background:none;border:none;padding:2px 6px}
@media print{#booktr-panel{display:none}}
</style>"""

READER_JS = """<script id="booktr-js">
(function(){
  var d=document.getElementById('booktr-data');
  var notes=[];try{notes=JSON.parse(d.textContent||'[]')}catch(e){}
  if(!notes.length)return;
  // 本页是否被 frame 挂载。与页面无关，对任何被 frame 挂载的页通用。
  var framed=false; try{ framed=window.self!==window.top; }catch(e){ framed=true; }
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function abs(u){ try{ return new URL(u, location.href).href; }catch(e){ return u; } }
  // 面板 HTML（standalone 直接插入；framed 交给顶层 host 插入）。关联页链接为绝对 URL，
  // 因宿主可能在别的层级；target=_top 使点击整窗跳转。
  function panelHtml(active){
    var a=String(active==null?'':active);
    var h='<button class="booktr-close" title="关闭">\\u00d7</button>';
    h+='<div class="booktr-ttl">\\u8bd1\\u8005\\u6ce8</div>';
    notes.forEach(function(n){
      h+='<div class="booktr-card'+(String(n.n)===a?' booktr-active':'')+'" id="booktr-card-'+n.n+'">';
      h+='<span class="booktr-head">'+esc(n.type)+' ['+n.n+']</span>';
      var body = n.diff_html
        ? esc(n.content)+'<div class="booktr-diff">'+n.diff_html+'</div>'
        : esc(n.content);
      h+='<div class="booktr-body">'+body+'</div>';
      if(n.related_pages&&n.related_pages.length){
        h+='<div class="booktr-rel">\\u5173\\u8054\\u9875\\u9762\\uff1a';
        n.related_pages.forEach(function(p,i){
          h+=(i?'\\u3001':'')+'<a href="'+esc(abs((n.prefix||'')+p))+'" target="_top">'+esc(p)+'</a>';
        });
        h+='</div>';
      }
      h+='</div>';
    });
    return h;
  }
  function activate(panel,n){
    var c=document.getElementById('booktr-card-'+n); if(!c)return;
    var ar=panel.querySelectorAll('.booktr-card.booktr-active');
    for(var i=0;i<ar.length;i++)ar[i].classList.remove('booktr-active');
    c.classList.add('booktr-active');
    if(c.scrollIntoView)c.scrollIntoView({block:'nearest'});
  }
  // 页内面板（standalone 用；framed 无 host 时作降级）
  var localPanel=null;
  function mountLocal(){
    if(localPanel)return localPanel;
    localPanel=document.createElement('aside');
    localPanel.id='booktr-panel';
    localPanel.innerHTML=panelHtml('');
    document.body.appendChild(localPanel);
    localPanel.querySelector('.booktr-close').addEventListener('click',function(){
      localPanel.classList.remove('booktr-open');});
    return localPanel;
  }
  function openLocal(n){ var p=mountLocal(); p.classList.add('booktr-open'); activate(p,n); }

  if(!framed){
    // 独立页：页内面板（原行为）
    mountLocal();
    var refs0=document.querySelectorAll('.booktr-ref');
    for(var k0=0;k0<refs0.length;k0++){
      (function(el){el.addEventListener('click',function(){openLocal(el.getAttribute('data-note'))})})(refs0[k0]);
    }
    function fromHash(){ var m=(location.hash||'').match(/^#booktr-(\\d+)$/); if(m)openLocal(m[1]); }
    fromHash();
    window.addEventListener&&window.addEventListener('hashchange',fromHash);
    return;
  }

  // 被 frame 挂载：把面板交给顶层 host 渲染（保留 frameset）；无 host 则页内降级（留在 frame 内）。
  var host=false;
  try{ window.addEventListener('message',function(e){
    var m=e.data||{}; if(m&&m.__booktr==='pong')host=true;
  }); }catch(e){}
  try{ window.top.postMessage({__booktr:'ping'},'*'); }catch(e){}
  var refs=document.querySelectorAll('.booktr-ref');
  for(var k=0;k<refs.length;k++){
    (function(el){el.addEventListener('click',function(){
      var n=el.getAttribute('data-note');
      if(host){ try{ window.top.postMessage({__booktr:'render',html:panelHtml(n),n:n},'*'); return; }catch(e){} }
      openLocal(n);
    })})(refs[k]);
  }
})();
</script>"""

# 顶层宿主脚本：注入到含 <frameset>/<iframe> 的页（这些页自身无注）。接收子 frame 的
# postMessage（ping→pong；render→在顶层整幅叠加 #booktr-panel），从而保留 frameset 布局。
HOST_JS = """<script id="booktr-host-js">
(function(){
  try{ if(window.self!==window.top)return; }catch(e){ return; }
  var panel=null;
  function ensure(){
    if(panel&&panel.parentNode)return panel;
    panel=document.createElement('aside');
    panel.id='booktr-panel';
    (document.documentElement||document.body).appendChild(panel);
    panel.addEventListener('click',function(e){
      var t=e.target; if(t&&t.className&&String(t.className).indexOf('booktr-close')>=0){
        panel.classList.remove('booktr-open'); }});
    return panel;
  }
  window.addEventListener('message',function(e){
    var m=e.data||{};
    if(!m||(m.__booktr!=='ping'&&m.__booktr!=='render'))return;
    if(m.__booktr==='ping'){ try{ e.source.postMessage({__booktr:'pong'},'*'); }catch(x){} return; }
    var p=ensure(); p.innerHTML=m.html; p.classList.add('booktr-open');
    try{ e.source.postMessage({__booktr:'ack'},'*'); }catch(x){}
  });
})();
</script>"""

_PLACEHOLDER_RE = re.compile(r"\[\[P\d+\]\]")


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


# 单个「短句」内允许 inline 高亮的最大修正处数；超过则整句划掉重写（避免碎改影响阅读）。
# 计数口径：删除记 1 处、添加记 1 处、替换（删+添）记 2 处。
_DIFF_INLINE_MAX = 2
# 停顿类标点：用于把勘误文本切分为「短句」（标点归入前一句句尾）。
# 不含 `.` 与 `…`：避免省略号 "..." / "…" 造成不对称分句，从而可原样比对出省略号差异。
_PAUSE_PUNCT = frozenset("。，、！？；：,?!;:")
_PUNCT_ONLY_STR = "".join(_PAUSE_PUNCT)
# 空白可视化：纯空白变更片段中的空白替换为可见符号。
_WS_VIS = {" ": "␣", "\u3000": "␣", "\t": "⇥", "\n": "⏎", "\r": ""}


def _clauses(s: str) -> list[str]:
    """按停顿类标点切分为「短句」（标点归入前一句句尾），丢弃纯标点空句。"""
    out: list[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if ch in _PAUSE_PUNCT:
            if buf.strip(_PUNCT_ONLY_STR):
                out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _nchanges(a: str, b: str) -> int:
    """两串间的修正处数：删除记 1、添加记 1、替换记 2（删+添）。"""
    n = 0
    for tag, *_ in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "replace":
            n += 2
        elif tag in ("delete", "insert"):
            n += 1
    return n


def _esc_delim(s: str) -> str:
    """del/ins 片段转义；纯空白片段用可见符号标出（便于发现空格/换行变更）。"""
    if s and not s.strip():
        return "".join(_WS_VIS.get(ch, _esc(ch)) for ch in s)
    return _esc(s)


def _del_span(s: str) -> str:
    return f'<del class="booktr-del">{_esc_delim(s)}</del>'


def _ins_span(s: str) -> str:
    return f'<ins class="booktr-ins">{_esc_delim(s)}</ins>'


def _inline_diff(a: str, b: str) -> str:
    """逐字内联 diff（``<del>``/``<ins>``），文本段一律转义。"""
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            parts.append(_esc(a[i1:i2]))
        elif tag == "delete":
            parts.append(_del_span(a[i1:i2]))
        elif tag == "insert":
            parts.append(_ins_span(b[j1:j2]))
        else:  # replace
            parts.append(_del_span(a[i1:i2]) + _ins_span(b[j1:j2]))
    return "".join(parts)


def _diff_html(old: str, new: str) -> str:
    """把 ``old -> new`` 的差异渲染为 HTML（勘误卡片用）。

    按停顿标点切分为「短句」后逐句比较：
    - 单句内修正处 ≤ ``_DIFF_INLINE_MAX`` → 逐字内联高亮；
    - 单句内修正处 > ``_DIFF_INLINE_MAX`` → 整句划掉重写（``<del>整句</del><ins>重写</ins>``）；
    - 短句数不一致的替换块 → 整块划掉重写。

    其中「修正处」按 :func:`_nchanges` 计数（删除 1、添加 1、替换 2）。

    仅去除占位符，**保留空白与标点**（空格/省略号等微小变更亦可见）。
    文本段一律 :func:`html.escape`，输出只含自定义标签，安全可内联。
    原始文本（去占位符后）相同则返回空串。
    """
    from . import refs as refs_mod

    A = _clauses(refs_mod.strip_placeholders(old or ""))
    B = _clauses(refs_mod.strip_placeholders(new or ""))
    parts: list[str] = []
    changed = False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, A, B).get_opcodes():
        if tag == "equal":
            parts.append(_esc("".join(A[i1:i2])))
            continue
        changed = True
        oa, ob = A[i1:i2], B[j1:j2]
        if tag == "replace" and len(oa) == len(ob):
            for x, y in zip(oa, ob):
                if x == y:
                    parts.append(_esc(x))
                elif _nchanges(x, y) > _DIFF_INLINE_MAX:
                    parts.append(_del_span(x) + _ins_span(y))
                else:
                    parts.append(_inline_diff(x, y))
        else:
            old_c, new_c = "".join(oa), "".join(ob)
            if old_c:
                parts.append(_del_span(old_c))
            if new_c:
                parts.append(_ins_span(new_c))
    return "".join(parts) if changed else ""


def _norm(text: str) -> str:
    return util.normalize_ws(text or "")


def _normalize_with_offsets(raw: str, base: int) -> tuple[str, list[int]]:
    """剥离占位符 + 空白归一，返回 (归一文本, 每个字符对应的原文偏移)。

    每个归一字符记录其在 ``raw``（相对偏移 + ``base``）中的起始位置；空白串折叠
    为单个空格，记录该串首个空白的位置。
    """
    chars: list[str] = []
    offs: list[int] = []
    prev_space = False
    i = 0
    n = len(raw)
    while i < n:
        m = _PLACEHOLDER_RE.match(raw, i)
        if m:
            i = m.end()
            continue
        ch = raw[i]
        if ch.isspace():
            if not prev_space:
                chars.append(" ")
                offs.append(base + i)
                prev_space = True
        else:
            chars.append(ch)
            offs.append(base + i)
            prev_space = False
        i += 1
    # 去首尾空格
    while chars and chars[0] == " ":
        chars.pop(0)
        offs.pop(0)
    while chars and chars[-1] == " ":
        chars.pop()
        offs.pop()
    return "".join(chars), offs


def _find_quote_end(html: str, seg, quote: str) -> int | None:
    """在段内定位引文，返回 HTML 中引文**结束**后的偏移（None=未命中）。

    优先字面精确匹配；否则在（剥离占位符 + 空白归一）的段文本上匹配并还原偏移。
    这样含全角空格差异、跨占位符边界的引文也能锚定到片段末尾。
    """
    q = _norm(quote)
    if not q:
        return None
    lo, hi = seg.start, seg.end
    # 1) 字面精确
    idx = html.find(quote, lo, hi)
    if idx >= 0:
        return idx + len(quote)
    # 2) 归一 + 占位符剥离
    norm_text, offs = _normalize_with_offsets(html[lo:hi], lo)
    needle = re.sub(r"\s+", " ", q)
    pos = norm_text.find(needle)
    if pos < 0:
        return None
    end_ci = pos + len(needle) - 1
    if end_ci < 0 or end_ci >= len(offs):
        return None
    return offs[end_ci] + 1


def render_page(html: str, rel: str, notes: list[dict], cfg: Config) -> tuple[str, list[str]]:
    """对单页 HTML 注入译者注角标与侧栏。返回 (新 HTML, 诊断行列表)。

    源 ``html`` 不被修改；无任何可用注则原样返回。
    """
    diag: list[str] = []
    items = [n for n in notes if (n.get("dst_quote") or "").strip()]
    if not items:
        return html, diag
    segs = seg_mod.split_segments(html, cfg)

    resolved: list[tuple[int, dict, int]] = []  # (end, note, sid)
    for note in items:
        q = note.get("dst_quote", "")
        hit = None
        for s in segs:
            if getattr(s, "kind", "") != "text":
                continue
            if _norm(q) and _norm(q) in _norm(getattr(s, "text", "")):
                end = _find_quote_end(html, s, q)
                if end is not None:
                    hit = (end, s.id)
                    break
        if hit is None:
            diag.append(f"  MISS  {rel}  dst_quote={q!r}")
            continue
        resolved.append((hit[0], note, hit[1]))

    if not resolved:
        return html, diag

    resolved.sort(key=lambda x: x[0])
    depth = rel.count("/")
    prefix = "../" * depth
    by_offset: dict[int, list[dict]] = {}
    data = []
    for num, (end, note, sid) in enumerate(resolved, 1):
        by_offset.setdefault(end, []).append(num)
        data.append({
            "n": num, "id": note.get("id", ""), "type": note.get("type", ""),
            "content": note.get("content", ""),
            "diff_html": note.get("diff_html", ""),
            "related_pages": note.get("related_pages", []) or [],
            "prefix": prefix,
        })
        diag.append(f"  OK    {rel}  [{num}] dst_quote={note.get('dst_quote')!r} "
                    f"-> sid={sid} end={end}")

    # 降序插入，保持偏移有效
    out = html
    for end in sorted(by_offset.keys(), reverse=True):
        nums = ",".join(str(n) for n in sorted(by_offset[end]))
        sup = f'<sup class="booktr-ref" data-note="{by_offset[end][0]}">[{nums}]</sup>'
        out = out[:end] + sup + out[end:]

    # style -> </head> 前（兜底 <body> 后 / 文件首）
    m = re.search(r"</head\s*>", out, re.I)
    if m:
        out = out[:m.start()] + STYLE + "\n" + out[m.start():]
    else:
        m2 = re.search(r"<body[^>]*>", out, re.I)
        out = (out[:m2.end()] + "\n" + STYLE + out[m2.end():]) if m2 else STYLE + out

    # data + reader -> </body> 前（兜底 </html> 前 / 文件尾）
    payload = ('<script type="application/json" id="booktr-data">'
               + json.dumps(data, ensure_ascii=False) + "</script>\n" + READER_JS + "\n")
    mb = re.search(r"</body\s*>", out, re.I)
    if mb:
        out = out[:mb.start()] + payload + out[mb.start():]
    else:
        mh = re.search(r"</html\s*>", out, re.I)
        out = (out[:mh.start()] + payload + out[mh.start():]) if mh else out + "\n" + payload
    return out, diag


_FRAME_RE = re.compile(r"<frameset\b|<iframe\b", re.I)


def _inject_host(html: str) -> str:
    """把宿主脚本 + 样式注入到含 frameset/iframe 的顶层页（无注，但作侧栏宿主）。"""
    m = re.search(r"</head\s*>", html, re.I)
    block = STYLE + "\n" + HOST_JS + "\n"
    if m:
        return html[:m.start()] + block + html[m.start():]
    m2 = re.search(r"<body[^>]*>", html, re.I)
    if m2:
        return html[:m2.end()] + "\n" + block + html[m2.end():]
    return block + html


def export_annotated(cfg: Config, out_dir: str, pages: list[str] | None = None,
                     dry_run: bool = False) -> dict:
    """导出注本：复制 ``out`` 树到 ``out_dir``，按 ``dst_quote`` 定位并注入角标/侧栏。

    源 ``out`` 零改动。``pages`` 限定要注的页面（缺省=所有有注的页）；``dry_run``
    只打印定位报告、不复制、不写文件。返回 ``{pages, ok, miss, copied, out_dir}``。
    """
    src_out = cfg.output_dir
    all_notes = load(cfg)
    by_page: dict[str, list[dict]] = {}
    for n in all_notes:
        rel = n.get("page") or (n.get("anchor") or {}).get("page")
        if rel:
            by_page.setdefault(rel, []).append(n)
    # 参考勘误：以「参考勘误」类注记并入（独立于 translators_notes，reset/clean 不清）
    if cfg.get("refs", "render_corrections", default=True):
        from . import refs as refs_mod
        for rel, cors in refs_mod.all_active(cfg).items():
            for c in cors:
                diff_html = _diff_html(c.get("ref_err", ""), c.get("corrected", ""))
                if not diff_html:
                    continue  # 真·空改（去占位符后原始文本相同）不渲染卡片
                by_page.setdefault(rel, []).append({
                    "id": c.get("id", ""),
                    "dst_quote": (c.get("corrected") or "")[:24],
                    "type": "参考勘误",
                    "content": "参考译文此处有误，已修正：",
                    "diff_html": diff_html,
                    "related_pages": [],
                })
    if pages:
        by_page = {p: by_page.get(p, []) for p in pages}
        by_page = {p: v for p, v in by_page.items() if v}

    diag: list[str] = []
    ok = miss = 0
    if not dry_run:
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        shutil.copytree(src_out, out_dir)

    for rel in sorted(by_page):
        p = os.path.join(src_out, rel.replace("/", os.sep))
        if not os.path.exists(p):
            diag.append(f"  SKIP  {rel}  (out page missing)")
            continue
        with open(p, "r", encoding="utf-8", newline="") as f:
            html = f.read()
        new, d = render_page(html, rel, by_page[rel], cfg)
        diag.extend(d)
        n_ok = sum(1 for x in d if x.lstrip().startswith("OK"))
        ok += n_ok
        miss += sum(1 for x in d if x.lstrip().startswith("MISS"))
        if not dry_run:
            dst = os.path.join(out_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(new)

    # 宿主：为含 frameset/iframe 的页注入顶层宿主脚本（保留 frameset 布局）
    hosts = 0
    if not dry_run:
        noted = set(by_page)
        for dp, _dn, fs in os.walk(out_dir):
            for fn in fs:
                if not fn.lower().endswith((".html", ".htm")):
                    continue
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, out_dir).replace(os.sep, "/")
                if rel in noted:
                    continue
                with open(p, "r", encoding="utf-8", newline="") as f:
                    h = f.read()
                if _FRAME_RE.search(h):
                    with open(p, "w", encoding="utf-8", newline="") as f:
                        f.write(_inject_host(h))
                    hosts += 1
                    diag.append(f"  HOST  {rel}")

    print("=== 译者注定位诊断 ===")
    print("\n".join(diag) if diag else "(无)")
    print(f"\n页面 {len(by_page)} / 命中 {ok} / 未命中 {miss}"
          + (f" -> {out_dir}" if not dry_run else "  [dry-run]"))
    return {"pages": len(by_page), "ok": ok, "miss": miss,
            "out_dir": out_dir, "dry_run": dry_run}
