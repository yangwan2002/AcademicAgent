"""A2c-1 wave 4 / Task 5：结构验收 + 旧路径零影响。"""

from __future__ import annotations

import ast
import pathlib

from paper_agent.config import Config
from paper_agent.literature.verification import (
    EventKind,
    VerificationStatus,
    aggregate_status,
    make_event,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "paper_agent"


def _imports(rel_path: str) -> set[str]:
    tree = ast.parse((_SRC / rel_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


# ---------------------------------------------------------------- 分层依赖

def _assert_no_tasking(rel_path: str):
    imports = _imports(rel_path)
    assert not any(m.startswith("paper_agent.tasking") for m in imports), (rel_path, imports)


def test_a2c1_modules_do_not_import_tasking():
    for mod in (
        "literature/retrieval/cross_verify.py",
        "literature/retrieval/__init__.py",
        "literature/verification.py",
        "providers/retrieval/crossref.py",
        "providers/retrieval/capabilities.py",
    ):
        _assert_no_tasking(mod)


def test_cross_verify_does_not_import_config():
    # 阈值/预算/clock 注入；模块不读全局 config
    assert "paper_agent.config" not in _imports("literature/retrieval/cross_verify.py")


# ---------------------------------------------------------------- 旧路径零影响

def test_global_require_final_default_unchanged():
    # A2c-1 不改全局默认；严格核验只在新 Loop 通过注入生效
    assert Config().literature_require_final_verification is False


def test_aggregate_status_compat_unchanged():
    # 既有 COMPAT 折叠不变：存储 authority=final 的事件仍 VERIFIED
    ev = make_event(
        EventKind.VERIFIED, source="openalex", identifier_type="doi",
        identifier="10.1/x", authority="final", checked_at="2026-01-01T00:00:00Z",
    )
    assert aggregate_status([ev]) is VerificationStatus.VERIFIED


def test_reference_record_status_still_compat_projection():
    # ReferenceRecord.verification_status 仍走 COMPAT（读存储 authority），未被 A2c-1 改动
    from paper_agent.literature.models import ReferenceRecord

    ev = make_event(
        EventKind.VERIFIED, source="openalex", identifier_type="doi",
        identifier="10.1/x", authority="final", checked_at="2026-01-01T00:00:00Z",
    )
    rec = ReferenceRecord(reference_id="r1", verification_events=(ev,))
    assert rec.verification_status is VerificationStatus.VERIFIED
