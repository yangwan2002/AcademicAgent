"""Offline-first retrieval-throughput benchmark entry point.

Characterizes the single-item strategy in the same production attempt used
for batched review. Historical golden payloads retain their original schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = ROOT / "src"
for path in (str(SRC), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from baseline import build_single_item_snapshot


def canonical_hash(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_profile(path: Path) -> tuple[dict, str]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    return profile, canonical_hash(profile)


def build_report(profile_path: Path) -> dict:
    profile, profile_hash = load_profile(profile_path)
    snapshot = build_single_item_snapshot()
    return {
        "schema_version": "retrieval-throughput-report-v1",
        "mode": "single-item-characterization",
        "network_used": False,
        "profile_id": profile["profile_id"],
        "profile_hash": profile_hash,
        "snapshot_hash": canonical_hash(snapshot),
        "result": snapshot,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval throughput offline baseline")
    parser.add_argument("--profile", type=Path, default=HERE / "benchmark_profile.json")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(args.profile)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
