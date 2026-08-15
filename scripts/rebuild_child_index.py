"""Rebuild child, vector, and keyword indexes from persisted parent documents."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.indexer import INDEX_METADATA_SCHEMA_VERSION
from core.knowledge_base import LocalKnowledgeBase
from core.workspace import SoulDriveWorkspace


PARENT_ID_PATTERN = re.compile(r"^(?P<document_id>.+)_parent_(?P<parent_index>\d+)$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--target-workspace", type=Path, required=True)
    parser.add_argument("--allow-model-change", action="store_true")
    args = parser.parse_args()

    source = SoulDriveWorkspace(str(args.source_workspace.resolve())).ensure()
    target = SoulDriveWorkspace(str(args.target_workspace.resolve())).ensure()
    if Path(source.root_path) == Path(target.root_path):
        raise ValueError("source and target workspaces must be different")

    groups, source_models = load_parent_groups(Path(source.parent_doc_path))
    if not groups:
        raise ValueError("source parent index is empty")

    kb = LocalKnowledgeBase(workspace_path=target.root_path)
    started_at = time.time()
    summaries = []
    try:
        if kb.collection.count() > 0:
            raise ValueError("target vector index must be empty")
        if source_models and kb.embedding_model_name not in source_models and not args.allow_model_change:
            raise ValueError(
                "embedding model changed; pass --allow-model-change to rebuild intentionally: "
                f"source={sorted(source_models)}, target={kb.embedding_model_name}"
            )

        for group_index, group in enumerate(groups, start=1):
            document_id = upgraded_document_id(group["document_id"], group["chunks"])
            for chunk in group["chunks"]:
                chunk.metadata["metadata_schema_version"] = INDEX_METADATA_SCHEMA_VERSION
                chunk.metadata["embedding_model_name"] = kb.embedding_model_name
            summary = kb.ingest_chunks(document_id, group["chunks"])
            summaries.append({
                "document_id": document_id,
                "source_filename": group["source_filename"],
                **summary,
            })
            print(
                f"[{group_index}/{len(groups)}] {group['source_filename']}: "
                f"parents={summary['parent_count']}, children={summary['child_count']}, "
                f"boundary_overlap={summary['boundary_overlap_count']}/"
                f"{summary['boundary_overlap_opportunity_count']}"
            )
    finally:
        kb.close()

    report = summarize_rebuild(
        source_workspace=source.root_path,
        target_workspace=target.root_path,
        embedding_model=kb.embedding_model_name,
        summaries=summaries,
        elapsed_seconds=time.time() - started_at,
    )
    report_path = Path(target.runtime_path) / "child_index_rebuild.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["totals"], ensure_ascii=False))


def load_parent_groups(parent_db_path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    if not parent_db_path.is_file():
        raise FileNotFoundError(f"parent index not found: {parent_db_path}")

    grouped: dict[str, list[tuple[int, SimpleNamespace]]] = defaultdict(list)
    source_names: dict[str, str] = {}
    source_models = set()
    with sqlite3.connect(parent_db_path) as database:
        rows = database.execute(
            "SELECT parent_id, content, metadata_json FROM parent_documents"
        ).fetchall()

    for parent_id, content, metadata_json in rows:
        match = PARENT_ID_PATTERN.match(str(parent_id))
        if not match:
            raise ValueError(f"invalid parent id: {parent_id}")
        document_id = match.group("document_id")
        parent_index = int(match.group("parent_index"))
        metadata = json.loads(metadata_json)
        metadata.pop("parent_id", None)
        metadata.pop("parent_index", None)
        metadata.pop("child_ids", None)
        source_name = str(
            metadata.get("source_filename")
            or metadata.get("document_title")
            or document_id
        )
        source_names[document_id] = source_name
        source_model = str(metadata.get("embedding_model_name") or "").strip()
        if source_model:
            source_models.add(source_model)
        grouped[document_id].append(
            (parent_index, SimpleNamespace(page_content=str(content), metadata=metadata))
        )

    groups = []
    for document_id, records in grouped.items():
        records.sort(key=lambda item: item[0])
        groups.append({
            "document_id": document_id,
            "source_filename": source_names[document_id],
            "chunks": [chunk for _index, chunk in records],
        })
    groups.sort(key=lambda item: item["source_filename"].lower())
    return groups, source_models


def upgraded_document_id(document_id: str, chunks: list[SimpleNamespace]) -> str:
    document_hash = str(chunks[0].metadata.get("document_hash") or "").strip()
    if document_hash:
        return f"{document_hash}_schema_{INDEX_METADATA_SCHEMA_VERSION}"
    if re.search(r"_schema_\d+$", document_id):
        return re.sub(
            r"_schema_\d+$",
            f"_schema_{INDEX_METADATA_SCHEMA_VERSION}",
            document_id,
        )
    return f"{document_id}_schema_{INDEX_METADATA_SCHEMA_VERSION}"


def summarize_rebuild(
    *,
    source_workspace: str,
    target_workspace: str,
    embedding_model: str,
    summaries: list[dict[str, Any]],
    elapsed_seconds: float,
) -> dict[str, Any]:
    additive = (
        "parent_count",
        "child_count",
        "formal_window_count",
        "boundary_overlap_opportunity_count",
        "boundary_overlap_count",
        "boundary_overlap_truncated_count",
        "boundary_overlap_token_skip_count",
        "parent_store_ms",
        "child_split_ms",
        "keyword_index_ms",
        "embedding_ms",
        "vector_store_ms",
    )
    totals = {
        key: sum(int(summary.get(key) or 0) for summary in summaries)
        for key in additive
    }
    opportunities = totals["boundary_overlap_opportunity_count"]
    totals.update({
        "document_count": len(summaries),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "boundary_overlap_coverage_rate": round(
            totals["boundary_overlap_count"] / opportunities,
            6,
        ) if opportunities else None,
        "max_child_tokens": max(
            (int(summary.get("max_child_tokens") or 0) for summary in summaries),
            default=0,
        ),
        "max_embedding_input_tokens": max(
            (int(summary.get("max_embedding_input_tokens") or 0) for summary in summaries),
            default=0,
        ),
    })
    return {
        "schema_version": "child_index_rebuild_v1",
        "index_metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
        "source_workspace": source_workspace,
        "target_workspace": target_workspace,
        "embedding_model": embedding_model,
        "totals": totals,
        "documents": summaries,
    }


if __name__ == "__main__":
    main()
