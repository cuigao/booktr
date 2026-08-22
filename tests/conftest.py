"""共享测试基建：隔离 data-dir、可编程 FakeLLM、样例站点构造。"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booktr.config import Config


def _default_data() -> dict:
    """测试专用配置：关闭无关功能，简化断言，全部落在 tmp data-dir。"""
    return {
        "source_dir": "site",
        "output_dir": "out",
        "work_dir": "work",
        "llm": {
            "provider": "mock",
            "max_repair": 3,
            "max_history_segments": 50,
            "summary_enabled": False,
            "auto_retranslate": True,
            "auto_retranslate_attempts": 1,
        },
        "style": {"rules_enabled": False, "exemplar_enabled": False},
        "tm": {"enabled": False},
        "llm_logs": {"dir": "work/llm_logs", "auto_export": False},
        "planner": {"context": {"plan_predecessors": 0, "time_predecessors": 0, "link_predecessors": 0}},
    }


@pytest.fixture
def tmp_cfg(tmp_path):
    """构造指向临时数据根（tmp_path）的 Config，绝不触碰主 src/data/。"""
    from booktr.config import find_project_root

    root = find_project_root()
    cfg = Config(root=root, data=_default_data(), data_dir=str(tmp_path))
    return cfg


class FakeLLM:
    """可编程 fake client，实现 chat / chat_multi。

    用法：
      fake = FakeLLM.default()                # 恒等翻译（正常 JSON）
      fake = FakeLLM(sequence=[...])          # 按调用次数依次返回
      fake = FakeLLM(responder=fn(user))      # 按 user 内容决定响应
    """

    def __init__(self, sequence=None, responder=None):
        self.sequence = list(sequence) if sequence else None
        self.responder = responder
        self.calls = 0
        self.last_user = ""

    @classmethod
    def default(cls):
        """恒等翻译：把源文本原样作为译文，正常 JSON（模拟 mock 行为）。"""

        def _resp(user):
            src = _extract_text(user)
            return _ok_json(src)

        return cls(responder=_resp)

    def chat(self, system, user, temperature=None, tag="chat"):
        self.calls += 1
        self.last_user = user
        return self._next(system, user)

    def chat_multi(self, messages, temperature=None, tag="chat_multi",
                   task_id="", context_id=""):
        self.calls += 1
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.last_user = last
        sysp = next((m["content"] for m in messages if m["role"] == "system"), "")
        return self._next(sysp, last)

    def _next(self, system, user):
        if self.sequence is not None:
            idx = min(self.calls - 1, len(self.sequence) - 1)
            return self.sequence[idx]
        if self.responder:
            return self.responder(user)
        return _ok_json(_extract_text(user))


def _extract_text(user: str) -> str:
    """从 user prompt 中抓取待翻译文本（### 待翻译文本 之后）。"""
    marker = "### 待翻译文本"
    if marker in user:
        return user.split(marker, 1)[1].strip()
    marker2 = "## 待翻译文本"
    if marker2 in user:
        return user.split(marker2, 1)[1].strip()
    return user


def _ok_json(translation, confidence=1.0, needs_human=False, untrusted=False):
    return json.dumps(
        {
            "translation": translation,
            "confidence": confidence,
            "glossary_conflicts": [],
            "notes": [],
            "needs_human": needs_human,
            "untrusted": untrusted,
        },
        ensure_ascii=False,
    )


SAMPLE_HTML = """<HTML><HEAD><TITLE>テストページ</TITLE></HEAD>
<BODY BGCOLOR="FFFFFF">
<CENTER><img src="icon/today.gif"><br></CENTER>
<blockquote>
<p>こんにちは。</p>
<p>今日はいい天気です。</p>
<p>明日は雨が降るでしょう。</p>
<p>それではまた。</p>
</blockquote>
<CENTER><A HREF="../index.html">HOME</A></CENTER>
</BODY></HTML>
"""


def write_sample_site(tmp_path) -> None:
    """写入最小站点镜像（含内联标签 + 中文/日文段）。"""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "page1.html").write_bytes(SAMPLE_HTML.encode("cp932"))


def read_json(path, default=None):
    from booktr import util

    return util.read_json(path, default)
