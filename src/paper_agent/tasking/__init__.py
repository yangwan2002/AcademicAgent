"""intent-planning 理解层：把一条自然语言消息解析为有序操作计划。

组件（按数据流）：operation_analyzer → extractors → plan_builder
→ plan_validator → clarifier/planning_loop。本包只负责「理解 → 计划 → 澄清 →
确认」，执行复用现有 execute_*（见 runtime/）。
"""
