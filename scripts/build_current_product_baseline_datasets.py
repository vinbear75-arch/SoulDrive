"""Build live-evaluation inputs for the versioned local product corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "souldrive_product_benchmark_manifest_v0.1":
        raise ValueError("unsupported product corpus manifest")
    if not payload.get("assets"):
        raise ValueError("product corpus manifest has no assets")
    return payload


def build_ingestion_records(manifest: dict) -> list[dict]:
    records = []
    for index, asset in enumerate(manifest["assets"], start=1):
        records.append(
            {
                "id": f"CP-ING-{index:03d}",
                "doc_id": asset["asset_id"],
                "project_id": asset.get("project_id"),
                "source_filename": asset["filename"],
                "language": "zh",
                "document_class": asset["asset_type"],
                "evaluation_scope": "current_product_asset_integrity",
                "difficulty": "medium",
                "min_chunk_count": 1,
                "required_metadata": [
                    "page",
                    "document_context",
                    "retrieval_context",
                    "source_provenance",
                    "token_safe",
                    "content_integrity",
                ],
                "required_modalities": [],
                "focus": ["当前产品语料", asset["asset_type"], "来源溯源", "正文完整性"],
            }
        )
    return records


def build_rag_records(manifest: dict) -> list[dict]:
    records = []
    for index, asset in enumerate(manifest["assets"], start=1):
        text = " ".join(str(asset.get("text") or "").split())
        phrase = text[:48] or asset["title"]
        records.append(
            {
                "id": f"CP-RAG-{index:03d}",
                "question": f"请从当前项目语料中查找“{asset['title']}”里关于“{phrase}”的记录。",
                "expected_sources": [asset["filename"]],
                "project_id": asset.get("project_id"),
                "type": "current_product_corpus_lookup",
                "rubric": ["source_traceability"],
                "expected_keywords": [asset["title"]],
                "should_refuse": False,
                "requires_multi_doc": False,
                "graph_relevant": False,
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Build current product corpus evaluation inputs.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--ingestion-output", required=True, type=Path)
    parser.add_argument("--rag-output", required=True, type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest.resolve())
    ingestion = build_ingestion_records(manifest)
    rag = build_rag_records(manifest)
    args.ingestion_output.parent.mkdir(parents=True, exist_ok=True)
    args.rag_output.parent.mkdir(parents=True, exist_ok=True)
    args.ingestion_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in ingestion),
        encoding="utf-8",
    )
    args.rag_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rag),
        encoding="utf-8",
    )
    print(json.dumps({"ingestion_samples": len(ingestion), "rag_samples": len(rag)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
