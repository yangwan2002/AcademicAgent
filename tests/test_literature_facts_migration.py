"""A1 wave 7：SessionFacts v2 持久化 + 迁移（v1 / 混合 schema）+ 原子写。"""

from __future__ import annotations

import json
import os

import pytest

from paper_agent.session.facts import KnownNumber, KnownRef, SessionFacts


def test_save_load_roundtrip_v2(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    f.add_ref(KnownRef(id="1", bibkey="smith2020", title="Deep Nets", verified=True))
    f.add_number("0.87", source="user", confirmed=True)
    f.set_section_structure("introduction", "背景 → gap → 贡献")
    f.save()

    g = SessionFacts.load(p)
    assert g.bibkeys() == {"smith2020"}
    assert [r for r in g.known_refs if r.bibkey == "smith2020"][0].verified is True
    assert g.number_values() == {"0.87"}
    assert g.get_section_structure("introduction") == "背景 → gap → 贡献"


def test_save_writes_schema_v2(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    f.add_ref(KnownRef(id="1", bibkey="k", title="T", verified=True))
    f.save()
    data = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert "references" in data and "known_refs" not in data
    # verification_status 是派生字段，不落 JSON
    assert "verification_status" not in data["references"][0]


def test_load_v1_migration_lossless(tmp_path):
    # 手写一个 v1 facts.json（无 schema_version，有 known_refs）
    p = tmp_path / "facts.json"
    p.write_text(json.dumps({
        "known_refs": [
            {"id": "1", "bibkey": "a", "title": "A", "verified": True},
            {"id": "2", "bibkey": "b", "title": "", "verified": False},
        ],
        "known_numbers": [{"value": "0.5", "source": "", "confirmed": True}],
        "claims": {"x": 1},
    }, ensure_ascii=False), encoding="utf-8")

    f = SessionFacts.load(str(p))
    assert len(f.known_refs) == 2                 # 条数守恒
    assert f.bibkeys() == {"a", "b"}
    # verified=True → 可引用（兼容），但底层状态非 VERIFIED（不虚高）
    assert "a" in f.ledger.citable_bibkeys()
    assert "a" not in f.ledger.citable_bibkeys(allow_legacy=False)
    rec_a = [r for r in f.ledger.all_records() if r.bibkey == "a"][0]
    assert rec_a.legacy_verified and rec_a.verification_method == "legacy"
    assert f.get_claim("x") == 1


def test_load_mixed_schema_merges_v1_extras(tmp_path):
    # 同时有 references 与 known_refs：known_refs 独有的 c 必须一并迁入
    p = tmp_path / "facts.json"
    f0 = SessionFacts(path=str(p))
    f0.add_ref(KnownRef(id="1", bibkey="a", title="A", verified=True))
    f0.save()
    data = json.loads(p.read_text(encoding="utf-8"))
    data["known_refs"] = [
        {"id": "1", "bibkey": "a", "title": "A", "verified": True},   # 已存在
        {"id": "9", "bibkey": "c", "title": "C", "verified": False},  # v2 没有
    ]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    f = SessionFacts.load(str(p))
    assert f.bibkeys() == {"a", "c"}              # 不静默忽略 known_refs


def test_atomic_write_creates_backup(tmp_path):
    p = tmp_path / "facts.json"
    p.write_text('{"schema_version": 2, "references": [], "known_numbers": [], "claims": {}}', encoding="utf-8")
    f = SessionFacts.load(str(p))
    f.add_ref(KnownRef(id="1", bibkey="k", title="T", verified=True))
    f.save()
    # 首次写回会备份原始字节
    assert (tmp_path / "facts.v1.bak.json").is_file()
    assert not list(tmp_path.glob("*.tmp"))       # 无残留临时文件
