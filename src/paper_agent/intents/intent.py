"""Planning 支持的写作操作类型。"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """可进入 Planning/PlanExecutor 的写作操作。"""

    POLISH = "polish"
    REWRITE = "rewrite"
    ADD_SECTION = "add_section"
    CONVERT_FORMAT = "convert_format"
    FORMAT_DOCX = "format_docx"


__all__ = ["Intent"]
