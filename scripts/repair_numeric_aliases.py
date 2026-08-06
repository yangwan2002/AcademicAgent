"""修复账本里重复的数字别名（一次性数据修复，可重复执行）。

## 背景

`retrieval/attempt.py` 在 Attempt 期按 `ledger.next_numeric_alias()` 预分配别名基线，但
不看 `pending_batches` —— **两批在彼此提交前各自算基线就会拿到同一段号**；
`ledger.add_record` 忠实追加、不校验唯一性，冲突直接落盘。

提交侧现已由 `transaction._dealias` 收口（新碰撞不再产生），但**既有 facts.json 不会被
自动改写**。重复别名会让 `resolve_token(ext="md")` 返回 AMBIGUOUS ⇒ 相关引用一律判
「无法解析」⇒ CitationQuotaGate 永远差满额，rewrite 重试到预算耗尽也不可能通过。

## 用法

    python scripts/repair_numeric_aliases.py                 # 全库体检（只读）
    python scripts/repair_numeric_aliases.py --apply         # 写入修复
    python scripts/repair_numeric_aliases.py --project proj_x --apply

## 安全性

- **只改 `numeric_aliases`**，其它字段逐字不动。
- 保留每个别名的**首次**出现（先入为主），只给后来的冲突者重新编号，取当前最大号 +1。
- 写入前自检：修复后别名集合必须无重复、且数量与原先「有别名的记录数」一致。
- 写入前把原文件另存为 `facts.json.bak-<时间戳>`。
- 若某文档已引用了被改号的别名，会**警告并拒绝**该项目（避免让正文与账本脱节）。
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_agent.gates.diff_gate import extract_citation_tokens

WORKSPACE = Path(".paper_workspace")


def _plan(refs: list[dict]) -> list[tuple[int, str, str]]:
    """返回 [(记录下标, 旧别名, 新别名), ...]。保留首次出现，只改后来的冲突者。"""
    seen: set[str] = set()
    out: list[tuple[int, str, str]] = []
    for idx, ref in enumerate(refs):
        for alias in list(ref.get("numeric_aliases") or []):
            if alias not in seen:
                seen.add(alias)
                continue
            nxt = max((int(a) for a in seen if str(a).isdigit()), default=0) + 1
            while str(nxt) in seen:
                nxt += 1
            seen.add(str(nxt))
            out.append((idx, alias, str(nxt)))
    return out


def _cited_tokens(proj_dir: Path) -> set[str]:
    """该项目所有文档里现存的引用标记（用于判断重编号是否会打断正文）。"""
    tokens: set[str] = set()
    for path in proj_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() in (".json", ".bak"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ext in ("md", "tex"):
            tokens.update(extract_citation_tokens(text, ext))
    return tokens


def _apply(refs: list[dict], plan: list[tuple[int, str, str]]) -> None:
    for idx, old, new in plan:
        aliases = list(refs[idx].get("numeric_aliases") or [])
        refs[idx]["numeric_aliases"] = [new if a == old else a for a in aliases]


def _verify(refs: list[dict]) -> None:
    counts = collections.Counter(
        a for r in refs for a in (r.get("numeric_aliases") or [])
    )
    dupes = {k: v for k, v in counts.items() if v > 1}
    if dupes:
        raise AssertionError(f"修复后仍有重复别名：{dupes}")


def repair(facts_path: Path, *, apply: bool) -> bool:
    """处理一个 facts.json。返回 True 表示「已修复」或「本来就干净」。"""
    data = json.loads(facts_path.read_text(encoding="utf-8"))
    refs = data.get("references") or []
    plan = _plan(refs)

    label = facts_path.parent.name
    if not plan:
        print(f"[OK]   {label}: {len(refs)} 条，无重复别名")
        return True

    print(f"[FIX]  {label}: {len(refs)} 条，需改 {len(plan)} 处")
    for idx, old, new in plan:
        title = (refs[idx].get("title") or "")[:44]
        print(f"         [{old}] → [{new}]  {refs[idx].get('reference_id')}  {title}")

    clash = {old for _, old, _ in plan} & _cited_tokens(facts_path.parent)
    if clash:
        print(f"[SKIP] {label}: 正文已引用将被改号的别名 {sorted(clash)}，"
              f"需同步改正文，本脚本不动它")
        return False

    if not apply:
        print(f"[DRY]  {label}: 未写入（加 --apply 生效）")
        return False

    _apply(refs, plan)
    _verify(refs)
    backup = facts_path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(facts_path, backup)
    facts_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[DONE] {label}: 已写入，原文件备份为 {backup.name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="修复账本重复数字别名")
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省只体检）")
    ap.add_argument("--project", default="", help="只处理某个 proj_* 目录")
    args = ap.parse_args()

    pattern = f"{args.project}/facts.json" if args.project else "proj_*/facts.json"
    paths = sorted(WORKSPACE.glob(pattern))
    if not paths:
        print(f"未找到 facts.json（{WORKSPACE / pattern}）")
        return 1

    ok = all(repair(p, apply=args.apply) for p in paths)
    if not args.apply:
        print("\n体检完毕，未改动任何文件。确认无误后加 --apply 执行。")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
