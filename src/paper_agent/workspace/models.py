"""工作区数据模型（V2 精简版）。

历史说明：本模块曾承载 V1 的「单一真相源」大对象 `PaperWorkspace` 及其一整套
大纲/任务/评审类型。V2 的五意图不使用它们（Phase 0 清理已移除）。当前仅保留两个
被活跃代码广泛引用的中立值类型：

- `ReferenceEntry`：单条文献记录（检索 / 引用核验 / 文献工具共用）。
- `RetryPolicy`：LLM 健壮性重试策略（ResilientLLMProvider 使用）。

保持纯 dataclass、零外部依赖，便于序列化与测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReferenceEntry:
    """单条文献记录。

    source_id（DOI / arXiv id 等）是真实性核验的依据，必须非空。
    verified 标记是否通过真实性核验，只有 True 才可被写作智能体引用。
    abstract 为摘要（部分来源提供），供写作与引用恰当性判断使用。

    - ``pdf_url``：可获取的 PDF 全文 URL（如 OpenAlex 的 oa_url）；空表示无。
    - ``abstract_sections``：abstract 的结构化分段（``section_name -> text``）；
      来源未提供分段则保持空 dict。
    - ``citation_aliases``：导入源文档使用的引用标记（如 "8"）；是本记录的别名，
      非全局稳定标识。
    - ``full_text``：被引论文正文全文（可选）；为空时 grounding 仍只到 abstract 层。
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
    citation_aliases: list[str] = field(default_factory=list)
    full_text: str = ""


@dataclass
class RetryPolicy:
    """LLM 健壮性重试策略。

    约束：max_retries >= 0，base_backoff <= max_backoff。
    """

    max_retries: int = 3
    base_backoff: float = 1.0        # 指数退避基数（秒）
    max_backoff: float = 30.0
    jitter: float = 0.25             # 抖动比例，避免重试风暴
    respect_retry_after: bool = True  # 429 优先采用响应头 Retry-After
