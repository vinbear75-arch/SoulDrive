"""Read-only structural preflight for unapproved business seeds, never scoring."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY = PROJECT_ROOT / "eval/fixtures/business_seed_review_v0.1"
TASK_COUNTS = {"cross_document_synthesis": 10, "paper_review": 8, "experiment_compare": 6, "memory_conflict": 6}
POLICY_KEYS = {"allow_quality_scoring", "allow_prompt_tuning", "allow_training", "allow_freezing", "allow_external_release"}


def validate_seed_review(directory: Path, *, project_root: Path = PROJECT_ROOT) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "souldrive_business_seed_review_manifest_v0.1":
        raise ValueError("unsupported review manifest")
    if manifest.get("review_status") != "pending_review":
        raise ValueError("this preflight accepts pending_review only")
    policy = manifest.get("usage_policy") or {}
    if set(policy) != POLICY_KEYS or any(value is not False for value in policy.values()):
        raise ValueError("unapproved seeds must forbid scoring, tuning, training, freezing and release")

    source_ids = set()
    root = project_root.resolve()
    for source in manifest.get("sources") or []:
        source_id = source["id"]
        if source_id in source_ids:
            raise ValueError("duplicate source id")
        source_ids.add(source_id)
        path = (root / source["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("source path is outside this project")
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"source fingerprint mismatch: {source_id}")

    schema = json.loads((root / "eval/schemas/business_seed_review_v0.1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    rows = [json.loads(line) for line in (directory / "seeds.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 30 or manifest.get("sample_count") != 30:
        raise ValueError("the authorized review batch must contain 30 seeds")
    seen = set()
    for row in rows:
        try:
            validator.validate(row)
        except ValidationError as exc:
            raise ValueError("seed violates the pending-review schema") from exc
        if row["id"] in seen:
            raise ValueError("duplicate seed id")
        seen.add(row["id"])
        refs = set(row["source_refs"])
        if not refs <= source_ids or not set(row["setup"]["selected_document_refs"]) <= refs:
            raise ValueError(f"unbound source reference: {row['id']}")
    counts = dict(Counter(row["business_task"] for row in rows))
    if counts != TASK_COUNTS or manifest.get("task_counts") != TASK_COUNTS:
        raise ValueError("business task counts differ from this review batch")
    return {
        "mode": "structure_only",
        "review_status": "pending_review",
        "sample_count": len(rows),
        "task_counts": counts,
        "source_fingerprints_verified": len(source_ids),
        "scoring_performed": False,
        "ready_for_freeze": False,
        "manual_review_required": manifest["manual_review_required"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        report = validate_seed_review(args.dataset_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"review preflight failed: {type(exc).__name__}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
