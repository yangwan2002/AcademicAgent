"""引用解析：从初稿中抽取参考文献列表与正文内引用标记。

- 参考文献列表：优先用结构化解析（经 ``StructuredParser`` 统一治理，含重试与
  ParseStatus）把 LLM 输出解析为结构化字段；LLM 不可用或解析失败（MOCK_FALLBACK
  / FAILED）时回退到正则的尽力解析（#5：此前直接 ``complete`` + ``extract_json``
  绕过了 StructuredParser，丢了重试与来源标记）。
- 正文引用标记：用正则抽取形如 [1]、[2,3]、[1-3] 的数字引用编号。

供引用审计智能体做存在性/元数据/对应关系检查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import ParseStatus, ReferenceEntry

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_INTEXT = re.compile(r"\[(\d+(?:\s*[,\-]\s*\d+)*)\]")
_REF_HEADER = re.compile(
    r"(参考文献|references|bibliography)", re.IGNORECASE
)


@dataclass
class ParsedCitations:
    references: list[ReferenceEntry] = field(default_factory=list)
    in_text_keys: list[str] = field(default_factory=list)  # 去重后的引用编号


class CitationParser:
    def __init__(
        self,
        llm: LLMProvider | None = None,
        parser: StructuredParser | None = None,
    ) -> None:
        self._llm = llm
        # #5：优先用注入的结构化解析器（统一治理重试/ParseStatus）；
        # 缺省时基于 llm 自建（保持向后兼容）。
        self._parser = parser if parser is not None else (
            StructuredParser(llm) if llm is not None else None
        )

    def parse(self, draft: str) -> ParsedCitations:
        ref_section = self._locate_reference_section(draft)
        references = self._parse_references(ref_section) if ref_section else []
        in_text = self._parse_in_text(draft)
        return ParsedCitations(references=references, in_text_keys=in_text)

    # --- 正文引用编号 ---

    @staticmethod
    def _parse_in_text(draft: str) -> list[str]:
        keys: list[str] = []
        for m in _INTEXT.finditer(draft):
            group = m.group(1)
            # 展开 [1-3] 区间与 [1,2] 列表。
            for part in re.split(r"\s*,\s*", group):
                if "-" in part:
                    try:
                        lo, hi = (int(x) for x in part.split("-", 1))
                        keys.extend(str(n) for n in range(lo, hi + 1))
                    except ValueError:
                        continue
                else:
                    keys.append(part.strip())
        # 去重并保持顺序。
        seen: set[str] = set()
        result = []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                result.append(k)
        return result

    # --- 参考文献列表 ---

    @staticmethod
    def _locate_reference_section(draft: str) -> str:
        """返回参考文献标题之后的文本；找不到则返回空串。"""
        matches = list(_REF_HEADER.finditer(draft))
        if not matches:
            return ""
        # 取最后一个出现的"参考文献"标题（通常在文末）。
        start = matches[-1].end()
        return draft[start:].strip()

    def _parse_references(self, ref_section: str) -> list[ReferenceEntry]:
        if self._parser is not None:
            llm_refs = self._parse_references_llm(ref_section)
            if llm_refs:
                return llm_refs
        return self._parse_references_regex(ref_section)

    def _parse_references_llm(self, ref_section: str) -> list[ReferenceEntry]:
        # #5：经 StructuredParser 统一治理（重试、JSON 模式、ParseStatus）。
        # 仅 PARSED 才采用；MOCK_FALLBACK / FAILED 回退正则。
        outcome = self._parser.request_json(
            templates.parse_references(ref_section=ref_section),
            required_keys=("references",),
        )
        if outcome.status is not ParseStatus.PARSED or outcome.data is None:
            return []
        items = outcome.data.get("references")
        if not isinstance(items, list):
            return []
        refs: list[ReferenceEntry] = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title", "")).strip()
            if not title:
                continue
            year = it.get("year")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None
            authors = it.get("authors") or []
            if isinstance(authors, str):
                authors = [authors]
            refs.append(
                ReferenceEntry(
                    id=f"draft_ref_{it.get('index', i + 1)}",
                    title=title,
                    authors=[str(a) for a in authors],
                    year=year,
                    source_id=str(it.get("doi") or ""),
                    source="draft",
                )
            )
        return refs

    @staticmethod
    def _parse_references_regex(ref_section: str) -> list[ReferenceEntry]:
        """正则尽力解析：按行/编号切分，提取年份与 DOI。"""
        refs: list[ReferenceEntry] = []
        # 按形如 "1." / "[1]" 的编号或换行切分。
        chunks = re.split(r"\n\s*(?:\[\d+\]|\d+\.)\s*", "\n" + ref_section)
        idx = 0
        for chunk in chunks:
            text = " ".join(chunk.split())
            if len(text) < 8:
                continue
            idx += 1
            year_m = _YEAR.search(text)
            doi_m = _DOI.search(text)
            refs.append(
                ReferenceEntry(
                    id=f"draft_ref_{idx}",
                    title=text[:160],
                    authors=[],
                    year=int(year_m.group(0)) if year_m else None,
                    source_id=doi_m.group(0) if doi_m else "",
                    source="draft",
                )
            )
        return refs
