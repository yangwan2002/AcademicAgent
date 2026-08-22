"""A2b wave 4 / Task 5：结构验收（分层依赖 + 槽位未激活）。"""

from __future__ import annotations

import ast
import pathlib

from paper_agent.intents.intent import Intent
from paper_agent.tasking.intent_spec import spec_for


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

def test_quota_does_not_import_tasking_config_datetime():
    imports = _imports("literature/quota.py")
    assert not any(m.startswith("paper_agent.tasking") for m in imports), imports
    assert "paper_agent.config" not in imports, imports
    assert "datetime" not in imports, imports


def test_quota_may_import_language():
    # 允许依赖同包 language（复用 counts_toward，不重造语言逻辑）
    imports = _imports("literature/quota.py")
    assert "paper_agent.literature.language" in imports


# ---------------------------------------------------------------- 槽位激活范围
# 按 A2d-5 预期变更更新：写作两意图已激活该槽，其余意图仍不得出现。

def test_citation_quota_slot_scoped_to_writing_intents():
    for intent in (Intent.REWRITE, Intent.ADD_SECTION):
        assert "citation_quota" in spec_for(intent).slots, intent
    for intent in (Intent.POLISH, Intent.CONVERT_FORMAT, Intent.FORMAT_DOCX):
        assert "citation_quota" not in spec_for(intent).slots, intent
