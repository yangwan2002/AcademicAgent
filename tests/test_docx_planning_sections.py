"""docx 会话的章节可见性 + 系统修订文本不得弹窗。

真机实录（33 章的 docx 骨架，用户要求「完善引言，参考文献10篇」）：
  1. chat.py 列出 33 个章节
  2. 规划层收到 **0** 个章节 → 校验报「计划期找不到要修改的章节」
  3. 修复提示说「文档当前没有任何章节」（与 1 直接矛盾）
  4. 该提示被当成用户需求，逐条弹窗追问三次，其中一条是「。」
  5. 引言明明存在，却规划成 add_section，于是追问「这一章想达到什么目的」

根因两处，都在本文件锁住：
  A. `_snapshot_sections` 用 `_TEXT_EXT`（只有 tex/md）判定，漏 docx
  B. 系统撰写的修订文本被 LLM 抽成 requirement，无槽位认领 → 弹窗
"""

from __future__ import annotations

import tempfile

import pytest

from paper_agent.application.turn_service import _snapshot_sections
from paper_agent.session.file_session import FileBackedSession

def _docx(path, paras: list[tuple[str, int]]) -> None:
    """用 python-docx 造真实文件。

    手写 document.xml 不行：`_heading_level` 读的是 **样式名**（`Heading 2`），
    而样式名要靠 styles.xml 解析 styleId 得到。省掉 styles.xml 则全部段落
    level=0，测试失败原因与被测 bug 无关。
    """
    from docx import Document

    doc = Document()
    for text, level in paras:
        doc.add_heading(text, level=level) if level else doc.add_paragraph(text)
    doc.save(str(path))


@pytest.fixture
def skeleton_docx(tmp_path):
    p = tmp_path / "骨架.docx"
    _docx(p, [
        ("空地协同SLAM小论文写作骨架", 1),
        ("一、论文核心定位", 1),
        ("1 引言", 2),
        ("（1）空地协同SLAM的视觉关联需求", 3),
        ("2 相关工作", 2),
        ("3 所提方法", 2),
    ])
    return p


def _session(path):
    return FileBackedSession(str(path), workspace_root=tempfile.mkdtemp())


# ---------------------------------------------------- A. docx 章节可见性

def test_docx_sections_visible_to_planner(skeleton_docx):
    """规划层必须看到 docx 的章节；为 0 就会误判「找不到章节」。"""
    secs = _snapshot_sections(_session(skeleton_docx))
    assert len(secs) == 6, "docx 章节对规划层不可见 → 复现真机 bug"


def test_docx_intro_section_findable(skeleton_docx):
    """「完善引言」要能定位到已存在的引言，否则会误规划成 add_section。"""
    ids = [s.section_id for s in _snapshot_sections(_session(skeleton_docx))]
    assert any("引言" in i for i in ids), f"引言不可寻址，实际 ids={ids}"


def test_docx_matches_chat_display(skeleton_docx):
    """规划层与 chat.py 展示必须同源同口径 —— 真机上两者结论相反。"""
    from paper_agent.session.section_index import SectionIndex

    s = _session(skeleton_docx)
    displayed = SectionIndex(s.read_working(), s.ext).list()
    planner = _snapshot_sections(s)
    assert [x.id for x in displayed] == [x.section_id for x in planner]


def test_text_formats_unaffected(tmp_path):
    """修复不能回退 tex/md。"""
    p = tmp_path / "a.md"
    p.write_text("# 引言\n正文\n\n# 方法\n正文\n", encoding="utf-8")
    assert len(_snapshot_sections(_session(p))) == 2


def test_unsupported_ext_still_empty(tmp_path):
    """不支持分章的格式仍返回空，不是「什么都当章节」。"""
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    assert _snapshot_sections(_session(p)) == ()


# ------------------------------- B. 系统撰写的修订文本不得产生弹窗

def test_system_authored_reasons_cover_both_paths():
    from paper_agent.tasking.contracts import RevisionReason
    from paper_agent.tasking.planning_loop import _SYSTEM_AUTHORED_REASONS

    assert RevisionReason.VALIDATION_REPAIR in _SYSTEM_AUTHORED_REASONS
    assert RevisionReason.NEEDS_REPLAN in _SYSTEM_AUTHORED_REASONS
    # 用户原话必须仍然弹窗 —— 否则真丢用户要求
    assert RevisionReason.SUPPLEMENT not in _SYSTEM_AUTHORED_REASONS
    assert RevisionReason.USER_MODIFY not in _SYSTEM_AUTHORED_REASONS
    assert RevisionReason.INITIAL not in _SYSTEM_AUTHORED_REASONS


def test_repair_hint_mentions_sections_when_available():
    """有章节时提示要列出来，不能说「没有任何章节」。"""
    from paper_agent.tasking.planning_loop import _repair_hint

    class _I:
        code, message = "section_not_found", "计划期找不到要修改的章节："

    class _V:
        issues = [_I()]

    hint = _repair_hint(_V(), ["1_引言", "2_相关工作"])
    assert "文档当前没有任何章节" not in hint
    assert "1_引言" in hint
