"""A2c-5 / Task 5+6：config 项 + 结构验收。"""

from __future__ import annotations

import ast
import pathlib

from paper_agent.config import Config, load_config

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "paper_agent"


def _imports(rel):
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------- config

def test_config_a2c_defaults():
    c = Config()
    assert c.literature_discovery_enabled is False       # Discovery 默认关
    assert c.literature_retrieval_max_iters == 6
    # 360s 而非旧的 120s：旧值只够约 2 轮，max_iters=6 名义上限跑不到。
    # 依据与关系型断言见 tests/test_retrieval_budget_coherence.py。
    assert c.literature_retrieval_time_budget_s == 360.0


def test_config_a2c_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("PAPER_LIT_RETRIEVAL_MAX_ITERS", "3")
    c = load_config()
    assert c.literature_discovery_enabled is True
    assert c.literature_retrieval_max_iters == 3


def test_config_a2c_invalid_degrades(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_RETRIEVAL_MAX_ITERS", "notanint")
    assert load_config().literature_retrieval_max_iters == 6   # 非法降级默认


# ---------------------------------------------------------------- 结构验收

def test_discovery_no_tasking_config():
    imports = _imports("literature/retrieval/discovery.py")
    assert not any(m.startswith("paper_agent.tasking") for m in imports), imports
    assert "paper_agent.config" not in imports, imports


def test_batch_transaction_no_tasking():
    for mod in ("literature/retrieval/batch.py", "literature/retrieval/transaction.py"):
        imports = _imports(mod)
        assert not any(m.startswith("paper_agent.tasking") for m in imports), (mod, imports)


def test_no_fake_success_discovery_provider():
    # 不提供伪成功生产 provider：discovery.py 里不定义「返回空却成功」的生产 Provider 类
    src = (_SRC / "literature/retrieval/discovery.py").read_text(encoding="utf-8")
    # 只应有协议消费 + resolve 逻辑，不含具体 web provider 实现
    assert "class WebSearchProvider" not in src
    assert "httpx" not in src


def test_citation_quota_slot_activated_with_handler_consumption():
    """按 A2d-5 预期变更更新：槽已激活，且**同批**有 Handler 真实消费（原子激活）。"""
    from paper_agent.intents.router import Intent
    from paper_agent.tasking.intent_spec import spec_for

    for intent in (Intent.REWRITE, Intent.ADD_SECTION):
        assert "citation_quota" in spec_for(intent).slots
    handlers_src = (_SRC / "runtime/handlers.py").read_text(encoding="utf-8")
    assert '_val(op, "citation_quota")' in handlers_src
