"""Audit the checked-out CRUD-RAG data contract without executing third-party code."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASK_DOCUMENT_FIELDS = {
    "event_summary": ["text"],
    "continuing_writing": ["beginning", "continuing"],
    "hallu_modified": ["newsBeginning", "newsRemainder", "hallucinatedContinuation"],
    "questanswer_1doc": ["news1"],
    "questanswer_2docs": ["news1", "news2"],
    "questanswer_3docs": ["news1", "news2", "news3"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def audit(repo: Path) -> dict[str, Any]:
    split_path = repo / "data" / "crud_split" / "split_merged.json"
    records = json.loads(split_path.read_text(encoding="utf-8"))
    if not isinstance(records, dict):
        raise ValueError("CRUD-RAG split_merged.json must be an object keyed by task")

    tasks = {}
    for name, rows in records.items():
        if not isinstance(rows, list):
            raise ValueError(f"{name} must be a list")
        fields = sorted({field for row in rows if isinstance(row, dict) for field in row})
        tasks[name] = {
            "sample_count": len(rows),
            "fields": fields,
            "document_fields": TASK_DOCUMENT_FIELDS.get(name, []),
            "has_reference_answer": "answers" in fields or "summary" in fields or "continuing" in fields or "realContinuation" in fields,
            "has_gold_document_id": False,
        }

    files = [
        "README.md",
        "README.zh_CN.md",
        "data/crud/CRUD_Data.zip",
        "data/crud/merged.zip",
        "data/crud_split/split_merged.json",
    ]
    return {
        "schema_version": "souldrive_crud_rag_contract_audit_v0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://github.com/IAAR-Shanghai/CRUD_RAG",
        "commit": _git_head(repo),
        "code_license_signal": "README declares Apache-2.0",
        "license_file_present": (repo / "LICENSE").is_file(),
        "data_license_status": "unresolved: repository has no LICENSE file; do not redistribute data without a separate data license confirmation",
        "tasks": tasks,
        "total_samples": sum(item["sample_count"] for item in tasks.values()),
        "evaluation_boundary": {
            "allowed_first_round": ["answer_reference_metrics", "summary_reference_metrics", "hallucination_detection_metrics"],
            "not_allowed_without_added_gold": ["Recall@K", "MRR", "nDCG"],
            "reason": "official split rows contain source texts and references but no gold retrieval document IDs/qrels",
        },
        "files": [
            {"path": relative, "bytes": (repo / relative).stat().st_size, "sha256": _sha256(repo / relative)}
            for relative in files
            if (repo / relative).is_file()
        ],
        "notes": [
            "This audit reads JSON and file metadata only; it executes no third-party code.",
            "CRUD-RAG remains outside the SoulDrive product workspace and is not mixed into GraphRAG documents.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = audit(args.repo.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
