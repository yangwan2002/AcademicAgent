"""docx 格式判定（扩展名层面）。

原 `docx_bridge` 模块承载的是「载入转 markdown、落盘转回 docx」的 pandoc 往返。
那条路已被 `docx_document` 的就地编辑取代 —— 往返会丢排版（md 里没有页边距/分栏/
页眉页脚的概念），而排版恰是 docx 用户的主要诉求之一。此处只留下格式判定。
"""

from __future__ import annotations

_DOCX_EXTS = frozenset({"docx"})


def is_docx(ext: str) -> bool:
    """按扩展名判断是否 docx（大小写与前导点均容忍）。"""
    return (ext or "").lower().lstrip(".") in _DOCX_EXTS


__all__ = ["is_docx"]
