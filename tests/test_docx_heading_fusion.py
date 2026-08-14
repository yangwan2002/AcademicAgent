"""标题吞并：LLM 返回 strip 过的正文时，splice 不得把标题与正文融成一段。

真机复现（2026-08-12，润色 4.2 节）：一次「保守润色」后 4.2 的标题与正文首句
合并成一段（且仍是 Heading 3 样式），4.3 整个标题消失、其下 9 段并入 4.2，
文档标题数 15 → 14，而 gate 报 passed=True。

根因是三层叠加，本文件逐层钉住：

- C-1 `body_of` 返回的正文**以 `\\n` 开头且以 `\\n` 结尾**（`body_start` 紧贴标题
  文字之后）。LLM 几乎必然返回 strip 过的文本，`splice` 拼接时首尾各丢一个换行，
  于是 `### 4.2 標題` 吞掉正文首句、正文末句吞掉 `### 4.3 標題`。
- C-2 `_heading_levels` 只认 md/markdown，漏了 docx → `PolishGate` 的标题结构
  检查对 docx 是**空操作**。同一处损坏，`.md` 会被拦、`.docx` 放行。
- C-3 gate 在 `splice` **之前**判定，跨边界融合结构上不可见 → 写回层需自守。
"""
from __future__ import annotations

import pytest

from paper_agent.gates.diff_gate import PolishGate, _heading_levels
from paper_agent.session.docx_document import (
    DocxDocumentError,
    apply_text,
    project_text,
)
from paper_agent.session.section_index import SectionIndex

docx = pytest.importorskip("docx")
from docx.shared import Pt


@pytest.fixture
def paper(tmp_path):
    """两个同级小节，第二个紧跟在第一个正文之后 —— 融合就发生在这个边界上。"""
    path = str(tmp_path / "论文.docx")
    doc = docx.Document()
    doc.add_heading("4 实验", 2)
    doc.add_heading("4.2 主共视支撑域预测结果", 3)
    doc.add_paragraph("为分析SAGENet的预测能力，划分为三组，结果如表1所示。")
    doc.add_paragraph("由表1可见，mIoU 随共视比例增加而提高，达 0.87。")
    doc.add_heading("4.3 掩膜约束下的匹配分析", 3)
    doc.add_paragraph("主共视区域预测的最终目的是提高匹配概率。")
    for p in doc.paragraphs:
        for r in p.runs:
            r.font.size = Pt(12)
    doc.save(path)
    return path


def _headings(path):
    return [p.text for p in docx.Document(path).paragraphs
            if p.style.name.startswith(("Heading", "标题"))]


# ---------------- C-1 · splice 的换行契约 ----------------

def test_body_of_carries_boundary_newlines(paper):
    """前提确认：body 首尾**本来就带**换行，故 strip 会破坏拼接。"""
    idx = SectionIndex(project_text(paper), ".docx")
    body = idx.body_of(idx.find("4.2 主共视支撑域预测结果"))
    assert body.startswith("\n")
    assert body.endswith("\n")


@pytest.mark.parametrize("mangle", [
    lambda s: s.strip(),        # LLM 最常见的形态：两端都 strip
    lambda s: s.lstrip(),       # 只丢首部 → 标题吞正文
    lambda s: s.rstrip(),       # 只丢尾部 → 正文吞下一个标题
])
def test_splice_restores_boundary_newlines(paper, mangle):
    """候选文本无论怎么 strip，splice 后标题必须仍是独立的行。"""
    text = project_text(paper)
    idx = SectionIndex(text, ".docx")
    sec = idx.find("4.2 主共视支撑域预测结果")
    new = idx.splice(sec, mangle(idx.body_of(sec)))

    lines = new.split("\n")
    assert "### 4.2 主共视支撑域预测结果" in lines
    assert "### 4.3 掩膜约束下的匹配分析" in lines
    # 标题行不得夹带正文
    assert _heading_levels(new, ".docx") == _heading_levels(text, ".docx")


def test_identity_splice_is_byte_exact(paper):
    """恒等 splice 不因规范化而改变全文（否则会造出虚假 diff）。"""
    text = project_text(paper)
    idx = SectionIndex(text, ".docx")
    sec = idx.find("4.2 主共视支撑域预测结果")
    assert idx.splice(sec, idx.body_of(sec)) == text


def test_fusion_does_not_reach_the_document(paper):
    """端到端：strip 过的候选写回 docx 后，标题数不变、`###` 不成为字面文字。"""
    idx = SectionIndex(project_text(paper), ".docx")
    sec = idx.find("4.2 主共视支撑域预测结果")
    before = _headings(paper)

    apply_text(paper, idx.splice(sec, idx.body_of(sec).strip()))

    assert _headings(paper) == before
    assert "4.3 掩膜约束下的匹配分析" in _headings(paper)
    for p in docx.Document(paper).paragraphs:
        assert "###" not in p.text, f"投影标记漏成字面文字：{p.text[:60]}"


# ---------------- C-2 · gate 要认 docx ----------------

def test_heading_levels_handles_docx():
    """docx 走投影文本，标题形态与 md 一致 —— 层级序列必须一样。"""
    t = "### 4.2 标题\n正文一段。\n### 4.3 另一标题\n正文二段。"
    assert _heading_levels(t, ".docx") == _heading_levels(t, "md") == [3, 3]


def test_polish_gate_catches_heading_loss_in_docx():
    """润色父章节时，子标题消失要在 docx 下也被拦（此前只有 .md 会拦）。

    父章节（`4 实验`）的 body **含**子标题，故这类丢失落在 gate 视野内 ——
    这正是 C-2 真正生效的场景。
    """
    original = "\n### 4.2 预测结果\n正文一。\n### 4.3 匹配分析\n正文二。"
    losing = "\n### 4.2 预测结果\n正文一。\n正文二。"        # 4.3 整个没了
    result = PolishGate(".docx").check(original, losing)
    assert not result.passed
    assert any("标题" in v for v in result.violations)


def test_gate_cannot_see_fusion_that_keeps_the_prefix():
    """如实钉住 gate 的局限：标题保住 `#` 前缀却吞进正文时，层级序列不变。

    这类融合只能靠 `splice` 的换行规范化根治，不能指望 gate。写下来是为了
    避免以后误以为「gate 过了就没有融合」。
    """
    original = "### 4.2 标题\n正文。"
    fused = "### 4.2 标题正文。"
    assert _heading_levels(original, ".docx") == _heading_levels(fused, ".docx")


# ---------------- C-3 · 写回层自守 ----------------

def test_apply_text_rejects_heading_loss(paper):
    """丢标题是损坏 → 抛错且**文件不被改动**。"""
    text = project_text(paper)
    losing = text.replace("### 4.3 掩膜约束下的匹配分析\n", "")
    before = _headings(paper)

    with pytest.raises(DocxDocumentError, match="标题"):
        apply_text(paper, losing)

    assert _headings(paper) == before, "断言失败时不得留下半截写入"


def test_apply_text_allows_heading_addition(paper):
    """新增标题是意图（add_section），必须放行。"""
    text = project_text(paper)
    added = text.replace(
        "### 4.3 掩膜约束下的匹配分析",
        "### 4.25 新增小节\n新增小节正文。\n### 4.3 掩膜约束下的匹配分析",
    )
    apply_text(paper, added)
    assert "4.25 新增小节" in _headings(paper)
    assert "4.3 掩膜约束下的匹配分析" in _headings(paper)


def test_apply_text_allows_heading_rename(paper):
    """改标题文字（层级不变、不减少）应放行 —— 判据是丢失，不是逐字相同。"""
    text = project_text(paper)
    renamed = text.replace("4.3 掩膜约束下的匹配分析", "4.3 掩膜约束下的匹配与几何验证")
    apply_text(paper, renamed)
    assert "4.3 掩膜约束下的匹配与几何验证" in _headings(paper)
    assert len(_headings(paper)) == 3
