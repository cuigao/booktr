"""abs_scan：审计某个实例目录中残留的绝对路径 / 实例名。

只读扫描：遍历 data_dir 下的文本类文件，报告含
  - Windows 盘符绝对路径（如 ``D:\\...``），
  - 实例名路径片段（如 ``instance/data-*`` / ``instance\\data-*``）
的文件，按目录汇总，便于跨机复制前核对可移植性。

用法::

    python src/tools/abs_scan.py --data-dir instance/data-deepseek-v4.1-flash-v3
    python src/tools/abs_scan.py --data-dir <dir> --out temp/abs_scan.txt

``--data-dir`` 遵循顶层约定：绝对路径，或相对**项目根**（src/）的路径。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 文本类文件（可能含路径字符串）
TEXT_EXTS = {".json", ".jsonl", ".md", ".txt", ".log", ".html", ".htm",
             ".csv", ".yaml", ".yml", ".js", ".css"}

# 盘符绝对路径：X:\...（须至少含一个反斜杠片段；用于**已解码**字符串）
_DRIVE = re.compile(r"[A-Za-z]:\\(?:[^\\\n\r\t\"']+\\)*[^\\\n\r\t\"']*")
# 实例名路径片段（原始文本即可）
_INSTANCE = re.compile(r"(?<![A-Za-z])instance[\\/]data-")


def _walk_strings(obj):
    """递归产出 JSON 结构中的所有字符串值。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _json_values(path: str):
    """解析 JSON/JSONL，产出（已解码）字符串；失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            if path.lower().endswith(".jsonl"):
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    yield from _walk_strings(json.loads(line))
            else:
                yield from _walk_strings(json.load(f))
    except (OSError, ValueError):
        return


def _iter_files(root: str, exts: set[str], max_bytes: int):
    """产出 (path, decoded_strings or None)。JSON 类给解码后的字符串列表。"""
    for base, _dirs, files in os.walk(root):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in exts:
                continue
            p = os.path.join(base, fn)
            try:
                if os.path.getsize(p) > max_bytes:
                    continue
            except OSError:
                continue
            if ext in (".json", ".jsonl"):
                vals = list(_json_values(p))
                if vals:  # 解析成功：只信解码后的值（避开 \n 转义误报）
                    yield p, vals
                    continue
                # 解析失败 → 回退原文扫描
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    yield p, [f.read()]
            except OSError:
                continue


def scan(data_dir: str, exts: set[str] | None = None,
         max_bytes: int = 50_000_000) -> list[dict]:
    """返回 [{file, rel, drive[], instance[]}, ...]（仅命中的文件）。"""
    exts = exts or TEXT_EXTS
    hits: list[dict] = []
    for p, values in _iter_files(data_dir, exts, max_bytes):
        blobs = [v for v in values if isinstance(v, str)]
        drive: list[str] = []
        inst: list[str] = []
        for v in blobs:
            drive += _DRIVE.findall(v)
            inst += _INSTANCE.findall(v)
        if drive or inst:
            hits.append({
                "file": p,
                "rel": os.path.relpath(p, data_dir),
                "drive": drive,
                "instance": inst,
            })
    return hits


def _fmt(hits: list[dict]) -> str:
    lines = [f"# abs_scan — 绝对路径/实例名审计",
             f"\n命中文件 {len(hits)} 个"]
    for h in hits:
        bits = []
        if h["drive"]:
            bits.append(f"盘符×{len(h['drive'])}")
        if h["instance"]:
            bits.append(f"实例名×{len(h['instance'])}")
        lines.append(f"  {h['rel']}  ({', '.join(bits)})")
        for d in dict.fromkeys(h["drive"]):
            lines.append(f"      驱动路径: {d[:160]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="审计实例目录中的绝对路径/实例名（只读）")
    ap.add_argument("--data-dir", required=True,
                    help="实例目录（绝对，或相对项目根 src/）")
    ap.add_argument("--out", default=None, help="输出文件（默认仅 stdout）")
    ap.add_argument("--ext", nargs="*", default=None,
                    help="限定扩展名（含点，如 .json .log）")
    args = ap.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dd = args.data_dir if os.path.isabs(args.data_dir) \
        else os.path.normpath(os.path.join(root, args.data_dir))
    if not os.path.isdir(dd):
        print(f"目录不存在: {dd}", file=sys.stderr)
        return 1

    exts = {e if e.startswith(".") else "." + e for e in args.ext} \
        if args.ext else None
    txt = _fmt(scan(dd, exts))
    print(txt)
    if args.out:
        out = args.out if os.path.isabs(args.out) \
            else os.path.normpath(os.path.join(root, args.out))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"\n留存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
