"""工作区数据模型。

`PaperWorkspace` 是系统的单一真相源，保存论文写作过程中的全部共享状态。
所有智能体只通过工作区读写来协作，不直接互相传递大段状态。

设计要点：
- 纯 dataclass，零外部依赖，便于序列化与测试。
- 提供 to_dict / from_dict 以支持 JSON 持久化（见 store.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InputMode(str, Enum):
    """输入模式（Req 1）。"""

    DRAFT_REVISION = "draft_revision"  # 用户提供初稿，做修订润色
    GENERATION = "generation"          # 用户提供主题+数据，从零生成


class OutputFormat(str, Enum):
    """输出格式（Req 10）。"""

    MARKDOWN = "markdown"
    LATEX = "latex"
    DOCX = "docx"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class ScoringDimension(str, Enum):
    """评审评分维度（Req 7.1）。"""

    LOGIC = "logic"            # 逻辑性
    NOVELTY = "novelty"        # 新颖性
    SUFFICIENCY = "sufficiency"  # 论证充分性
    LANGUAGE = "language"      # 语言质量


class ParseStatus(str, Enum):
    """结构化解析/评审来源状态（升级 Req 1）。

    用于标记一条 ReviewRecord（或结构化解析结果）的真实来源，
    决定其能否触发编排器的 `quality_met` 判定。
    """

    PARSED = "parsed"                 # 成功解析为合法结构
    MOCK_FALLBACK = "mock_fallback"   # 识别为 Mock/测试 provider 的非结构化输出回退
    FAILED = "failed"                 # 生产 provider 多次尝试后仍无法解析


@dataclass
class OutlineNode:
    """论文大纲中的一个节点（章节）。"""

    section_id: str
    title: str
    order: int
    summary_hint: str = ""  # 该章节应涵盖内容的提示
    # Artifact evidence that this section must/ may consume.  Empty lists preserve
    # compatibility with revision projects that do not have a ResearchArtifact.
    required_evidence_ids: list[str] = field(default_factory=list)
    allowed_evidence_ids: list[str] = field(default_factory=list)


@dataclass
class TaskItem:
    """任务清单中的一个条目（Req 2）。"""

    id: str
    description: str
    section_ref: str | None = None
    needs_retrieval: bool = False  # Req 2.5：标记需要文献检索
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class ReferenceEntry:
    """单条文献记录（Req 3.3 / 4）。

    source_id（DOI / arXiv id 等）是真实性核验的依据，必须非空。
    verified 标记是否通过真实性核验，只有 True 才可被写作智能体引用。
    abstract 为摘要（部分来源提供），供写作与引用恰当性判断使用。

    Round 6：``pdf_url`` 与 ``abstract_sections`` 给写作期 ``fetch_paper_section``
    工具按需取材——之前只到 abstract 整段，写 Related Work 经常只是同义改写。
    - ``pdf_url``：可获取的 PDF 全文 URL（如 OpenAlex 的 oa_url）；空表示无。
    - ``abstract_sections``：abstract 的结构化分段（``section_name -> text``，
       如 ``{"motivation": "...", "method": "...", "results": "..."}``）；若来源
       未提供分段则保持空 dict，工具回退到对 ``abstract`` 整段切片。
    """

    id: str
    title: str
    authors: list[str]
    year: int | None
    source_id: str
    source: str = ""          # 来源，如 "arxiv" / "semantic_scholar" / "openalex"
    verified: bool = False
    abstract: str = ""
    pdf_url: str = ""
    abstract_sections: dict[str, str] = field(default_factory=dict)
    # Citation markers used by an imported source document (for example "8").
    # They are aliases of this verified record, not globally stable identifiers.
    citation_aliases: list[str] = field(default_factory=list)
    # Round 9：被引论文的**正文全文**（可选）。默认空——为空时 grounding 仍只到
    # abstract 层（行为不变）；非空时引用忠实性审计的 grounding 会从正文按段落取材，
    # 消解「细节声明在正文、abstract 里没有 → 被迫 cannot_verify」的假阴。
    # 由可选的富化步骤（reference_enrichment）从 pdf_url 抓取解析后填充；旧 JSON
    # 无此键 → from_dict 回落默认空串（向后兼容）。
    full_text: str = ""


@dataclass
class SectionDraft:
    """某章节的草稿（Req 5）。

    cited_reference_ids 中的 id 必须都存在于工作区的 verified_references 中
    （正确性 Property 1）。
    """

    section_id: str
    title: str
    content: str = ""
    cited_reference_ids: list[str] = field(default_factory=list)
    artifact_hash: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    # Each item is ``{"claim": str, "evidence_ids": [str], "kind": str}``.
    claim_manifest: list[dict] = field(default_factory=list)


@dataclass
class FigureRecord:
    """图表及其说明（Req 6）。

    source_experiment_id / rendered_from_data 为数据出图（Req 7）新增的可选字段：
    - ``source_experiment_id``：本图来源的实验 id（数据出图时填充，否则为空）。
    - ``rendered_from_data``：True 表示由真实实验数据渲染而来（区别于 LLM 文字图题）。
    二者均带默认值，保证旧 JSON（无这两个键）反序列化向后兼容（Req 9.4）。
    """

    figure_id: str
    data_ref: str             # 指向图表数据/图像文件
    caption: str = ""         # 图表说明；为空表示需由系统生成
    caption_provided_by_user: bool = False
    source_experiment_id: str = ""   # 数据出图来源实验 id（Req 7.1）
    rendered_from_data: bool = False  # 是否由真实数据渲染（Req 7.1）


@dataclass
class ReviewRecord:
    """一轮评审记录（Req 7）。

    section_feedback: 章节级反馈（section_id -> 该章节需改进之处），
    用于驱动写作智能体的定位式局部修改（Req 5.8）。

    parse_status: 标记本条记录的来源（升级 Req 1）。仅当 `PARSED` 时
    才可触发编排器达标判定；`FAILED` 表示生产解析失败、`MOCK_FALLBACK`
    表示测试/Mock provider 的确定性回退。
    unparsed_reason: 当 `parse_status == FAILED` 时标识失败类别（非空）。
    """

    iteration: int
    scores: dict[ScoringDimension, float] = field(default_factory=dict)
    suggestions: dict[ScoringDimension, str] = field(default_factory=dict)
    section_feedback: dict[str, str] = field(default_factory=dict)
    parse_status: ParseStatus = ParseStatus.PARSED
    unparsed_reason: str = ""


@dataclass
class AdversarialReviewRecord:
    """对抗式评审记录（Round 4 升级：打破自评 reward-hack）。

    与 ``ReviewRecord`` 互补：主审打分（主观维度，易被 LLM 自评 reward-hack），
    对抗式评审持默认 reject 立场列出具体 weakness，必须二者**联合**通过才算
    质量达标——单独一方通过不足以触发 ``quality_met``。

    decision: "reject" | "borderline" | "accept"；只有 ``accept`` 才视为通过。
    weaknesses: 每条含 ``section_id / category / severity / issue / suggested_fix``。
    critical_count: weaknesses 中 ``severity == "critical"`` 的条数。
    parse_status: 与 ``ReviewRecord`` 同义；仅 ``PARSED`` 才作为达标判据之一。
    """

    iteration: int
    decision: str = "reject"  # reject | borderline | accept
    weaknesses: list[dict] = field(default_factory=list)
    critical_count: int = 0
    parse_status: ParseStatus = ParseStatus.PARSED
    unparsed_reason: str = ""


@dataclass
class SectionEdit:
    """章节级精确编辑意图（由 edit_section 工具产出，升级 Req 6）。

    anchor 为定位锚文本；mode 限定为 {replace, insert_after, insert_before}。
    由 WritingAgent 汇聚为 WorkspaceMutation 落盘，工具本身不直接写工作区。
    """

    section_id: str
    anchor: str
    replacement: str
    mode: str = "replace"   # replace | insert_after | insert_before


@dataclass
class StreamChunk:
    """流式增量（升级 Req 5）。kind ∈ {"content", "thinking"}。"""

    kind: str
    text: str


@dataclass
class RetryPolicy:
    """LLM 健壮性重试策略（升级 Req 4）。

    约束：max_retries >= 0，base_backoff <= max_backoff。
    """

    max_retries: int = 3
    base_backoff: float = 1.0        # 指数退避基数（秒）
    max_backoff: float = 30.0
    jitter: float = 0.25             # 抖动比例，避免重试风暴
    respect_retry_after: bool = True # 429 优先采用响应头 Retry-After


@dataclass
class PaperWorkspace:
    """论文工作区：单一真相源（Req 9）。"""

    workspace_id: str
    input_mode: InputMode
    output_format: OutputFormat = OutputFormat.MARKDOWN
    original_draft: str | None = None       # 草稿修订模式输入
    topic_background: str | None = None      # 从零生成模式输入
    outline: list[OutlineNode] = field(default_factory=list)
    task_checklist: list[TaskItem] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)
    verified_references: list[ReferenceEntry] = field(default_factory=list)
    section_drafts: dict[str, SectionDraft] = field(default_factory=dict)
    section_summaries: dict[str, str] = field(default_factory=dict)
    # 草稿修订模式：初稿按章节切分后的原文（section_id -> 该章节初稿正文）。
    # 由 PlanAgent 在草稿修订模式填充，供 WritingAgent 作为修订基底——避免把
    # 整篇初稿压成 300 字片段后从零重写（初稿内容必须被保留）。
    draft_sections: dict[str, str] = field(default_factory=dict)
    figures: list[FigureRecord] = field(default_factory=list)
    review_records: list[ReviewRecord] = field(default_factory=list)
    # 对抗式评审记录（Round 4）：与 review_records 配对，每轮各一条，达标需联合通过。
    adversarial_records: list[AdversarialReviewRecord] = field(default_factory=list)
    iteration: int = 0
    citation_audit: list[dict] = field(default_factory=list)  # 引用审计发现（Req 11）
    quality_report: list[dict] = field(default_factory=list)  # 确定性质量检查发现
    # Candidate sections rejected by ArtifactCommitGate.  Keeping this separate
    # from section_drafts guarantees that rejected facts can never be exported.
    artifact_violations: list[dict] = field(default_factory=list)
    # 引用忠实性审计报告（citation-faithfulness-audit Req 5.3/5.4）：每条 finding 一个 dict。
    citation_faithfulness: list[dict] = field(default_factory=list)
    # 检索阶段是否已完成（#8）：用此标志而非「库里是否有文献」判定，避免审计
    # 已塞入若干文献后检索阶段被整体跳过、草稿修订模式欠补检索。
    retrieval_completed: bool = False
    profile: dict = field(default_factory=dict)  # 论文档案 / steering 偏好
    # Round 7：用户提供的真实研究内容（结构化）。None 表示无 artifact——
    # GENERATION 模式无 artifact 时会显式降级为「LLM 推断版」。
    artifact: "ResearchArtifact | None" = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    # --- 便捷查询方法 ---

    def verified_reference_ids(self) -> set[str]:
        """返回所有已验证文献的 id 集合。"""
        ids: set[str] = set()
        for reference in self.verified_references:
            if not reference.verified:
                continue
            ids.add(reference.id)
            if reference.source_id:
                ids.add(reference.source_id)
            ids.update(reference.citation_aliases)
        return ids

    def ordered_sections(self) -> list[OutlineNode]:
        """按 order 返回大纲章节。"""
        return sorted(self.outline, key=lambda n: n.order)

    # --- 序列化 ---

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "input_mode": self.input_mode.value,
            "output_format": self.output_format.value,
            "original_draft": self.original_draft,
            "topic_background": self.topic_background,
            "outline": [vars(n) for n in self.outline],
            "task_checklist": [
                {**vars(t), "status": t.status.value} for t in self.task_checklist
            ],
            "glossary": dict(self.glossary),
            "verified_references": [vars(r) for r in self.verified_references],
            "section_drafts": {
                sid: vars(d) for sid, d in self.section_drafts.items()
            },
            "section_summaries": dict(self.section_summaries),
            "draft_sections": dict(self.draft_sections),
            "figures": [vars(f) for f in self.figures],
            "review_records": [
                {
                    "iteration": rr.iteration,
                    "scores": {k.value: v for k, v in rr.scores.items()},
                    "suggestions": {k.value: v for k, v in rr.suggestions.items()},
                    "section_feedback": dict(rr.section_feedback),
                    "parse_status": rr.parse_status.value,
                    "unparsed_reason": rr.unparsed_reason,
                }
                for rr in self.review_records
            ],
            "adversarial_records": [
                {
                    "iteration": ar.iteration,
                    "decision": ar.decision,
                    "weaknesses": list(ar.weaknesses),
                    "critical_count": ar.critical_count,
                    "parse_status": ar.parse_status.value,
                    "unparsed_reason": ar.unparsed_reason,
                }
                for ar in self.adversarial_records
            ],
            "iteration": self.iteration,
            "citation_audit": list(self.citation_audit),
            "quality_report": list(self.quality_report),
            "artifact_violations": list(self.artifact_violations),
            "citation_faithfulness": list(self.citation_faithfulness),
            "retrieval_completed": self.retrieval_completed,
            "profile": dict(self.profile),
            # Round 7：artifact 序列化（None 时省略键，老版本 JSON 反序列化时回落到 None）。
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PaperWorkspace":
        # Round 7：artifact 反序列化。延迟导入避免 research_artifact ↔ models 循环依赖。
        artifact_raw = data.get("artifact")
        artifact_obj = None
        if artifact_raw:
            from paper_agent.workspace.research_artifact import ResearchArtifact

            artifact_obj = ResearchArtifact.from_dict(artifact_raw)
        return cls(
            workspace_id=data["workspace_id"],
            input_mode=InputMode(data["input_mode"]),
            output_format=OutputFormat(data.get("output_format", "markdown")),
            original_draft=data.get("original_draft"),
            topic_background=data.get("topic_background"),
            outline=[OutlineNode(**n) for n in data.get("outline", [])],
            task_checklist=[
                TaskItem(
                    id=t["id"],
                    description=t["description"],
                    section_ref=t.get("section_ref"),
                    needs_retrieval=t.get("needs_retrieval", False),
                    status=TaskStatus(t.get("status", "pending")),
                )
                for t in data.get("task_checklist", [])
            ],
            glossary=dict(data.get("glossary", {})),
            verified_references=[
                ReferenceEntry(**r) for r in data.get("verified_references", [])
            ],
            section_drafts={
                sid: SectionDraft(**d)
                for sid, d in data.get("section_drafts", {}).items()
            },
            section_summaries=dict(data.get("section_summaries", {})),
            draft_sections=dict(data.get("draft_sections", {})),
            figures=[
                FigureRecord(
                    **{
                        k: v
                        for k, v in f.items()
                        if k in {fld.name for fld in fields(FigureRecord)}
                    }
                )
                for f in data.get("figures", [])
            ],
            review_records=[
                ReviewRecord(
                    iteration=rr["iteration"],
                    scores={
                        ScoringDimension(k): v for k, v in rr.get("scores", {}).items()
                    },
                    suggestions={
                        ScoringDimension(k): v
                        for k, v in rr.get("suggestions", {}).items()
                    },
                    section_feedback=dict(rr.get("section_feedback", {})),
                    parse_status=ParseStatus(rr.get("parse_status", "parsed")),
                    unparsed_reason=rr.get("unparsed_reason", ""),
                )
                for rr in data.get("review_records", [])
            ],
            adversarial_records=[
                AdversarialReviewRecord(
                    iteration=ar["iteration"],
                    decision=ar.get("decision", "reject"),
                    weaknesses=list(ar.get("weaknesses", [])),
                    critical_count=int(ar.get("critical_count", 0)),
                    parse_status=ParseStatus(ar.get("parse_status", "parsed")),
                    unparsed_reason=ar.get("unparsed_reason", ""),
                )
                for ar in data.get("adversarial_records", [])
            ],
            iteration=data.get("iteration", 0),
            citation_audit=list(data.get("citation_audit", [])),
            quality_report=list(data.get("quality_report", [])),
            artifact_violations=list(data.get("artifact_violations", [])),
            citation_faithfulness=list(data.get("citation_faithfulness", [])),
            retrieval_completed=bool(data.get("retrieval_completed", False)),
            profile=dict(data.get("profile", {})),
            artifact=artifact_obj,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
