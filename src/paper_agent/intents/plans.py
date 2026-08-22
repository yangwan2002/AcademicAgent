"""领域执行器使用的操作参数对象。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PolishPlan:
    section_id: str
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    source_text: str = ""


@dataclass
class ConvertPlan:
    target_ext: str
    presets: list[str] = field(default_factory=list)


@dataclass
class RewritePlan:
    section_id: str
    direction: str
    details: str = ""
    want_search: bool = False
    search_query: str = ""
    search_limit: int = 5
    citation_quota: object | None = None
    source_text: str = ""


@dataclass
class AddSectionPlan:
    title: str
    purpose: str
    position: str = "end"
    section_type: str = ""
    structure_kind: str = "desc"
    structure_desc: str = ""
    subsections: list[str] = field(default_factory=list)
    want_search: bool = False
    search_query: str = ""
    search_limit: int = 5
    citation_quota: object | None = None
    source_text: str = ""


FORMAT_ALIASES = {
    "docx": "docx",
    "word": "docx",
    "latex": "tex",
    "tex": "tex",
    ".tex": "tex",
    "markdown": "md",
    "md": "md",
    ".md": "md",
}


__all__ = [
    "AddSectionPlan",
    "ConvertPlan",
    "FORMAT_ALIASES",
    "PolishPlan",
    "RewritePlan",
]
