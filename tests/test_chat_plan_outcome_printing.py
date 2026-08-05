"""回归：失败原因必须完整打印。

真机现象是 `1. rejected — 我尝试了 3 次仍未通过校验。最后一次违反：` 后面**空无一物**。
Gate 其实算出了违规并挂在 message 上，但 `_print_plan_outcome` 只取 `splitlines()[0]`，
第 2 行起的详情全被丢掉 —— 等于把唯一的排查线索藏起来了。
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "_chat_script", Path(__file__).resolve().parents[1] / "scripts" / "chat.py")


def _load_printer():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod._print_plan_outcome


class _Status:
    def __init__(self, value):
        self.value = value


class _Op:
    def __init__(self, message):
        self.status = _Status("rejected")
        self.message = message
        self.generated_outputs = ()
        self.changed_working_copy = False


class _Report:
    def __init__(self, ops):
        self.plan_status = _Status("failed")
        self.operations = ops
        self.rollback_available = False


class _Out:
    def __init__(self, report):
        self.planning_exit = None
        self.report = report


def test_multiline_violation_detail_is_printed(capsys):
    msg = ("我尝试了 3 次仍未通过校验。最后一次违反：\n"
           "  - total 未达标：需 10（min），实际 0，缺 10\n"
           "  - 段落数从 8 变为 16，超出允许区间 [6, 13]。")
    _load_printer()(_Out(_Report([_Op(msg)])))
    printed = capsys.readouterr().out
    assert "total 未达标" in printed
    assert "段落数从 8 变为 16" in printed


def test_single_line_message_still_prints_inline(capsys):
    _load_printer()(_Out(_Report([_Op("章节不存在：引言")])))
    printed = capsys.readouterr().out
    assert "章节不存在：引言" in printed


def test_empty_message_does_not_crash(capsys):
    _load_printer()(_Out(_Report([_Op("")])))
    assert "rejected" in capsys.readouterr().out
