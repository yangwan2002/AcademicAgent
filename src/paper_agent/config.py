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

    # ---- streaming-and-trace 开关（默认全关 → 与既有行为逐字节一致）----
    trace_enabled: bool = False       # 落 runs/<run_id>/trace.json
    stream_enabled: bool = False      # 流式呈现候选
    runs_dir: str = "runs"            # trace 落盘根目录


# 向后兼容别名：V2 骨架代码里以 RuntimeConfig 指代同一对象。
RuntimeConfig = Config


def load_config() -> Config:
    """从环境变量加载配置。缺项走文档化默认，不做严格校验。"""
    return Config(
        llm_provider=os.environ.get("PAPER_LLM", "openai"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
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
        trace_enabled=_env_flag("PAPER_TRACE", False),
        stream_enabled=_env_flag("PAPER_STREAM", False),
        runs_dir=os.environ.get("PAPER_RUNS_DIR", "runs"),
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


__all__ = ["Config", "RuntimeConfig", "load_config"]
