"""Audit a JSONL evaluation set for cross-split identity leakage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def audit_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_split: dict[str, int] = {}
    identities: dict[str, set[str]] = {}
    templates: dict[str, set[str]] = {}
    for row in records:
        split = str(row.get("split") or "").strip()
        if not split:
            continue
        by_split[split] = by_split.get(split, 0) + 1
        for key in ("doc_id", "source_doc_id", "source_sha256", "document_id"):
            value = str(row.get(key) or "").strip()
            if value:
                identities.setdefault(value, set()).add(split)
        for value in row.get("doc_ids") or []:
            if str(value).strip():
                identities.setdefault(str(value).strip(), set()).add(split)
        template = str(row.get("template_id") or "").strip()
        if template:
            templates.setdefault(template, set()).add(split)

    identity_leaks = sorted(key for key, splits in identities.items() if len(splits) > 1)
    template_leaks = sorted(key for key, splits in templates.items() if len(splits) > 1)
    duplicate_ids = _duplicates(records, "id")
    duplicate_questions = _duplicates(records, "question")
    return {
        "schema_version": "souldrive_eval_leakage_audit_v0.1",
        "record_count": len(records),
        "split_counts": dict(sorted(by_split.items())),
        "missing_split_count": sum(1 for row in records if not str(row.get("split") or "").strip()),
        "duplicate_record_ids": duplicate_ids,
        "duplicate_questions": duplicate_questions,
        "cross_split_identity_leaks": identity_leaks,
        "cross_split_template_leaks": template_leaks,
        "pass": not (duplicate_ids or duplicate_questions or identity_leaks or template_leaks or any_split_missing(records)),
    }


def _duplicates(records: list[dict[str, Any]], key: str) -> list[str]:
    seen: dict[str, int] = {}
    for row in records:
        value = str(row.get(key) or "").strip()
        if value:
            seen[value] = seen.get(value, 0) + 1
    return sorted(value for value, count in seen.items() if count > 1)


def any_split_missing(records: list[dict[str, Any]]) -> bool:
    return bool(records) and any(not str(row.get("split") or "").strip() for row in records)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else None
    if isinstance(payload, dict):
        for key in ("records", "queries", "episodes", "items"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit evaluation split leakage.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_records(load_jsonl(args.dataset.resolve()))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
