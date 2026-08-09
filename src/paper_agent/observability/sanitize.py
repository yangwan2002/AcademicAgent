"""隐私脱敏器（Sanitizer）。

统一把任何将要离开进程内存、进入 trace.json / 日志 / exporter 的文本，转成
**只含哈希 + 短预览 + 长度**的不可变值对象（`SafeText` / `SafePrompt`），并在
此之前剔除 API 密钥等敏感串。覆盖 prompt、response、diff、论文正文、interview
答案、violations、error 消息（Req 8.1/8.5）。

设计要点：
- 预览截断到 ≤500 字符（Req 8.2）。
- `redact()` 剔除常见密钥形态（Req 8.4）。
- `sanitize_messages()` 先把消息列表规范化为 `json.dumps(sort_keys=True,
  ensure_ascii=False, separators=(",",":"))`，使哈希与字典键顺序无关，再脱敏。
- 结果对象 `frozen=True`，一经产生不可改。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PREVIEW_MAX_CHARS = 500
_REDACTED = "***REDACTED***"


@dataclass(frozen=True)
class SafeText:
    """一段被脱敏的文本：只保留哈希、短预览与长度，不含原文。"""

    hash: str
    preview: str
    length: int


@dataclass(frozen=True)
class SafePrompt:
    """一组被脱敏的 prompt 消息：语义同 SafeText，但来源是消息列表。"""

    hash: str
    preview: str
    length: int


# --- 密钥剔除 ---

# 形如 `api_key: xxx` / `token=xxx` / `Authorization: xxx` 的键值对：保留标签，
# 仅替换取值部分，便于阅读又不泄露。
_KV_SECRET = re.compile(
    r"(?i)((?:api[_-]?key|apikey|secret|token|password|passwd|access[_-]?key"
    r"|authorization|auth[_-]?token)\s*[:=]\s*)(['\"]?)([^\s'\",}]+)"
)

# 常见密钥前缀（sk-/pk-/rk-/ada_ 等）后跟一长串字符。
_PREFIXED_KEY = re.compile(r"\b(?:sk|pk|rk|ada|key)[-_][A-Za-z0-9]{12,}\b")

# `Bearer <token>`。
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}")

# 独立出现的长十六进制串（≥32），典型如 32 位十六进制 API key 尾段。
_LONG_HEX = re.compile(r"\b[A-Fa-f0-9]{32,}\b")

# 用户整句原话块：由 `generate.context_builder.SOURCE_TEXT_OPEN/CLOSE` 包裹。
#
# 为什么在这里剔除而不是不放进 prompt：那段原话对生成有用（承载「目前只是一个概述」
# 这类只供 LLM 消费的背景），但**整句 turn 文本此前从不进任何 prompt**，trace 因此有
# 「原始用户文本不落盘」不变量。两个目标不冲突 —— 发给 provider 照发，落 trace 前切掉。
#
# 注意这不同于正文/goal/constraints：那些早已进 500 字预览，是既定立场；整句原话是本次
# 新引入的，不该顺带扩大记录范围。`.*?` 配 DOTALL：原话可能含换行。
_SOURCE_TEXT_BLOCK = re.compile(
    r"<<<user_source_text>>>.*?<<</user_source_text>>>", re.DOTALL
)


def redact(text: str) -> str:
    """剔除文本中的 API 密钥 / 令牌等敏感串（Req 8.4）与用户整句原话块。

    防御式：任何异常都退回原串截断保护。
    """
    if not text:
        return text
    s = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    s = _BEARER.sub(_REDACTED, s)
    s = _PREFIXED_KEY.sub(_REDACTED, s)
    s = _LONG_HEX.sub(_REDACTED, s)
    s = _SOURCE_TEXT_BLOCK.sub(_REDACTED, s)
    return s


def _summarize(text: str) -> tuple[str, str, int]:
    """把（已脱敏的）文本压成 (hash, preview, length)。"""
    redacted = redact(text)
    h = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
    preview = redacted[:PREVIEW_MAX_CHARS]
    return h, preview, len(redacted)


def sanitize_text(text: Any) -> SafeText:
    """把任意文本脱敏为 `SafeText`（hash + 预览 + 长度）。

    None 视为空串。非字符串先 `str()`。
    """
    s = "" if text is None else str(text)
    h, preview, length = _summarize(s)
    return SafeText(hash=h, preview=preview, length=length)


def digest(text: Any, *, length: int = 16) -> str:
    """**只出摘要、不出预览**的哈希（A2f trace 用；主题/查询原文绝不进 trace）。

    统一入口,避免在 Loop/Handler 里散落 sha1;先剔密钥再哈希,截断到 length。
    """
    s = "" if text is None else str(text)
    return hashlib.sha256(redact(s).encode("utf-8")).hexdigest()[:length]


def _message_to_canonical(m: Any) -> dict:
    """把一条消息（Message 对象或 dict）规范化为可稳定序列化的普通 dict。

    仅保留 role / content 及工具调用的结构信息；图像不含 base64 原文，只记数量，
    避免把大体积二进制/隐私图像带入 trace。
    """
    if isinstance(m, Mapping):
        role = m.get("role")
        content = m.get("content")
        tool_calls = m.get("tool_calls")
        images = m.get("images")
    else:
        role = getattr(m, "role", None)
        content = getattr(m, "content", None)
        tool_calls = getattr(m, "tool_calls", None)
        images = getattr(m, "images", None)

    d: dict = {"role": role, "content": content}
    if tool_calls:
        norm_tc = []
        for tc in tool_calls:
            if isinstance(tc, Mapping):
                norm_tc.append(
                    {"name": tc.get("name"), "arguments": tc.get("arguments")}
                )
            else:
                norm_tc.append(
                    {
                        "name": getattr(tc, "name", None),
                        "arguments": getattr(tc, "arguments", None),
                    }
                )
        d["tool_calls"] = norm_tc
    if images:
        d["images_count"] = len(images)
    return d


def sanitize_messages(messages: Any) -> SafePrompt:
    """把 prompt 消息列表脱敏为 `SafePrompt`。

    先规范化为 `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":"))`
    的字符串，使哈希与嵌套字典键顺序无关；再 redact→hash→preview。
    """
    try:
        canonical = [_message_to_canonical(m) for m in (messages or [])]
        text = json.dumps(
            canonical,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except Exception:  # noqa: BLE001 - 脱敏绝不拖垮业务
        text = str(messages)
    h, preview, length = _summarize(text)
    return SafePrompt(hash=h, preview=preview, length=length)
