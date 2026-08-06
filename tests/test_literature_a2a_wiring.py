"""A2a wave 4：config 语言阈值 + 结构验收（分层依赖）。"""

from __future__ import annotations

import ast
import pathlib

import pytest

from paper_agent.config import Config, load_config
from paper_agent.literature.language import LanguageThresholds, classify_language


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "paper_agent"


def _imports(rel_path: str) -> set[str]:
    """解析一个模块的所有 import 目标（模块全名）。"""
    tree = ast.parse((_SRC / rel_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


# ---------------------------------------------------------------- config

def test_config_language_defaults():
    c = Config()
    assert c.literature_lang_han_zh == 0.50
    assert c.literature_lang_latin == 0.50
    assert c.literature_lang_kana_min == 0.05
    assert c.literature_lang_hangul_min == 0.05
    assert c.literature_lang_policy == "high_only"


def test_load_config_language_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_LANG_HAN_ZH", "0.6")
    monkeypatch.setenv("PAPER_LIT_LANG_POLICY", "include_low_with_warning")
    c = load_config()
    assert c.literature_lang_han_zh == 0.6
    assert c.literature_lang_policy == "include_low_with_warning"


def test_invalid_policy_degrades_strict_with_warning(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_LANG_POLICY", "loose")
    with pytest.warns(RuntimeWarning):
        c = load_config()
    assert c.literature_lang_policy == "high_only"       # 降级到严格，不进宽松


def test_thresholds_from_config_flow_into_classify():
    # 组合层用 config 构造 LanguageThresholds 注入分类器（config 不被 language 直接依赖）
    c = Config(literature_lang_han_zh=0.6)
    th = LanguageThresholds(
        han_ratio_zh=c.literature_lang_han_zh,
        latin_ratio=c.literature_lang_latin,
        kana_min=c.literature_lang_kana_min,
        hangul_min=c.literature_lang_hangul_min,
    )
    v = classify_language("基于深度学习的图像分类研究", "", "zh", thresholds=th)
    assert v.language_code == "zh"


# ---------------------------------------------------------------- 结构验收

def test_structured_module_does_not_import_tasking():
    imports = _imports("providers/llm/structured.py")
    assert not any(m.startswith("paper_agent.tasking") for m in imports), imports


@pytest.mark.parametrize("mod", [
    "literature/language.py",
    "literature/relevance.py",
    "literature/untrusted.py",
    "literature/models.py",
    "literature/ledger.py",
    "literature/dedup.py",
    "literature/verification.py",
    "literature/identifiers.py",
    "literature/sources.py",
    "literature/verifier.py",
    "literature/migration.py",
    "literature/bibkey.py",
])
def test_literature_does_not_import_tasking(mod):
    imports = _imports(mod)
    assert not any(m.startswith("paper_agent.tasking") for m in imports), (mod, imports)


def test_language_does_not_import_config():
    imports = _imports("literature/language.py")
    assert "paper_agent.config" not in imports, imports
