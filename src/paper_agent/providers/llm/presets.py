"""OpenAI 兼容厂商预设表（数据驱动）。

接入一个新的 OpenAI 兼容厂商 = 在此字典加一条记录，无需新增任何类。
若某厂商不在表中，也可通过 Config 直接传 base_url + api_key_env 接入（零代码）。

端点信息来自各厂商公开文档，内容经改写以符合合规要求；
具体端点/模型名以厂商最新文档为准。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VendorPreset:
    base_url: str
    api_key_env: str
    default_model: str
    # 厂商专有的非标准请求参数（透传到 extra_body），可选。
    extra_body: dict | None = None
    # 默认请求参数（如 stream），可选。
    default_options: dict | None = None


# 厂商名（小写） → 预设。新增厂商只需在此追加一行。
VENDOR_PRESETS: dict[str, VendorPreset] = {
    "openai": VendorPreset(
        base_url="",  # 空表示用 OpenAI SDK 默认端点
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
    ),
    "qwen": VendorPreset(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        default_model="qwen-plus",
    ),
    "deepseek": VendorPreset(
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
    ),
    "moonshot": VendorPreset(
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        default_model="moonshot-v1-8k",
    ),
    "zhipu": VendorPreset(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPU_API_KEY",
        default_model="glm-4-plus",
    ),
    # 携程内部 LLM 网关（OpenAI 兼容）。API Key 通过环境变量提供，切勿硬编码。
    # 大模型长响应易触发非流式读超时，故默认走流式（stream=True）。
    "ctrip": VendorPreset(
        base_url="http://aigw.fx.ctripcorp.com/llm/100000416/v1",
        api_key_env="CTRIP_LLM_API_KEY",
        default_model="Qwen3.5-397B-A17B",
        default_options={"stream": True},
    ),
}
