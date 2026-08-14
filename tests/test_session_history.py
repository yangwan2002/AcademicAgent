"""`FileBackedSession.history()` 与 git 输出编码（P-4 第二步）。

两件事，都是真机暴露的：

1. **历史读取通路**：真机问"我刚才改了什么""改过几次""上次润色的是哪一章"，三问
   全答"无法查看修改历史"。而每次 `write_working` 都打了 commit（`polish <id>`），
   信息一直在，缺的只是读出来的方法。

2. **git 输出按 UTF-8 解码**：`_git` 原来用 `text=True`，走平台默认编码。Windows
   上是 GBK，于是带中文的 commit message（`polish 1_引言`）读出来是乱码，章节 id
   对不上标题；更糟的是遇到解不开的字节会在 subprocess 的读取线程里抛
   `UnicodeDecodeError`，调用方拿到 `stdout=None` 而不是错误码。这条影响**所有**
   git 调用，不只是历史。
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.session.file_session import FileBackedSession

_DOC = "# 1 引言\n\n初始内容。\n\n# 2 方法\n\n方法内容。\n"

_needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="需要 git"
)


def _session(tmp_path, use_git=True):
    src = tmp_path / "paper.md"
    src.write_text(_DOC, encoding="utf-8")
    return FileBackedSession(
        str(src), workspace_root=str(tmp_path / "ws"), use_git=use_git
    )


def _edit(session, addition, message):
    session.write_working(session.read_working() + addition, message=message)


@_needs_git
def test_history_lists_edits_newest_first(tmp_path):
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    _edit(s, "\n补一句。\n", "polish 1_引言")
    _edit(s, "\n再补。\n", "rewrite 2_方法")

    hist = s.history()
    assert [h["message"] for h in hist] == ["rewrite 2_方法", "polish 1_引言"]


@_needs_git
def test_history_excludes_init_commit(tmp_path):
    """`init` 是建仓的空提交，不是用户编辑 —— 否则"改过几次"永远多算一次。"""
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    assert s.history() == []          # 只有 init 时，历史为空
    _edit(s, "\nx\n", "polish 1_引言")
    assert len(s.history()) == 1


@_needs_git
def test_history_preserves_chinese_message(tmp_path):
    """回归：GBK 解码会让 `polish 1_引言` 变乱码，章节 id 就对不上标题了。"""
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    _edit(s, "\nx\n", "polish 1_引言")
    assert s.history()[0]["message"] == "polish 1_引言"


@_needs_git
def test_history_reports_line_deltas(tmp_path):
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    _edit(s, "\n新增一行。\n", "polish 1_引言")
    entry = s.history()[0]
    # numstat 给的是行数（字段名如此），至少要记到有新增。
    assert entry["lines_added"] >= 1
    assert "lines_removed" in entry


@_needs_git
def test_history_respects_limit(tmp_path):
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    for i in range(5):
        _edit(s, f"\n第 {i} 行。\n", "polish 1_引言")
    assert len(s.history(limit=2)) == 2
    assert len(s.history(limit=99)) == 5


def test_history_empty_without_git(tmp_path):
    """降级到内存快照栈时没有历史可读，返回空 —— 由上层如实说明，不假装有。"""
    s = _session(tmp_path, use_git=False)
    _edit(s, "\nx\n", "polish 1_引言")
    assert s.history() == []
    assert s.git_enabled is False


@_needs_git
def test_history_is_read_only(tmp_path):
    """history() 不能动 working copy 或提交数。"""
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    _edit(s, "\nx\n", "polish 1_引言")
    before_text = s.read_working()
    before_n = len(s.history())

    s.history()
    s.history(limit=3)

    assert s.read_working() == before_text
    assert len(s.history()) == before_n


@_needs_git
def test_undo_still_works_after_encoding_change(tmp_path):
    """`_git` 换了解码方式，undo 走的是同一个原语 —— 确认没被带坏。"""
    s = _session(tmp_path)
    if not s.git_enabled:
        pytest.skip("git 不可用")
    _edit(s, "\n临时内容。\n", "polish 1_引言")
    assert "临时内容" in s.read_working()
    assert s.undo() is True
    assert "临时内容" not in s.read_working()
