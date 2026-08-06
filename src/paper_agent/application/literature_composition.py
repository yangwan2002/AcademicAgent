"""写作前文献编排器的组合装配（application 层，接线用）。

把散落的机制件——ProviderRegistry / AttemptDeps / RelevanceJudge / RetrievalLimits /
语言 classifier——按 `Config` 装配成一个可用的 `WritingLiteratureOrchestrator`。

分层纪律：本模块属 **application 层**（可依赖 literature / providers / config）；
`literature/*` 领域模块**不**反向依赖它。所有阈值/预算/policy 由此注入,不在领域层读 config。

装配策略（诚实、无伪成功）：
- `literature_writing_integration` 关 → 返回 ``None``（调用方据此保持旧路径）。
- 检索 provider = ``mock`` → search=mock、verify=MockVerifyProvider（离线自洽,联调用）。
- 其余（openalex/arxiv/…）→ search=该 provider、verify=CrossrefVerifyProvider（真实网络）。
- search 源无 SEARCH profile（如 api）→ 返回 ``None`` 并说明,不硬造能力。
- 相关性 judge 优先用独立 reviewer LLM（打破自评 reward-hack）,缺省回退 writer LLM。
- supervised 恒为 True：真实相关性评测达标前,证据集确认必须人工过目（A2c 上线门槛）。
"""

from __future__ import annotations

from paper_agent.config import Config
from paper_agent.application.literature_ports import ThroughputPolicySnapshot
from paper_agent.literature.language import (
    LanguageThresholds,
    classify_record_language,
)
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    WEAKLY_ACCEPTANCE,
    JudgeVersion,
    RelevanceJudge,
    acceptance_for_purpose,
    relevance_content_hash,
)
from paper_agent.literature.relevance_batch import (
    BatchRelevanceJudge,
    make_batch_judge_version,
)
from paper_agent.literature.relevance_runtime import (
    ActiveRelevanceRuntime,
    RelevanceTextPolicy,
)
from paper_agent.literature.retrieval.attempt import AttemptDeps, RelevanceAcceptancePolicy
from paper_agent.literature.retrieval.budget import RetrievalLimits
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget,
    WindowLimits,
)
from paper_agent.literature.sources import ProviderRole, profile_for


class CompositionUnavailable(Exception):
    """无法装配写作编排器（附人类可读原因；调用方降级为旧路径并提示）。"""


def _thresholds(config: Config) -> LanguageThresholds:
    return LanguageThresholds(
        han_ratio_zh=config.literature_lang_han_zh,
        latin_ratio=config.literature_lang_latin,
        kana_min=config.literature_lang_kana_min,
        hangul_min=config.literature_lang_hangul_min,
    )


def _limits(config: Config) -> RetrievalLimits:
    return RetrievalLimits(
        max_iterations=config.literature_retrieval_max_iters,
        max_provider_calls=config.literature_retrieval_max_provider_calls,
        max_search_calls=config.literature_retrieval_max_search_calls,
        max_verify_calls=config.literature_retrieval_max_verify_calls,
        max_llm_calls=config.literature_retrieval_max_llm_calls,
        provider_time_budget_s=config.literature_retrieval_time_budget_s,
    )


def build_acceptance_resolver(config: Config):
    """据 config 造「按章节用途解析接受策略」的解析器（F-3）。

    返回 `(section_purpose) -> RelevanceAcceptancePolicy`。组合层**一次解析**，
    再由编排器注入 Attempt / assess_availability / build_bundle **三处**——
    此前三处各自判断，其中两处硬编码，导致「也收 weakly」只改摄入不改消费。

    模式（`literature_acceptance_mode`）：
    - ``strict``（默认）：一律 STRICT，与改造前逐字节一致；
    - ``by_purpose``：引言/相关工作/背景/讨论 收 weakly，方法/证据类保持严格；
    - ``permissive``：一律收 weakly。
    旧的 `literature_accept_weakly_relevant=True` 等价于 ``permissive``（向后兼容）。
    """
    mode = (config.literature_acceptance_mode or "strict").strip().lower()
    if config.literature_accept_weakly_relevant:
        mode = "permissive"

    if mode == "permissive":
        return lambda *_signals: WEAKLY_ACCEPTANCE
    if mode == "by_purpose":
        return lambda *signals: acceptance_for_purpose(*signals)
    return lambda *_signals: STRICT_ACCEPTANCE


def _acceptance_policy(config: Config) -> RelevanceAcceptancePolicy:
    """装配期的**默认**接受策略（无章节信号上下文时用）。

    真正生效的是 `build_acceptance_resolver`：编排器按每次请求的章节信号
    解析后覆盖 `AttemptDeps.acceptance_policy`。此处只提供一个安全默认。
    """
    return build_acceptance_resolver(config)()


def _active_relevance_runtime(
    config: Config,
    reviewer_llm,
    *,
    token_estimator=None,
):
    if not config.literature_relevance_batch_enabled:
        return None
    if config.literature_relevance_batch_size < 2:
        raise CompositionUnavailable(
            "批量相关性已启用，但 literature_relevance_batch_size 必须 >= 2"
        )
    if config.literature_batch_retry_limit not in (0, 1):
        raise CompositionUnavailable("literature_batch_retry_limit 只能为 0 或 1")
    if (
        config.literature_relevance_batch_input_tokens is not None
        and token_estimator is None
    ):
        raise CompositionUnavailable(
            "配置了 batch input token 上限，但未注入可靠 tokenizer"
        )

    text_policy = RelevanceTextPolicy(version="relevance-text-batch-v1")
    input_budget = BatchInputBudget(
        max_input_chars=config.literature_relevance_batch_input_chars,
        max_input_tokens=config.literature_relevance_batch_input_tokens,
        max_output_chars=config.literature_relevance_batch_output_chars,
        max_output_tokens=config.literature_relevance_batch_output_tokens,
    )
    provider = config.reviewer_llm_provider or config.llm_provider
    model_id = config.reviewer_llm_model or config.llm_model
    evaluator = BatchRelevanceJudge(
        reviewer_llm,
        judge_version=make_batch_judge_version(
            provider=provider,
            model_id=model_id,
            text_policy=text_policy,
            input_budget=input_budget,
        ),
        text_policy=text_policy,
        input_budget=input_budget,
        retry_limit=config.literature_batch_retry_limit,
    )
    return ActiveRelevanceRuntime(
        evaluator=evaluator,
        text_policy=text_policy,
        window_limits=WindowLimits(
            batch_size=config.literature_relevance_batch_size,
            input_budget=input_budget,
        ),
        token_estimator=token_estimator,
    )


def _throughput_policy_snapshot(active_runtime, config: Config) -> ThroughputPolicySnapshot:
    if active_runtime is None:
        return ThroughputPolicySnapshot(
            reorder_enabled=config.literature_pipeline_reorder_enabled,
            judge_provider=config.reviewer_llm_provider or config.llm_provider,
            judge_model_id=config.reviewer_llm_model or config.llm_model,
        )
    budget = active_runtime.window_limits.input_budget
    policy = active_runtime.text_policy
    version = active_runtime.evaluator.judge_version
    return ThroughputPolicySnapshot(
        batch_enabled=True,
        reorder_enabled=config.literature_pipeline_reorder_enabled,
        batch_size=active_runtime.window_limits.batch_size,
        max_input_chars=budget.max_input_chars,
        max_input_tokens=budget.max_input_tokens,
        max_output_chars=budget.max_output_chars,
        max_output_tokens=budget.max_output_tokens,
        text_policy_version=policy.version,
        max_title_chars=policy.max_title_chars,
        max_abstract_chars=policy.max_abstract_chars,
        judge_provider=version.provider,
        judge_model_id=version.model_id,
        prompt_version=version.prompt_version,
        schema_version=version.schema_version,
        retry_limit=active_runtime.evaluator.retry_limit,
    )


def _build_registry(config: Config, retrieval_provider) -> ProviderRegistry:
    name = (config.retrieval_provider or "").strip().lower()
    if ProviderRole.SEARCH not in profile_for(name).roles:
        raise CompositionUnavailable(
            f"检索源 {name!r} 未声明 SEARCH 能力,无法用于写作前文献检索。"
        )
    search = (named(name, retrieval_provider),)

    if name == "mock":
        from paper_agent.providers.retrieval.mock import MockVerifyProvider

        verify = (named("mock", MockVerifyProvider()),)
    else:
        verify = _real_verify_sources()
    return ProviderRegistry(search_sources=search, verify_sources=verify)


def _real_verify_sources() -> tuple:
    """真实核验源集合（A2e，design D5）：crossref(final doi) + openalex + arxiv,
    S2 仅当配置了 API key（匿名 429）。权限仍由 sources.py 决定,聚合库对 DOI 只 corroboration。
    """
    import os

    from paper_agent.providers.retrieval.crossref import CrossrefVerifyProvider
    from paper_agent.providers.retrieval.verify_adapters import (
        ArxivVerifyProvider,
        OpenAlexVerifyProvider,
        SemanticScholarVerifyProvider,
    )

    sources = [
        named("crossref", CrossrefVerifyProvider()),
        named("openalex", OpenAlexVerifyProvider()),
        named("arxiv", ArxivVerifyProvider()),
    ]
    if os.environ.get("S2_API_KEY"):
        sources.append(named("semantic_scholar", SemanticScholarVerifyProvider()))
    return tuple(sources)


def build_writing_orchestrator(
    config: Config,
    *,
    writer_llm,
    reviewer_llm=None,
    retrieval_provider,
    facts,
    budget_chars: int | None = None,
    relevance_token_estimator=None,
):
    """据配置装配 `WritingLiteratureOrchestrator`。

    - 集成开关关 → 返回 ``None``（不是错误,是显式未启用）。
    - 装配失败（无 SEARCH 源等）→ 抛 :class:`CompositionUnavailable`（附原因）。

    ``budget_chars``：证据集上下文预算。``None``（默认）→ 取
    ``config.literature_bundle_budget_chars``（可由 ``PAPER_LIT_BUNDLE_BUDGET_CHARS``
    覆盖）。此前是硬编码默认参数 ``8000``,`chat.py` 不传 ⇒ CLI 无法调整,
    而真机 10 条记录需 15213 字符 ⇒ `total=10` 恒定被 `CONTEXT_BUDGET_INSUFFICIENT`
    毙掉。显式传值仍然优先（测试与 dogfood runner 用 0 表示不限）。
    """
    if not config.literature_writing_integration:
        return None

    from paper_agent.application.literature_flow import WritingLiteratureOrchestrator

    registry = _build_registry(config, retrieval_provider)

    judge = RelevanceJudge(
        reviewer_llm or writer_llm,
        judge_version=JudgeVersion(
            provider=(config.reviewer_llm_provider or config.llm_provider),
            model_id=(config.reviewer_llm_model or config.llm_model),
        ),
    )
    active_runtime = _active_relevance_runtime(
        config,
        reviewer_llm or writer_llm,
        token_estimator=relevance_token_estimator,
    )
    limits = _limits(config)
    deps = AttemptDeps(
        registry=registry,
        ledger=facts.ledger,
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=judge,
        relevance_store=facts.relevance_store,
        budget=VerifyBudget(
            max_verify_calls=limits.max_verify_calls,
            provider_time_budget_s=limits.provider_time_budget_s,
        ),
        verify_cache=VerificationCache(),
        acceptance_policy=_acceptance_policy(config),
        language_thresholds=_thresholds(config),
        language_policy=config.literature_lang_policy,
        active_relevance_runtime=active_runtime,
        pipeline_reorder_enabled=config.literature_pipeline_reorder_enabled,
    )

    thresholds = _thresholds(config)

    def _classifier(rec):
        return classify_record_language(rec, thresholds)

    return WritingLiteratureOrchestrator(
        deps=deps,
        limits=limits,
        planner=build_query_planner(config, writer_llm=writer_llm),
        classifier=_classifier,
        policy=config.literature_lang_policy,
        judge_key=(active_runtime.judge_key if active_runtime else judge.judge_version.key()),
        content_hasher=(
            active_runtime.content_hash if active_runtime else relevance_content_hash
        ),
        budget_chars=(
            config.literature_bundle_budget_chars if budget_chars is None else budget_chars
        ),
        supervised=True,   # 真实相关性评测达标前恒 supervised（A2c 上线门槛）
        throughput_policy_snapshot=_throughput_policy_snapshot(active_runtime, config),
        # F-3：接受语义的唯一解析入口，由编排器注入 Attempt / availability / bundle 三处
        acceptance_resolver=build_acceptance_resolver(config),
    )


def build_query_planner(config: Config, *, writer_llm=None):
    """按 config 选检索查询规划器（A2f D8）。llm/hybrid 用 writer LLM，**不用相关性 reviewer**。"""
    mode = (config.literature_query_planner or "deterministic").strip().lower()
    if mode == "deterministic" or writer_llm is None:
        return DeterministicQueryPlanner()
    from paper_agent.literature.retrieval.query_planner import LLMQueryPlanner

    if mode == "llm":
        return LLMQueryPlanner(writer_llm)
    if mode == "hybrid":
        return _HybridQueryPlanner(LLMQueryPlanner(writer_llm))
    return DeterministicQueryPlanner()


class _HybridQueryPlanner:
    """先确定性扩展；产不出查询才调 LLM（A2f D8：LLM 为兜底，非首选）。"""

    def __init__(self, llm_planner) -> None:
        self._det = DeterministicQueryPlanner()
        self._llm = llm_planner

    def propose(self, request):
        plan = self._det.propose(request)      # 确定性不记 LLM
        if not plan.is_empty:
            return plan
        return self._llm.propose(request)      # 兜底：真发模型调用（llm_called=True）


def build_search_synthesizer(config: Config, *, writer_llm=None):
    """按 config 选检索意图合成器（A2f）。合成关 → None（Handler 走旧 initial_query 路径）。"""
    if not config.literature_search_synthesis:
        return None
    from paper_agent.tasking.query_synthesizer import (
        DeterministicSearchIntentSynthesizer,
        LLMSearchIntentSynthesizer,
    )

    if writer_llm is None:
        return DeterministicSearchIntentSynthesizer()
    return LLMSearchIntentSynthesizer(writer_llm)


__all__ = [
    "build_writing_orchestrator", "CompositionUnavailable",
    "build_query_planner", "build_search_synthesizer",
    "build_acceptance_resolver",
]
