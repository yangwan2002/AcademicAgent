"""运行时配置。

从环境变量读取 provider 配置；不涉及 v1 那 200+ 行 feature flag 集市。
所有字段可选（有合理默认），启动即用。

设计说明：
- ``Config`` 是 provider 工厂（``providers/factory.py``）依赖的配置对象，
  字段覆盖 writer / reviewer / vlm 三套 LLM 端点与检索源。
- ``reviewer_*`` 一组字段用于给「校验者」配置独立模型，从物理上打破
  「同一次调用既写又自评」的 reward-hack —— 对应终版架构里
  「生成与校验分离」的原则。默认全空表示 reviewer 复用 writer 的 LLM。
- ``load_config()`` 从环境变量装配，缺项走文档化默认，不做严格校验（v1 的教训）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """运行时配置。provider 工厂据此构造 LLM / 检索实例。"""

    # ---- writer LLM ----
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_base_url: str | None = None
    llm_api_key_env: str | None = None

    # ---- LLM 健壮性（经 factory 注入 ResilientLLMProvider）----
    # 写正文那次调用 prompt 与输出都最长，最容易撞超时。一次瞬时超时不该让整个
    # 计划 failed，故默认开重试；置 0 可关掉（回到裸 provider 的原始异常语义）。
    llm_max_retries: int = 3
    llm_retry_base_backoff_s: float = 1.0
    llm_retry_max_backoff_s: float = 30.0
    llm_request_timeout_s: float = 120.0     # 单次 HTTP 请求超时（传给底层 client）

    # ---- reviewer LLM（可选，独立模型，用于生成/校验分离）----
    reviewer_llm_provider: str = ""
    reviewer_llm_model: str = ""
    reviewer_llm_base_url: str | None = None
    reviewer_llm_api_key_env: str | None = None

    # ---- 多模态 / vision LLM（可选，用于视觉版面验收）----
    vlm_provider: str = ""
    vlm_model: str = ""
    vlm_base_url: str | None = None
    vlm_api_key_env: str | None = None

    # ---- 检索源 ----
    retrieval_provider: str = "mock"

    # ---- 运行时/工作区 ----
    workspace_root: str = ".paper_workspace"

    # ---- Week 1 gate 参数（卡 1 · polish）----
    polish_max_retry: int = 2
    polish_max_char_change_ratio: float = 0.30  # 字数变化 ≤ 30%

    # ---- streaming-and-trace 开关 ----
    #: 落 runs/<run_id>/trace.json。**默认开**：真机排障全靠它，而「改了但看不见
    #: 有没有生效」已经反复付过代价（F-1 的词数门槛让功能从未执行，正是靠埋点才
    #: 发现）。落盘走 `sanitize`（用户原话不入 trace），开着不额外泄露。
    #: 关掉用 `PAPER_TRACE=0`。
    trace_enabled: bool = True        # 落 runs/<run_id>/trace.json
    stream_enabled: bool = False      # 流式呈现候选（默认关，只影响呈现）
    runs_dir: str = "runs"            # trace 落盘根目录
    #: 额外把 trace 上传 Langfuse。本地 JSON 始终照落，此路只是附加。
    #: 需同时配 LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY，缺任一则静默不启用。
    langfuse_enabled: bool = False

    # ---- intent-planning 理解层（默认 off → 完全走现有 Router，行为不变）----
    intent_planning_mode: str = "off"          # off | shadow | active
    planning_max_operations: int = 5           # 单 Turn 最多操作数
    planning_max_revisions: int = 3            # 规划循环最多修订次数
    planning_max_input_chars: int = 20000      # 单条消息最大字符数
    planning_timeout_s: float = 60.0           # 单次 provider 调用超时（秒）
    planning_max_llm_calls: int = 20           # 单 Turn 总 LLM 调用上限
    planning_provider_time_budget_s: float = 120.0  # 单 Turn provider 累计时间预算
    shadow_timeout_s: float = 15.0             # shadow 模式严格短超时

    # ---- literature-evidence / A1（默认值保证行为不变）----
    literature_strict_verification: bool = False        # 仅 VERIFIED 可引用（不含 legacy）
    literature_title_similarity: float = 0.85           # 标题相似度阈值
    literature_dedup_suggestions_enabled: bool = True   # 是否产出低置信去重建议
    literature_require_final_verification: bool = False  # 是否要求最终核验源（A2 接 Crossref 后开）
    literature_source_priority: str = "openalex,semantic_scholar,arxiv"  # 同层级源排序

    # ---- literature-evidence / A2a 语言判定阈值（初始值，需由 Golden Set 校准）----
    literature_lang_han_zh: float = 0.50        # Han 占比 ≥ → Han 主导（中文候选）
    # 汉字绝对数 ≥ → 也算 Han 主导（与占比是 OR 关系）。治「中文标题嵌 BERT-BiLSTM-CRF
    # 这类缩写把拉丁占比顶过半」的漏判；英文标题汉字数恒 0，零误伤。见 language.py 注释。
    literature_lang_han_count: int = 6
    literature_lang_latin: float = 0.50         # 拉丁占比 ≥ → 拉丁主导
    literature_lang_kana_min: float = 0.05      # 假名占比 ≥ → 显著假名（→ ja）
    literature_lang_hangul_min: float = 0.05    # Hangul 占比 ≥ → 显著 Hangul（→ ko）
    literature_lang_policy: str = "high_only"   # high_only | include_low_with_warning

    # ---- literature-evidence / A2d 写作接入总开关（默认关；A2d-5 全链路就绪才启用）----
    literature_writing_integration: bool = False

    # ---- literature-evidence / A2f 检索意图准备与查询规划 ----
    literature_search_synthesis: bool = False           # 合成检索意图（默认关；开则可从上下文推主题）
    literature_query_planner: str = "deterministic"     # deterministic | llm | hybrid
    literature_max_search_intent_revisions: int = 2     # 澄清/修改重合成上限

    # ---- F-1 检索召回质量：OpenAlex 短语优先（默认开）----
    # OpenAlex `search=` 是 OR-ish 词袋，词多则向领域高被引大热门倾斜（实测前 10 条
    # 只 2 条切题）；引住前 3 词后 10/10 切题。命中不足自动回退裸词，故召回下界不变，
    # 默认开。关掉则 search 行为与改造前逐字节相同。
    literature_openalex_phrase_first: bool = True

    # ---- literature-evidence / A2c 检索 Loop（组合层据此构造 RetrievalLimits 注入）----
    literature_discovery_enabled: bool = False          # Discovery 兜底默认关（无真实 Web 后端）
    # 下面四个上限与 max_iters 是**一组**，改一个要连带核另外几个：否则名义轮数跑不到，
    # 检索会以 budget_exhausted 静默收尾（结果保留，但配额补不齐）。
    # 2026-08 真机校准（zh=5/en=7，7 次 attempt、273.9s）：单 attempt 实测耗时均值
    # 42.9s / 最坏 54.5s，verify 均值 18.3 次 / 最坏 20 次。旧值 time=120s 只够 ~2 轮、
    # verify=120 只够 6 轮但会与时间同时见底 —— 实跑停在 iterations=3、
    # exhausted_dimensions=['max_verify_calls']，且有 3 条候选取回后未判完即丢
    # （reasons=relevance:budget_exhausted）。故按「6 轮最坏情况 + 余量」重定。
    literature_retrieval_max_iters: int = 6
    literature_retrieval_max_provider_calls: int = 240  # 全局上限，须 ≥ search + verify
    literature_retrieval_max_search_calls: int = 30
    literature_retrieval_max_verify_calls: int = 150    # 6 轮最坏 120 次 + 25% 余量
    literature_retrieval_max_llm_calls: int = 30        # 实测 7 轮仅 12 次，余量充足
    literature_retrieval_time_budget_s: float = 360.0   # 6 轮最坏 327s + 余量
    # 接受相关性等级：仅 relevant | 也收 weakly_relevant（引言常引背景/邻近工作）
    literature_accept_weakly_relevant: bool = False
    # F-3：接受策略模式。真机数据（2026-08-01）显示 18 个候选里 10 个判 weakly_relevant，
    # 含写引言必引的综述；而 method/evidence 类章节要的是直接支撑。故按章节用途分档。
    #   strict     —— 一律只收 relevant（默认，与改造前逐字节一致）
    #   by_purpose —— 按 section_purpose 分档（引言/相关工作/背景/讨论 收 weakly）
    #   permissive —— 一律也收 weakly（等价于旧 literature_accept_weakly_relevant=true）
    literature_acceptance_mode: str = "strict"

    # ---- 证据集上下文预算（build_bundle）----
    # 只约束**证据块**（文献题录 + 摘要片段），不含目标章节正文（后者当前无上限，
    # 见 backlog D-4「两套预算体系互不相识」）。
    #
    # 默认值从 8000 提到 24000：8000 是硬编码默认参数，从未按真实数据校准。
    # 真机实测（2026-08-02，proj_1780f6e14f27 的 10 条 OpenAlex 记录）
    # 平均 1521 字符/条 ⇒ 8000 只装 5 条，而用户要求 total=10 需 15213 字符，
    # 于是 `total=10` 恒定触发 CONTEXT_BUDGET_INSUFFICIENT → 操作被 rejected。
    # 24000 ≈ 16 条全摘要，覆盖「引言 10 篇」这类常见要求且留余量。
    #
    # 计量口径是**字符**而非 token（backlog P-3 已记）：中文约 1–1.5 字符/token、
    # 英文约 4 字符/token，故同一预算下中文的真实 token 成本是英文的 3–4 倍。
    # 换 token 计量属独立改动，不在本次范围。
    literature_bundle_budget_chars: int = 24000

    # ---- retrieval-throughput / B1（默认关；size=1 保持逐篇语义）----
    # B1 默认开启（2026-08-06 受控 A/B）：单篇判定时 relevance LLM 占 125s 里的
    # 89.9s（72%），批量后 5 次调用 51.1s、LLM 秒/候选 4.50→2.15。省下的预算够多跑
    # 一轮检索 ⇒ 停止原因首次从 budget_exhausted 变 quota_satisfied、入库 6→17。
    # size 与 enabled 必须同改：`_active_relevance_runtime` 对 size<2 直接拒绝装配。
    literature_relevance_batch_enabled: bool = True
    literature_relevance_batch_size: int = 10
    literature_relevance_batch_input_chars: int = 12000
    literature_relevance_batch_input_tokens: int | None = None
    literature_relevance_batch_output_chars: int = 8000
    literature_relevance_batch_output_tokens: int | None = None
    literature_batch_retry_limit: int = 1
    # ---- retrieval-throughput / B2（默认关；相关性先于严格核验）----
    literature_pipeline_reorder_enabled: bool = False


# 向后兼容别名：V2 骨架代码里以 RuntimeConfig 指代同一对象。
RuntimeConfig = Config


def load_config() -> Config:
    """从环境变量加载配置。缺项走文档化默认，不做严格校验。"""
    return Config(
        llm_provider=os.environ.get("PAPER_LLM", "openai"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
        llm_max_retries=_env_int("PAPER_LLM_MAX_RETRIES", 3, minimum=0),
        llm_retry_base_backoff_s=_env_float(
            "PAPER_LLM_RETRY_BASE_BACKOFF", 1.0, minimum=0.0
        ),
        llm_retry_max_backoff_s=_env_float(
            "PAPER_LLM_RETRY_MAX_BACKOFF", 30.0, minimum=0.0
        ),
        llm_request_timeout_s=_env_float(
            "PAPER_LLM_TIMEOUT", 120.0, minimum=1.0
        ),
        reviewer_llm_provider=os.environ.get("PAPER_REVIEWER_LLM", ""),
        reviewer_llm_model=os.environ.get("PAPER_REVIEWER_LLM_MODEL", ""),
        reviewer_llm_base_url=os.environ.get("PAPER_REVIEWER_BASE_URL") or None,
        reviewer_llm_api_key_env=os.environ.get("PAPER_REVIEWER_KEY_ENV") or None,
        vlm_provider=os.environ.get("PAPER_VLM", ""),
        vlm_model=os.environ.get("PAPER_VLM_MODEL", ""),
        vlm_base_url=os.environ.get("PAPER_VLM_BASE_URL") or None,
        vlm_api_key_env=os.environ.get("PAPER_VLM_KEY_ENV") or None,
        retrieval_provider=os.environ.get("PAPER_RETRIEVAL", "mock"),
        workspace_root=os.environ.get("PAPER_WORKSPACE_DIR", ".paper_workspace"),
        trace_enabled=_env_flag("PAPER_TRACE", True),
        stream_enabled=_env_flag("PAPER_STREAM", False),
        langfuse_enabled=_env_flag("PAPER_LANGFUSE", False),
        runs_dir=os.environ.get("PAPER_RUNS_DIR", "runs"),
        intent_planning_mode=_env_mode("PAPER_INTENT_PLANNING_MODE", "off"),
        planning_max_operations=_env_int("PAPER_PLANNING_MAX_OPERATIONS", 5, minimum=1),
        planning_max_revisions=_env_int("PAPER_PLANNING_MAX_REVISIONS", 3, minimum=1),
        planning_max_input_chars=_env_int("PAPER_PLANNING_MAX_INPUT_CHARS", 20000, minimum=1),
        planning_timeout_s=_env_float("PAPER_PLANNING_TIMEOUT", 60.0, minimum=0.0),
        planning_max_llm_calls=_env_int("PAPER_PLANNING_MAX_LLM_CALLS", 20, minimum=1),
        planning_provider_time_budget_s=_env_float(
            "PAPER_PLANNING_PROVIDER_TIME_BUDGET", 120.0, minimum=0.0
        ),
        shadow_timeout_s=_env_float("PAPER_SHADOW_TIMEOUT", 15.0, minimum=0.0),
        literature_strict_verification=_env_flag("PAPER_LIT_STRICT_VERIFY", False),
        literature_title_similarity=_env_float("PAPER_LIT_TITLE_SIM", 0.85, minimum=0.0),
        literature_dedup_suggestions_enabled=_env_flag("PAPER_LIT_DEDUP_SUGGEST", True),
        literature_require_final_verification=_env_flag(
            "PAPER_LIT_REQUIRE_FINAL_VERIFY", False
        ),
        literature_source_priority=os.environ.get(
            "PAPER_LIT_SOURCE_PRIORITY", "openalex,semantic_scholar,arxiv"
        ),
        literature_lang_han_zh=_env_float("PAPER_LIT_LANG_HAN_ZH", 0.50, minimum=0.0),
        literature_lang_han_count=_env_int("PAPER_LIT_LANG_HAN_COUNT", 6, minimum=1),
        literature_lang_latin=_env_float("PAPER_LIT_LANG_LATIN", 0.50, minimum=0.0),
        literature_lang_kana_min=_env_float("PAPER_LIT_LANG_KANA_MIN", 0.05, minimum=0.0),
        literature_lang_hangul_min=_env_float("PAPER_LIT_LANG_HANGUL_MIN", 0.05, minimum=0.0),
        literature_lang_policy=_env_lang_policy("PAPER_LIT_LANG_POLICY", "high_only"),
        literature_writing_integration=_env_flag("PAPER_LIT_WRITING_INTEGRATION", False),
        literature_search_synthesis=_env_flag("PAPER_LIT_SEARCH_SYNTHESIS", False),
        literature_query_planner=_env_query_planner("PAPER_LIT_QUERY_PLANNER", "deterministic"),
        literature_max_search_intent_revisions=_env_int(
            "PAPER_LIT_MAX_SEARCH_INTENT_REVISIONS", 2, minimum=0),
        literature_openalex_phrase_first=_env_flag("PAPER_LIT_PHRASE_FIRST", True),
        literature_discovery_enabled=_env_flag("PAPER_LIT_DISCOVERY_ENABLED", False),
        literature_retrieval_max_iters=_env_int("PAPER_LIT_RETRIEVAL_MAX_ITERS", 6, minimum=1),
        literature_retrieval_max_provider_calls=_env_int(
            "PAPER_LIT_RETRIEVAL_MAX_PROVIDER_CALLS", 240, minimum=1),
        literature_retrieval_max_search_calls=_env_int("PAPER_LIT_RETRIEVAL_MAX_SEARCH_CALLS", 30, minimum=1),
        literature_retrieval_max_verify_calls=_env_int("PAPER_LIT_RETRIEVAL_MAX_VERIFY_CALLS", 150, minimum=1),
        literature_retrieval_max_llm_calls=_env_int("PAPER_LIT_RETRIEVAL_MAX_LLM_CALLS", 30, minimum=1),
        literature_retrieval_time_budget_s=_env_float("PAPER_LIT_RETRIEVAL_TIME_BUDGET", 360.0, minimum=0.0),
        literature_accept_weakly_relevant=_env_flag("PAPER_LIT_ACCEPT_WEAKLY_RELEVANT", False),
        literature_acceptance_mode=_env_acceptance_mode(
            "PAPER_LIT_ACCEPTANCE_MODE", "strict"),
        # 0 = 不限（build_bundle 对 budget_chars<=0 视为无预算），故 minimum=0。
        literature_bundle_budget_chars=_env_int(
            "PAPER_LIT_BUNDLE_BUDGET_CHARS", 40000, minimum=0),
        literature_relevance_batch_enabled=_env_flag(
            "PAPER_LIT_RELEVANCE_BATCH_ENABLED", True),
        literature_relevance_batch_size=_env_int(
            "PAPER_LIT_RELEVANCE_BATCH_SIZE", 10, minimum=1),
        literature_relevance_batch_input_chars=_env_int(
            "PAPER_LIT_RELEVANCE_BATCH_INPUT_CHARS", 12000, minimum=1),
        literature_relevance_batch_input_tokens=_env_optional_int(
            "PAPER_LIT_RELEVANCE_BATCH_INPUT_TOKENS", minimum=1),
        literature_relevance_batch_output_chars=_env_int(
            "PAPER_LIT_RELEVANCE_BATCH_OUTPUT_CHARS", 8000, minimum=1),
        literature_relevance_batch_output_tokens=_env_optional_int(
            "PAPER_LIT_RELEVANCE_BATCH_OUTPUT_TOKENS", minimum=1),
        literature_batch_retry_limit=_env_int(
            "PAPER_LIT_BATCH_RETRY_LIMIT", 1, minimum=0),
        literature_pipeline_reorder_enabled=_env_flag(
            "PAPER_LIT_PIPELINE_REORDER_ENABLED", False),
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_mode(name: str, default: str) -> str:
    """理解层三态；非法值安全降级为 default。"""
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in ("off", "shadow", "active") else default


def _env_lang_policy(name: str, default: str) -> str:
    """语言计数策略；非法值降级为默认（high_only 严格），并打印可观测警告。

    降级方向恒为**更严格**（默认 high_only），不会静默进入宽松模式。
    """
    raw = (os.environ.get(name) or "").strip().lower()
    valid = ("high_only", "include_low_with_warning")
    if raw == "":
        return default
    if raw not in valid:
        import warnings
        warnings.warn(
            f"{name}={raw!r} 非法，降级为 {default!r}（不进入宽松模式）",
            RuntimeWarning,
            stacklevel=2,
        )
        return default
    return raw


def _env_acceptance_mode(name: str, default: str) -> str:
    """相关性接受策略模式；非法值降级为 ``strict``（**更严格**方向）并告警。

    与 `_env_lang_policy` 同一纪律：降级恒为更严格，绝不静默放宽引用门槛。
    """
    raw = (os.environ.get(name) or "").strip().lower()
    valid = ("strict", "by_purpose", "permissive")
    if raw == "":
        return default
    if raw not in valid:
        import warnings
        warnings.warn(
            f"{name}={raw!r} 非法，降级为 'strict'（不静默放宽引用门槛）",
            RuntimeWarning,
            stacklevel=2,
        )
        return "strict"
    return raw


def _env_query_planner(name: str, default: str) -> str:
    """检索查询规划器模式；非法值降级 deterministic 并告警（不静默进入 LLM 模式）。"""
    raw = (os.environ.get(name) or "").strip().lower()
    valid = ("deterministic", "llm", "hybrid")
    if raw == "":
        return default
    if raw not in valid:
        import warnings
        warnings.warn(f"{name}={raw!r} 非法，降级为 {default!r}", RuntimeWarning, stacklevel=2)
        return default
    return raw


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """解析整数环境变量；非法/越界降级为 default。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw.strip())
    except (ValueError, TypeError):
        return default
    if minimum is not None and val < minimum:
        return default
    return val


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """解析浮点环境变量；非法/越界降级为 default。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw.strip())
    except (ValueError, TypeError):
        return default
    if minimum is not None and val < minimum:
        return default
    return val


def _env_optional_int(name: str, *, minimum: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except (ValueError, TypeError):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


__all__ = ["Config", "RuntimeConfig", "load_config"]
