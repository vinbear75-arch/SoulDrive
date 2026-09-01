"""Create a deterministic, local CRUD-RAG evaluation slice with derived retrieval IDs.

CRUD-RAG's split contains source texts and reference answers but no official qrels.
This script therefore labels its own source fields as *derived* gold evidence.  It
is useful for SoulDrive regression testing, never as an official retrieval score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


QA_TASKS = ("questanswer_1doc", "questanswer_2docs", "questanswer_3docs")
TASK_COUNTS = {"questanswer_1doc": 24, "questanswer_2docs": 24, "questanswer_3docs": 24, "event_summary": 16, "hallu_modified": 16}


def _stable_rows(rows: list[dict[str, Any]], *, seed: str, count: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: hashlib.sha256(f"{seed}:{row.get('ID', '')}".encode("utf-8")).hexdigest())[:count]


def _split(index: int) -> str:
    return "calibration" if index % 2 == 0 else "sealed_test"


def freeze(source: Path, output: Path, *, seed: str = "souldrive-crud-rag-v1") -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    selected = {
        task: _stable_rows(list(payload[task]), seed=f"{seed}:{task}", count=count)
        for task, count in TASK_COUNTS.items()
    }
    output.mkdir(parents=True, exist_ok=True)
    documents: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []

    def add_document(task: str, sample_id: str, field: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        document_id = f"crud-derived:{digest[:20]}"
        documents.setdefault(document_id, {
            "document_id": document_id,
            "origin": "CRUD-RAG official split source field",
            "task": task,
            "source_sample_id": sample_id,
            "source_field": field,
            "text": text,
        })
        return document_id

    for task, task_rows in selected.items():
        for task_index, item in enumerate(task_rows):
            sample_id = str(item["ID"])
            split = _split(len(rows))
            if task in QA_TASKS:
                fields = [field for field in ("news1", "news2", "news3") if str(item.get(field) or "").strip()]
                gold_ids = [add_document(task, sample_id, field, str(item[field])) for field in fields]
                rows.append({
                    "sample_id": f"{task}:{sample_id}", "split": split, "task": "qa",
                    "source_task": task, "question": str(item["questions"]), "reference_answer": str(item["answers"]),
                    "derived_gold_document_ids": gold_ids, "gold_kind": "derived_from_official_source_fields",
                })
            elif task == "event_summary":
                gold_id = add_document(task, sample_id, "text", str(item["text"]))
                rows.append({
                    "sample_id": f"{task}:{sample_id}", "split": split, "task": "summary",
                    "source_task": task, "question": "请忠实概括这篇新闻的核心事件。", "reference_answer": str(item["summary"]),
                    "derived_gold_document_ids": [gold_id], "gold_kind": "derived_from_official_source_fields",
                })
            else:
                gold_id = add_document(task, sample_id, "newsBeginning", str(item["newsBeginning"]))
                rows.append({
                    "sample_id": f"{task}:{sample_id}", "split": split, "task": "hallucination_detection",
                    "source_task": task,
                    "question": "给定原文开头和候选续写，识别候选续写是否包含原文不支持的信息，并说明依据。",
                    "reference_answer": str(item["realContinuation"]), "candidate_answer": str(item["hallucinatedContinuation"]),
                    "unsupported_claims": item.get("allKeywords") or {}, "derived_gold_document_ids": [gold_id],
                    "gold_kind": "derived_from_official_source_fields",
                })

    (output / "corpus.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in documents.values()), encoding="utf-8")
    (output / "samples.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")
    manifest = {
        "schema_version": "souldrive_crud_rag_local_slice_v0.1", "source_file": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "selection_seed": seed,
        "sample_count": len(rows), "corpus_document_count": len(documents),
        "task_counts": dict(Counter(item["task"] for item in rows)), "split_counts": dict(Counter(item["split"] for item in rows)),
        "retrieval_metric_policy": "derived_retrieval_only_not_official_crud_rag_qrels",
        "limitations": ["No official qrels are supplied by CRUD-RAG split_merged.json.", "Do not tune on sealed_test or claim an official CRUD-RAG retrieval ranking."],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", default="souldrive-crud-rag-v1")
    args = parser.parse_args()
    print(json.dumps(freeze(args.source.resolve(), args.output.resolve(), seed=args.seed), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
