"""根据配置构造 provider 实例。

将「配置字符串 → 具体实现」的映射集中于此，使 app 装配与调用方
无需 import 具体 provider 类。真实 provider（openai / api）惰性导入，
保持核心零依赖。
"""

from __future__ import annotations

from paper_agent.config import Config
from paper_agent.providers.llm.base import LLMError, LLMProvider
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.llm.presets import VENDOR_PRESETS
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.utils.dotenv import load_dotenv


def build_llm_provider(config: Config) -> LLMProvider:
    """据配置构造 LLM provider（数据驱动，接新厂商通常无需改代码）。

    解析顺序：
    1. "mock" → MockLLMProvider。
    2. 预设厂商名（见 presets.VENDOR_PRESETS）→ 通用 OpenAI 兼容 provider。
    3. 显式提供 llm_base_url + llm_api_key_env → 通用 provider（零代码接入）。
    """
    load_dotenv()  # 加载 .env，使本地运行无需手动配置环境变量
    name = config.llm_provider.lower()
    if name == "mock":
        return MockLLMProvider()

    # Anthropic Messages 协议（含 Anthropic 兼容网关）：另一个实现同一 LLMProvider
    # 抽象的适配器。经 base_url 指向网关，模型名可为网关侧任意模型（如 qwen3.7-max）。
    if name == "anthropic":
        from paper_agent.providers.llm.anthropic_provider import AnthropicProvider

        if not config.llm_model:
            raise LLMError("使用 Anthropic provider 时必须指定 llm_model。")
        return AnthropicProvider(
            model=config.llm_model,
            api_key_env=config.llm_api_key_env or "ANTHROPIC_API_KEY",
            base_url=config.llm_base_url or None,
        )

    from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider

    if name in VENDOR_PRESETS:
        preset = VENDOR_PRESETS[name]
        return OpenAICompatibleProvider(
            model=config.llm_model or preset.default_model,
            api_key_env=config.llm_api_key_env or preset.api_key_env,
            base_url=config.llm_base_url or (preset.base_url or None),
            extra_body=preset.extra_body,
            default_options=preset.default_options,
        )

    # 未预设厂商：只要给了 base_url 即可纯靠配置接入。
    if config.llm_base_url:
        if not config.llm_model:
            raise LLMError("使用自定义 LLM 端点时必须指定 llm_model。")
        return OpenAICompatibleProvider(
            model=config.llm_model,
            api_key_env=config.llm_api_key_env or "OPENAI_API_KEY",
            base_url=config.llm_base_url,
        )

    raise ValueError(
        f"未知的 LLM provider：{config.llm_provider}。"
        f"可用预设：{', '.join(sorted(VENDOR_PRESETS))}；"
        "或设置 llm_base_url + llm_api_key_env 接入自定义厂商。"
    )


def build_reviewer_llm_provider(config: Config) -> LLMProvider | None:
    """据配置为 reviewer 构造独立 LLM provider（Round 4）。

    若 ``reviewer_llm_*`` 字段全部为空，返回 ``None``——调用方据此回退到 writer
    的 LLM。否则用 reviewer 的字段覆盖 writer 配置后走同一构造路径，使 reviewer
    可指向不同模型/端点。任一字段为空则继承 writer 对应字段。

    用「reviewer 与 writer 不共享 LLM 实例」打破自评 reward-hack。
    """
    has_override = any(
        [
            config.reviewer_llm_provider,
            config.reviewer_llm_model,
            config.reviewer_llm_base_url,
            config.reviewer_llm_api_key_env,
        ]
    )
    if not has_override:
        return None
    reviewer_cfg = Config(
        llm_provider=config.reviewer_llm_provider or config.llm_provider,
        llm_model=config.reviewer_llm_model or config.llm_model,
        llm_base_url=config.reviewer_llm_base_url or config.llm_base_url,
        llm_api_key_env=config.reviewer_llm_api_key_env or config.llm_api_key_env,
        retrieval_provider=config.retrieval_provider,
    )
    return build_llm_provider(reviewer_cfg)


def resolved_llm_model(config: Config, *, reviewer: bool = False) -> str:
    """Return the effective model name used for diversity enforcement."""
    provider = (
        config.reviewer_llm_provider or config.llm_provider
        if reviewer
        else config.llm_provider
    ).lower()
    model = (
        config.reviewer_llm_model or config.llm_model
        if reviewer
        else config.llm_model
    )
    if model:
        return model.strip().lower()
    preset = VENDOR_PRESETS.get(provider)
    return preset.default_model.strip().lower() if preset else ""


def reviewer_model_is_diverse(config: Config) -> bool:
    """True only when reviewer explicitly resolves to another model."""
    if config.llm_provider.lower() == "mock":
        return True
    if (config.reviewer_llm_provider or config.llm_provider).lower() == "mock":
        return False
    writer_model = resolved_llm_model(config)
    reviewer_model = resolved_llm_model(config, reviewer=True)
    return bool(
        config.reviewer_llm_model
        and writer_model
        and reviewer_model
        and reviewer_model != writer_model
    )


def build_vlm_provider(config: Config) -> LLMProvider | None:
    """据配置构造**多模态（vision）** LLM provider（visual-layout-acceptance）。

    与主文本 LLM 解耦：由 ``vlm_*`` 字段独立配置。``vlm_provider`` 为空 → 返回
    ``None``（未配置，视觉验收闸优雅降级跳过）。"mock" → MockLLMProvider（测试用）。
    其余走通用 OpenAI 兼容 provider（vision 能力由所选模型决定）。
    """
    name = (config.vlm_provider or "").lower()
    if not name:
        return None
    if name == "mock":
        return MockLLMProvider()
    load_dotenv()
    from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider

    if name in VENDOR_PRESETS:
        preset = VENDOR_PRESETS[name]
        return OpenAICompatibleProvider(
            model=config.vlm_model or preset.default_model,
            api_key_env=config.vlm_api_key_env or preset.api_key_env,
            base_url=config.vlm_base_url or (preset.base_url or None),
            extra_body=preset.extra_body,
            default_options=preset.default_options,
        )
    if not config.vlm_model:
        raise LLMError("配置多模态 provider 时必须指定 vlm_model。")
    return OpenAICompatibleProvider(
        model=config.vlm_model,
        api_key_env=config.vlm_api_key_env or "PAPER_VLM_API_KEY",
        base_url=config.vlm_base_url or None,
    )


def build_retrieval_provider(config: Config) -> RetrievalProvider:
    name = config.retrieval_provider.lower()
    if name == "mock":
        return MockRetrievalProvider()
    if name == "api":
        from paper_agent.providers.retrieval.api import ApiRetrievalProvider

        return ApiRetrievalProvider()
    if name == "openalex":
        from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

        return OpenAlexRetrievalProvider()
    if name == "arxiv":
        from paper_agent.providers.retrieval.api import ArxivRetrievalProvider

        return ArxivRetrievalProvider()
    raise ValueError(f"未知的检索 provider：{config.retrieval_provider}")
