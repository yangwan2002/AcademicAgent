"""streaming-and-trace Task 2 验收：Sanitizer + sanitize_messages。

对应 tasks.md Task 2 / Requirement 8：
- sanitize_text -> SafeText{hash, preview≤500, length}
- redact() 剔除密钥
- sanitize_messages -> SafePrompt（规范化 json.dumps(sort_keys) 后 redact→hash→preview）
- 截断；hash 与字典顺序无关；密钥剔除；SafeText 不可改
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.sanitize import (
    PREVIEW_MAX_CHARS,
    SafePrompt,
    SafeText,
    redact,
    sanitize_messages,
    sanitize_text,
)
from paper_agent.providers.llm.base import Message, ToolCall


# --- sanitize_text 基础 ---

def test_sanitize_text_shape():
    st = sanitize_text("hello world")
    assert isinstance(st, SafeText)
    assert st.length == len("hello world")
    assert st.preview == "hello world"
    # sha256 十六进制 64 字符
    assert len(st.hash) == 64


def test_sanitize_text_none_is_empty():
    st = sanitize_text(None)
    assert st.preview == ""
    assert st.length == 0


def test_preview_truncated_to_500():
    long = "阿" * 2000
    st = sanitize_text(long)
    assert len(st.preview) == PREVIEW_MAX_CHARS
    # length 记录完整长度（此处无密钥，redact 不改变）
    assert st.length == 2000


def test_same_input_same_hash():
    assert sanitize_text("abc").hash == sanitize_text("abc").hash
    assert sanitize_text("abc").hash != sanitize_text("abd").hash


def test_safetext_is_frozen():
    st = sanitize_text("x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        st.hash = "y"  # type: ignore[misc]


# --- redact 密钥剔除 ---

def test_redact_env_style_key():
    # 真实网关 key 形态：ada_ + 32 位十六进制
    raw = "ADA_LLM_KEY=ada_800aac45e6a742489460d155234322a4 用于鉴权"
    out = redact(raw)
    assert "ada_800aac45e6a742489460d155234322a4" not in out
    assert "REDACTED" in out


def test_redact_bearer_token():
    out = redact("Authorization: Bearer sk-abc123def456ghi789")
    assert "sk-abc123def456ghi789" not in out
    assert "REDACTED" in out


def test_redact_kv_secret_keeps_label():
    out = redact("api_key: sk-verylongsecrettokenvalue12345")
    assert "sk-verylongsecrettokenvalue12345" not in out
    # 标签保留，便于阅读
    assert "api_key" in out


def test_redact_long_hex():
    out = redact("hash=deadbeefdeadbeefdeadbeefdeadbeef00")
    assert "deadbeefdeadbeefdeadbeefdeadbeef00" not in out


def test_sanitize_text_redacts_before_hash_and_preview():
    st = sanitize_text("token=ada_800aac45e6a742489460d155234322a4")
    assert "ada_800aac45e6a742489460d155234322a4" not in st.preview


def test_redact_leaves_normal_text():
    normal = "这是一段普通的论文正文，没有任何密钥。"
    assert redact(normal) == normal


# --- sanitize_messages ---

def test_sanitize_messages_shape():
    msgs = [Message(role="system", content="你是助手"), Message(role="user", content="改写这段")]
    sp = sanitize_messages(msgs)
    assert isinstance(sp, SafePrompt)
    assert len(sp.hash) == 64
    assert sp.length > 0


def test_sanitize_messages_hash_order_independent_of_dict_keys():
    # 两条消息内容语义相同，但工具调用 arguments 的字典键顺序不同 → hash 应一致
    a = [
        Message(
            role="assistant",
            content="调用工具",
            tool_calls=[ToolCall(id="1", name="search", arguments={"q": "x", "n": 3})],
        )
    ]
    b = [
        Message(
            role="assistant",
            content="调用工具",
            tool_calls=[ToolCall(id="1", name="search", arguments={"n": 3, "q": "x"})],
        )
    ]
    assert sanitize_messages(a).hash == sanitize_messages(b).hash


def test_sanitize_messages_redacts_secret_in_content():
    msgs = [Message(role="user", content="我的 key 是 ada_800aac45e6a742489460d155234322a4")]
    sp = sanitize_messages(msgs)
    assert "ada_800aac45e6a742489460d155234322a4" not in sp.preview


def test_sanitize_messages_empty():
    sp = sanitize_messages([])
    assert isinstance(sp, SafePrompt)
    assert len(sp.hash) == 64


def test_sanitize_messages_excludes_image_base64():
    from paper_agent.providers.llm.base import ImageInput

    big = "data:image/png;base64," + "A" * 5000
    msgs = [Message(role="user", content="看图", images=[ImageInput(data_url=big)])]
    sp = sanitize_messages(msgs)
    # 不把 base64 原文带进预览/哈希载荷
    assert "AAAA" not in sp.preview
    assert sp.length < 5000


def test_safeprompt_is_frozen():
    sp = sanitize_messages([Message(role="user", content="x")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        sp.preview = "y"  # type: ignore[misc]
