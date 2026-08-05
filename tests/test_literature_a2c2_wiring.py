"""A2c-2 wave 4 / Task 5：结构验收（分层依赖 + 未落盘 + 未接写作链路）。"""

from __future__ import annotations

import ast
import pathlib

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


def test_a2c2_modules_do_not_import_tasking_or_config():
    for mod in (
        "literature/retrieval/batch.py",
        "literature/retrieval/registry.py",
        "literature/retrieval/transaction.py",
    ):
        imports = _imports(mod)
        assert not any(m.startswith("paper_agent.tasking") for m in imports), (mod, imports)
        assert "paper_agent.config" not in imports, (mod, imports)


def test_transaction_only_writes_via_atomic_helper():
    # A2c-2 的 plan_commit 纯内存（见 test_retrieval_transaction 的行为断言）；
    # A2c-5 的 commit_batch 落盘**只**经 migration.atomic_write_json，不做裸 os.replace。
    src = (_SRC / "literature/retrieval/transaction.py").read_text(encoding="utf-8")
    assert "os.replace" not in src


def test_transaction_does_not_import_facts_module():
    # 不 import session.facts 模块（facts 作参数传入，落盘经注入的 facts.path + 原子写）
    imports = _imports("literature/retrieval/transaction.py")
    assert "paper_agent.session.facts" not in imports
