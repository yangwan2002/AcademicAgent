"""Export/import workflow for independent human relevance adjudication."""

from __future__ import annotations

import argparse
import copy
import csv
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.json"
VERDICTS = {"relevant", "weakly_relevant", "unrelated"}
FIELDS = [
    "case_id",
    "criteria_id",
    "language",
    "slices",
    "topic",
    "research_question",
    "include_conditions",
    "exclude_conditions",
    "title",
    "abstract",
    "annotator_1_id",
    "annotator_1_verdict",
    "annotator_1_rationale",
    "annotator_2_id",
    "annotator_2_verdict",
    "annotator_2_rationale",
    "adjudicated_verdict",
    "adjudication_note",
]


def _expanded_abstract(case: dict) -> str:
    candidate = case["candidate"]
    abstract = str(candidate.get("abstract", ""))
    repeat = max(1, int(candidate.get("abstract_repeat", 1)))
    return " ".join(abstract for _ in range(repeat)) if abstract else ""


def export_sheet(manifest_path: Path, output_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    criteria = {row["id"]: row for row in manifest["criteria"]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for case in manifest["cases"]:
            task = criteria[case["criteria_id"]]
            writer.writerow({
                "case_id": case["id"],
                "criteria_id": case["criteria_id"],
                "language": case["language"],
                "slices": "|".join(case["slices"]),
                "topic": task.get("topic", ""),
                "research_question": task.get("research_question", ""),
                "include_conditions": "|".join(task.get("include_conditions", [])),
                "exclude_conditions": "|".join(task.get("exclude_conditions", [])),
                "title": case["candidate"]["title"],
                "abstract": _expanded_abstract(case),
            })


def import_adjudication(
    manifest_path: Path,
    sheet_path: Path,
    output_path: Path,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with sheet_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_id = {row.get("case_id", ""): row for row in rows}
    expected_ids = {case["id"] for case in manifest["cases"]}
    if set(by_id) != expected_ids or "" in by_id:
        raise ValueError("annotation sheet case ids must exactly match manifest")

    updated = copy.deepcopy(manifest)
    for case in updated["cases"]:
        row = by_id[case["id"]]
        annotator_1 = row.get("annotator_1_id", "").strip()
        annotator_2 = row.get("annotator_2_id", "").strip()
        verdict_1 = row.get("annotator_1_verdict", "").strip()
        verdict_2 = row.get("annotator_2_verdict", "").strip()
        adjudicated = row.get("adjudicated_verdict", "").strip()
        if not annotator_1 or not annotator_2 or annotator_1 == annotator_2:
            raise ValueError(f"{case['id']}: two distinct anonymous annotator ids required")
        if verdict_1 not in VERDICTS or verdict_2 not in VERDICTS:
            raise ValueError(f"{case['id']}: invalid independent verdict")
        if adjudicated not in VERDICTS:
            raise ValueError(f"{case['id']}: explicit adjudicated verdict required")
        note = row.get("adjudication_note", "").strip()
        if verdict_1 != verdict_2 and not note:
            raise ValueError(f"{case['id']}: disagreement requires adjudication note")
        case["label"] = {
            "verdict": adjudicated,
            "status": "adjudicated",
            "annotators": [annotator_1, annotator_2],
            "independent_verdicts": [verdict_1, verdict_2],
            "rationale": note or row.get("annotator_1_rationale", "").strip(),
        }
    updated["annotation_protocol"]["status"] = "adjudicated"
    updated["dataset_id"] = manifest["dataset_id"] + "-adjudicated"
    output_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Human relevance annotation helper")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    export.add_argument("--output", type=Path, required=True)
    ingest = sub.add_parser("import")
    ingest.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ingest.add_argument("--sheet", type=Path, required=True)
    ingest.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        export_sheet(args.manifest, args.output)
    else:
        if args.output.resolve() == args.manifest.resolve():
            parser.error("output must not overwrite the source manifest")
        import_adjudication(args.manifest, args.sheet, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
