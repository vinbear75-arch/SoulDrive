import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.secure_vector_store import SecureVectorStore
from core.security_context import get_workspace_keys
from core.evaluation import wilson_interval
from core.workspace import SoulDriveWorkspace
from scripts import eval_rag


DEFAULT_DATASET = PROJECT_ROOT / "eval" / "ingestion_quality_v0.1.jsonl"


@dataclass(frozen=True)
class ChunkRecord:
    content: str
    metadata: dict[str, Any]


def configure_utf8_stdio() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive document cleaning, chunking, and ingestion quality.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Ingestion evaluation dataset JSONL path.")
    parser.add_argument("--workspace", default=None, help="SoulDrive workspace root to inspect.")
    parser.add_argument("--passphrase", default=None, help="Workspace passphrase for encrypted indexes.")
    parser.add_argument("--chunks-jsonl", default=None, help="Read indexed chunk records from JSONL instead of a workspace.")
    parser.add_argument("--save-report", default=None, help="Optional JSON report output path.")
    parser.add_argument("--audit-dataset-only", action="store_true", help="Only audit ingestion dataset breadth and depth.")
    args = parser.parse_args(argv)

    records = load_dataset(Path(args.dataset))
    if args.audit_dataset_only:
        quality = dataset_quality(records)
        report = {"mode": "评测集质量审计", "dataset_quality": quality}
        if args.save_report:
            save_report(Path(args.save_report), report)
        print_dataset_quality(quality)
        return 0 if quality["pass"] else 1

    if args.chunks_jsonl:
        chunks = load_chunks_jsonl(Path(args.chunks_jsonl))
    else:
        workspace = eval_rag.resolve_workspace_arg(args.workspace)
        if not workspace:
            parser.error("pass --workspace <path> or --chunks-jsonl <path>")
        eval_rag.prepare_workspace_keys(workspace, args.passphrase)
        chunks = collect_workspace_chunks(workspace)

    report = summarize(records, chunks)
    if args.save_report:
        save_report(Path(args.save_report), report)
    print_report(report)
    return 0


def load_dataset(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        payload["_line_number"] = line_number
        records.append(payload)
    return records


def load_chunks_jsonl(path: Path) -> list[ChunkRecord]:
    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        chunks.append(
            ChunkRecord(
                content=str(payload.get("content") or payload.get("document") or ""),
                metadata=dict(payload.get("metadata") or payload.get("metadatas") or {}),
            )
        )
    return chunks


def collect_workspace_chunks(workspace_path: str) -> list[ChunkRecord]:
    workspace = SoulDriveWorkspace(workspace_path).ensure()
    keys = get_workspace_keys(workspace.root_path)
    if keys is not None and Path(workspace.secure_vector_store_path).exists():
        store = SecureVectorStore(workspace.secure_vector_store_path, keys)
        try:
            payload = store.get(include=["documents", "metadatas"])
            return [
                ChunkRecord(content=str(content or ""), metadata=dict(metadata or {}))
                for content, metadata in zip(payload.get("documents", []), payload.get("metadatas", []))
            ]
        finally:
            store.close()
    return collect_plain_keyword_chunks(workspace.keyword_index_path)


def collect_plain_keyword_chunks(keyword_index_path: str) -> list[ChunkRecord]:
    path = Path(keyword_index_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        table_name = keyword_table_name(conn)
        if not table_name:
            return []
        rows = conn.execute(
            f"SELECT content, metadata_json FROM {table_name}"
        ).fetchall()
    finally:
        conn.close()
    chunks = []
    for content, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        chunks.append(ChunkRecord(content=str(content or ""), metadata=metadata))
    return chunks


def keyword_table_name(conn: sqlite3.Connection) -> str | None:
    available = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for candidate in ("keyword_documents_v2", "keyword_documents"):
        if candidate in available:
            return candidate
    return None


def summarize(records: list[dict[str, Any]], chunks: list[ChunkRecord | dict[str, Any]]) -> dict[str, Any]:
    normalized_chunks = [normalize_chunk(chunk) for chunk in chunks]
    rows = [score_record(record, normalized_chunks) for record in records]
    all_matched_chunks = []
    seen_chunks = set()
    for row in rows:
        for chunk in row["matched_chunks"]:
            chunk_key = id(chunk)
            if chunk_key in seen_chunks:
                continue
            seen_chunks.add(chunk_key)
            all_matched_chunks.append(chunk)
    table_rows = [row for row in rows if "table" in row["required_modalities"]]
    image_rows = [row for row in rows if "image" in row["required_modalities"]]
    overlap_rows = [row for row in rows if "paragraph_overlap" in row["required_metadata"]]
    applicable_overlap_rows = [row for row in overlap_rows if row["paragraph_overlap_applicable"]]
    section_rows = [row for row in rows if "section" in row["required_metadata"]]
    formal_window_rows = [row for row in rows if "formal_window" in row["required_metadata"]]
    applicable_formal_window_rows = [
        row for row in formal_window_rows if row["formal_window_applicable"]
    ]
    document_context_rows = [row for row in rows if "document_context" in row["required_metadata"]]
    retrieval_context_rows = [row for row in rows if "retrieval_context" in row["required_metadata"]]
    provenance_rows = [row for row in rows if "source_provenance" in row["required_metadata"]]
    parent_linkage_rows = [row for row in rows if "parent_linkage" in row["required_metadata"]]
    semantic_unit_rows = [row for row in rows if "semantic_units" in row["required_metadata"]]
    index_schema_rows = [row for row in rows if "index_schema" in row["required_metadata"]]
    content_integrity_rows = [row for row in rows if "content_integrity" in row["required_metadata"]]
    token_safe_rows = [row for row in rows if "token_safe" in row["required_metadata"]]

    return {
        "mode": "清洗切片入库评测",
        "sample_count": len(rows),
        "chunk_count": len(normalized_chunks),
        "matched_chunk_count": len(all_matched_chunks),
        "document_coverage_rate": ratio(sum(row["document_present"] for row in rows), len(rows)),
        "chunk_count_pass_rate": ratio(sum(row["chunk_count_pass"] for row in rows), len(rows)),
        "page_metadata_rate": ratio(sum(chunk_has_page(chunk) for chunk in all_matched_chunks), len(all_matched_chunks)),
        "section_metadata_rate": ratio(sum(row["section_metadata_pass"] for row in section_rows), len(section_rows)),
        "dynamic_chunk_rate": ratio(sum(chunk_is_dynamic(chunk) for chunk in all_matched_chunks), len(all_matched_chunks)),
        "paragraph_overlap_applicability_rate": ratio(
            len(applicable_overlap_rows),
            len(overlap_rows),
        ),
        "paragraph_overlap_rate": ratio(
            sum(row["paragraph_overlap_pass"] for row in applicable_overlap_rows),
            len(applicable_overlap_rows),
        ),
        "formal_window_applicability_rate": ratio(
            len(applicable_formal_window_rows),
            len(formal_window_rows),
        ),
        "formal_window_rate": ratio(
            sum(row["formal_window_pass"] for row in applicable_formal_window_rows),
            len(applicable_formal_window_rows),
        ),
        "document_context_rate": ratio(
            sum(row["document_context_pass"] for row in document_context_rows),
            len(document_context_rows),
        ),
        "retrieval_context_rate": ratio(
            sum(row["retrieval_context_pass"] for row in retrieval_context_rows),
            len(retrieval_context_rows),
        ),
        "source_provenance_rate": ratio(
            sum(row["source_provenance_pass"] for row in provenance_rows),
            len(provenance_rows),
        ),
        "parent_linkage_rate": ratio(
            sum(row["parent_linkage_pass"] for row in parent_linkage_rows),
            len(parent_linkage_rows),
        ),
        "semantic_unit_rate": ratio(
            sum(row["semantic_units_pass"] for row in semantic_unit_rows),
            len(semantic_unit_rows),
        ),
        "index_schema_rate": ratio(
            sum(row["index_schema_pass"] for row in index_schema_rows),
            len(index_schema_rows),
        ),
        "content_integrity_rate": ratio(
            sum(row["content_integrity_pass"] for row in content_integrity_rows),
            len(content_integrity_rows),
        ),
        "token_safe_rate": ratio(sum(row["token_safe_pass"] for row in token_safe_rows), len(token_safe_rows)),
        "table_coverage_rate": ratio(sum(row["table_pass"] for row in table_rows), len(table_rows)),
        "image_metadata_rate": ratio(sum(row["image_pass"] for row in image_rows), len(image_rows)),
        "average_chunk_chars": int(sum(len(chunk.content) for chunk in all_matched_chunks) / len(all_matched_chunks)) if all_matched_chunks else None,
        "chunk_char_distribution": chunk_char_distribution(normalized_chunks),
        "oversized_chunk_rate": ratio(sum(len(chunk.content) > 1800 for chunk in normalized_chunks), len(normalized_chunks)),
        "missing_page_chunk_count": sum(not chunk_has_page(chunk) for chunk in normalized_chunks),
        "missing_section_chunk_count": sum(not chunk_has_section(chunk) for chunk in normalized_chunks),
        "dataset_quality": dataset_quality(records),
        "scope_metrics": grouped_pass_rates(rows, "evaluation_scope"),
        "document_class_metrics": grouped_pass_rates(rows, "document_class"),
        "difficulty_metrics": grouped_pass_rates(rows, "difficulty"),
        "overall_ingestion_quality_rate": ratio(sum(row["pass"] for row in rows), len(rows)),
        "rows": [
            {key: value for key, value in row.items() if key != "matched_chunks"}
            for row in rows
        ],
    }


def score_record(record: dict[str, Any], chunks: list[ChunkRecord]) -> dict[str, Any]:
    required_metadata = [str(item) for item in record.get("required_metadata", [])]
    required_modalities = [str(item) for item in record.get("required_modalities", [])]
    source_filename = str(record.get("source_filename") or "")
    matched_chunks = [
        chunk
        for chunk in chunks
        if source_matches(
            str(
                chunk.metadata.get("source_filename")
                or chunk.metadata.get("document_title")
                or ""
            ),
            source_filename,
        )
    ]
    min_chunk_count = int(record.get("min_chunk_count") or 1)
    document_present = bool(matched_chunks)
    chunk_count_pass = len(matched_chunks) >= min_chunk_count
    page_metadata_pass = all(chunk_has_page(chunk) for chunk in matched_chunks) if matched_chunks else False
    section_metadata_pass = any(chunk_has_section(chunk) for chunk in matched_chunks) if "section" in required_metadata else True
    dynamic_chunk_pass = any(chunk_is_dynamic(chunk) for chunk in matched_chunks) if "dynamic_chunk" in required_metadata else True
    formal_window_applicable = formal_paragraph_window_applicable(matched_chunks)
    paragraph_overlap_applicable = formal_window_applicable
    paragraph_overlap_pass = (
        not paragraph_overlap_applicable
        or any(
            chunk_is_prose_or_clause(chunk) and chunk_overlap_chars(chunk) > 0
            for chunk in matched_chunks
        )
    ) if "paragraph_overlap" in required_metadata else True
    formal_window_pass = (
        not formal_window_applicable
        or any(
            chunk_is_prose_or_clause(chunk) and chunk_has_formal_window(chunk)
            for chunk in matched_chunks
        )
        if "formal_window" in required_metadata
        else True
    )
    document_context_pass = (
        bool(matched_chunks) and all(chunk_has_document_context(chunk) for chunk in matched_chunks)
        if "document_context" in required_metadata
        else True
    )
    retrieval_context_pass = (
        bool(matched_chunks) and all(chunk_has_retrieval_context(chunk) for chunk in matched_chunks)
        if "retrieval_context" in required_metadata
        else True
    )
    source_provenance_pass = (
        bool(matched_chunks) and all(chunk_has_source_provenance(chunk) for chunk in matched_chunks)
        if "source_provenance" in required_metadata
        else True
    )
    parent_linkage_pass = (
        bool(matched_chunks) and all(chunk_has_parent_linkage(chunk) for chunk in matched_chunks)
        if "parent_linkage" in required_metadata
        else True
    )
    semantic_units_pass = (
        bool(matched_chunks) and all(chunk_has_semantic_units(chunk) for chunk in matched_chunks)
        if "semantic_units" in required_metadata
        else True
    )
    index_schema_pass = (
        bool(matched_chunks) and all(chunk_has_index_schema(chunk) for chunk in matched_chunks)
        if "index_schema" in required_metadata
        else True
    )
    content_integrity_pass = (
        bool(matched_chunks) and all(chunk_has_content_integrity(chunk) for chunk in matched_chunks)
        if "content_integrity" in required_metadata
        else True
    )
    token_safe_pass = (
        bool(matched_chunks) and all(chunk_is_token_safe(chunk) for chunk in matched_chunks)
        if "token_safe" in required_metadata
        else True
    )
    table_pass = any(chunk_has_table(chunk) for chunk in matched_chunks) if "table" in required_modalities else True
    image_pass = any(chunk_has_image_metadata(chunk) for chunk in matched_chunks) if "image" in required_modalities else True
    checks = [
        document_present,
        chunk_count_pass,
        page_metadata_pass if "page" in required_metadata else True,
        section_metadata_pass,
        dynamic_chunk_pass,
        paragraph_overlap_pass,
        formal_window_pass,
        document_context_pass,
        retrieval_context_pass,
        source_provenance_pass,
        parent_linkage_pass,
        semantic_units_pass,
        index_schema_pass,
        content_integrity_pass,
        token_safe_pass,
        table_pass,
        image_pass,
    ]
    return {
        "id": str(record.get("id") or ""),
        "source_filename": source_filename,
        "document_class": str(record.get("document_class") or ""),
        "evaluation_scope": str(record.get("evaluation_scope") or "unknown"),
        "difficulty": str(record.get("difficulty") or "unknown"),
        "required_metadata": required_metadata,
        "required_modalities": required_modalities,
        "matched_chunk_count": len(matched_chunks),
        "document_present": document_present,
        "chunk_count_pass": chunk_count_pass,
        "page_metadata_pass": page_metadata_pass,
        "section_metadata_pass": section_metadata_pass,
        "dynamic_chunk_pass": dynamic_chunk_pass,
        "paragraph_overlap_applicable": paragraph_overlap_applicable,
        "paragraph_overlap_pass": paragraph_overlap_pass,
        "formal_window_applicable": formal_window_applicable,
        "formal_window_pass": formal_window_pass,
        "document_context_pass": document_context_pass,
        "retrieval_context_pass": retrieval_context_pass,
        "source_provenance_pass": source_provenance_pass,
        "parent_linkage_pass": parent_linkage_pass,
        "semantic_units_pass": semantic_units_pass,
        "index_schema_pass": index_schema_pass,
        "content_integrity_pass": content_integrity_pass,
        "token_safe_pass": token_safe_pass,
        "table_pass": table_pass,
        "image_pass": image_pass,
        "pass": all(checks),
        "matched_chunks": matched_chunks,
    }


def grouped_pass_rates(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group = str(row.get(key) or "unknown")
        groups.setdefault(group, []).append(row)
    return {
        group: {
            "sample_count": len(group_rows),
            "pass_rate": ratio(
                sum(bool(row.get("pass")) for row in group_rows),
                len(group_rows),
            ),
            "confidence_interval_95": wilson_interval(
                sum(bool(row.get("pass")) for row in group_rows),
                len(group_rows),
            ),
        }
        for group, group_rows in sorted(groups.items())
    }


def normalize_chunk(chunk: ChunkRecord | dict[str, Any]) -> ChunkRecord:
    if isinstance(chunk, ChunkRecord):
        return chunk
    return ChunkRecord(
        content=str(chunk.get("content") or chunk.get("document") or ""),
        metadata=dict(chunk.get("metadata") or chunk.get("metadatas") or {}),
    )


def source_matches(source_filename: str, expected_filename: str) -> bool:
    if not source_filename or not expected_filename:
        return False
    return eval_rag.source_matches(source_filename, expected_filename) or eval_rag.source_matches(expected_filename, source_filename)


def chunk_has_page(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return metadata.get("page") is not None or metadata.get("page_number") is not None


def chunk_has_section(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return bool(
        metadata.get("Header 1")
        or metadata.get("Header 2")
        or metadata.get("Header 3")
        or metadata.get("section")
        or metadata.get("section_path")
    )


def chunk_is_dynamic(chunk: ChunkRecord) -> bool:
    return str(chunk.metadata.get("chunk_strategy") or "") in {
        "dynamic_structure_overlap_v1",
        "formal_structure_paragraph_window_v2",
    }


def chunk_has_formal_window(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return (
        str(metadata.get("chunk_strategy") or "") == "formal_structure_paragraph_window_v2"
        and int(metadata.get("paragraph_window_size") or 0) >= 2
    )


def chunk_is_prose_or_clause(chunk: ChunkRecord) -> bool:
    content_kind = str(chunk.metadata.get("content_kind") or "prose").lower()
    return content_kind in {"prose", "clause"}


def formal_paragraph_window_applicable(chunks: list[ChunkRecord]) -> bool:
    eligible = [chunk for chunk in chunks if chunk_is_prose_or_clause(chunk)]
    if any(int(chunk.metadata.get("semantic_unit_count") or 0) >= 2 for chunk in eligible):
        return True

    ordered = sorted(
        eligible,
        key=lambda chunk: (
            _metadata_int(chunk.metadata, "parent_index"),
            _metadata_int(chunk.metadata, "child_index"),
        ),
    )
    previous_by_parent: list[ChunkRecord] = []
    seen_parent_ids = set()
    for chunk in ordered:
        parent_id = str(chunk.metadata.get("parent_id") or "")
        if parent_id and parent_id in seen_parent_ids:
            continue
        if parent_id:
            seen_parent_ids.add(parent_id)
        previous_by_parent.append(chunk)

    for previous, current in zip(previous_by_parent, previous_by_parent[1:]):
        previous_section = _chunk_section_identity(previous)
        if not previous_section or previous_section != _chunk_section_identity(current):
            continue
        previous_page = _chunk_page(previous)
        current_page = _chunk_page(current)
        if previous_page and current_page and not 0 <= current_page - previous_page <= 1:
            continue
        return True
    return False


def _chunk_section_identity(chunk: ChunkRecord) -> str:
    metadata = chunk.metadata
    section_path = metadata.get("section_path")
    if isinstance(section_path, (list, tuple)):
        parts = [str(item).strip() for item in section_path if str(item).strip()]
    elif section_path:
        parts = [part.strip() for part in str(section_path).split(">") if part.strip()]
    else:
        parts = [
            str(metadata.get(key) or "").strip()
            for key in ("Header 1", "Header 2", "Header 3", "section")
            if str(metadata.get(key) or "").strip()
        ]
    return " > ".join(parts)


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    try:
        return int(metadata.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _chunk_page(chunk: ChunkRecord) -> int:
    metadata = chunk.metadata
    return _metadata_int(metadata, "page_number") or _metadata_int(metadata, "page")


def chunk_has_document_context(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return bool(metadata.get("document_title") and metadata.get("document_profile"))


def chunk_has_retrieval_context(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return bool(metadata.get("retrieval_context") and metadata.get("document_title"))


def chunk_has_source_provenance(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return bool(
        metadata.get("source_filename")
        and metadata.get("document_hash")
        and metadata.get("metadata_schema_version")
    )


def chunk_has_parent_linkage(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    return bool(metadata.get("parent_id") and metadata.get("child_id"))


def chunk_has_semantic_units(chunk: ChunkRecord) -> bool:
    try:
        return int(chunk.metadata.get("semantic_unit_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def chunk_has_index_schema(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    try:
        schema_version = int(metadata.get("metadata_schema_version") or 0)
    except (TypeError, ValueError):
        return False
    return schema_version > 0 and bool(metadata.get("embedding_model_name"))


def chunk_has_content_integrity(chunk: ChunkRecord) -> bool:
    text = str(chunk.content or "")
    if not text.strip() or "\ufffd" in text:
        return False
    return not any(
        ord(character) < 32 and character not in "\n\r\t"
        for character in text
    )


def chunk_is_token_safe(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    try:
        token_count = int(metadata.get("embedding_input_tokens") or 0)
        token_limit = int(metadata.get("embedding_token_limit") or 0)
    except (TypeError, ValueError):
        return False
    return token_count > 0 and token_limit > 0 and token_count <= token_limit


def chunk_overlap_chars(chunk: ChunkRecord) -> int:
    try:
        return int(chunk.metadata.get("chunk_overlap_chars") or 0)
    except (TypeError, ValueError):
        return 0


def chunk_has_table(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    if int(metadata.get("table_count") or 0) > 0:
        return True
    if metadata.get("tables_json"):
        return True
    return "|" in chunk.content and "---" in chunk.content


def chunk_has_image_metadata(chunk: ChunkRecord) -> bool:
    metadata = chunk.metadata
    if int(metadata.get("image_count") or 0) > 0:
        return True
    if metadata.get("image_metadata_json"):
        return True
    return "[图像" in chunk.content or "[图片" in chunk.content


def dataset_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    document_classes = {str(record.get("document_class") or "") for record in records}
    languages = {str(record.get("language") or "") for record in records}
    evaluation_scopes = {str(record.get("evaluation_scope") or "") for record in records}
    focus_tags = {
        str(tag)
        for record in records
        for tag in record.get("focus", [])
    }
    required_metadata = {
        str(item)
        for record in records
        for item in record.get("required_metadata", [])
    }
    table_records = [record for record in records if "table" in record.get("required_modalities", [])]
    image_records = [record for record in records if "image" in record.get("required_modalities", [])]
    multimodal_records = [
        record
        for record in records
        if {"table", "image"}.issubset(set(record.get("required_modalities", [])))
    ]
    deep_chunk_records = [record for record in records if int(record.get("min_chunk_count") or 0) >= 8]
    section_records = [record for record in records if "section" in record.get("required_metadata", [])]
    document_ids = [str(record.get("doc_id") or "") for record in records]
    samples_per_document = {
        doc_id: document_ids.count(doc_id)
        for doc_id in set(document_ids)
        if doc_id
    }
    hard_records = [record for record in records if record.get("difficulty") == "hard"]

    checks = {
        "sample_count": len(records) >= 216,
        "document_count": len(samples_per_document) >= 36,
        "min_samples_per_document": min(samples_per_document.values() or [0]) >= 6,
        "document_class_count": len(document_classes) >= 4,
        "language_count": {"zh", "en"}.issubset(languages),
        "evaluation_scope_count": len(evaluation_scopes) >= 10,
        "hard_record_count": len(hard_records) >= 96,
        "enterprise_technical_plan_count": count_records(records, "document_class", "enterprise_technical_plan") >= 15,
        "government_formal_or_standard_count": (
            count_records(records, "document_class", "government_formal_document")
            + count_records(records, "document_class", "government_standard")
        ) >= 84,
        "rag_research_reference_count": count_records(records, "document_class", "rag_research_reference") >= 60,
        "table_record_count": len(table_records) >= 44,
        "image_record_count": len(image_records) >= 30,
        "multimodal_record_count": len(multimodal_records) >= 20,
        "section_metadata_record_count": len(section_records) >= 34,
        "deep_chunk_record_count": len(deep_chunk_records) >= 28,
        "focus_tag_count": len(focus_tags) >= 40,
        "required_metadata_core": {
            "page",
            "dynamic_chunk",
            "paragraph_overlap",
            "document_context",
            "token_safe",
            "formal_window",
            "retrieval_context",
            "source_provenance",
            "parent_linkage",
            "semantic_units",
            "index_schema",
            "content_integrity",
        }.issubset(required_metadata),
    }
    passed_count = sum(1 for passed in checks.values() if passed)
    return {
        "schema_version": "ingestion_dataset_quality_v2",
        "pass": all(checks.values()),
        "score": ratio(passed_count, len(checks)),
        "checks": checks,
        "sample_count": len(records),
        "document_count": len(samples_per_document),
        "min_samples_per_document": min(samples_per_document.values() or [0]),
        "document_class_count": len(document_classes),
        "language_count": len(languages),
        "evaluation_scope_count": len(evaluation_scopes),
        "hard_record_count": len(hard_records),
        "focus_tag_count": len(focus_tags),
        "table_record_count": len(table_records),
        "image_record_count": len(image_records),
        "multimodal_record_count": len(multimodal_records),
        "deep_chunk_record_count": len(deep_chunk_records),
    }


def count_records(records: list[dict[str, Any]], key: str, value: str) -> int:
    return sum(1 for record in records if record.get(key) == value)


def chunk_char_distribution(chunks: list[ChunkRecord]) -> dict[str, int | None]:
    lengths = sorted(len(chunk.content) for chunk in chunks)
    if not lengths:
        return {"min": None, "p50": None, "p90": None, "max": None}
    return {
        "min": lengths[0],
        "p50": percentile(lengths, 0.50),
        "p90": percentile(lengths, 0.90),
        "max": lengths[-1],
    }


def percentile(sorted_values: list[int], fraction: float) -> int:
    index = int((len(sorted_values) - 1) * fraction)
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


def ratio(numerator: int | float, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / denominator


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_report(report: dict[str, Any]) -> None:
    print(f"评测模式: {report['mode']}")
    print(f"清洗切片评测样本数: {report['sample_count']}")
    print(f"索引切片数: {report['chunk_count']}")
    print(f"文档覆盖率: {format_percent(report['document_coverage_rate'])}")
    print(f"最小切片数达标率: {format_percent(report['chunk_count_pass_rate'])}")
    print(f"页码元数据覆盖率: {format_percent(report['page_metadata_rate'])}")
    print(f"章节元数据覆盖率: {format_percent(report['section_metadata_rate'])}")
    print(f"动态切片策略覆盖率: {format_percent(report['dynamic_chunk_rate'])}")
    print(
        "段落重叠适用率/达标率: "
        f"{format_percent(report['paragraph_overlap_applicability_rate'])}/"
        f"{format_percent(report['paragraph_overlap_rate'])}"
    )
    print(
        "正式文档两段滑窗适用率/达标率: "
        f"{format_percent(report['formal_window_applicability_rate'])}/"
        f"{format_percent(report['formal_window_rate'])}"
    )
    print(f"文档上下文元数据覆盖率: {format_percent(report['document_context_rate'])}")
    print(f"检索上下文覆盖率: {format_percent(report['retrieval_context_rate'])}")
    print(f"来源溯源覆盖率: {format_percent(report['source_provenance_rate'])}")
    print(f"父子索引关联率: {format_percent(report['parent_linkage_rate'])}")
    print(f"语义单元标注率: {format_percent(report['semantic_unit_rate'])}")
    print(f"索引版本标注率: {format_percent(report['index_schema_rate'])}")
    print(f"正文完整性通过率: {format_percent(report['content_integrity_rate'])}")
    print(f"嵌入 Token 安全率: {format_percent(report['token_safe_rate'])}")
    print(f"表格保留覆盖率: {format_percent(report['table_coverage_rate'])}")
    print(f"图片元数据覆盖率: {format_percent(report['image_metadata_rate'])}")
    print(f"平均切片字符数: {report['average_chunk_chars'] if report['average_chunk_chars'] is not None else '无样本'}")
    distribution = report["chunk_char_distribution"]
    print(
        "切片字符分布: "
        f"min={distribution['min']}, p50={distribution['p50']}, "
        f"p90={distribution['p90']}, max={distribution['max']}"
    )
    print(f"超长切片比例: {format_percent(report['oversized_chunk_rate'])}")
    print(f"缺页码切片数: {report['missing_page_chunk_count']}")
    print(f"缺章节切片数: {report['missing_section_chunk_count']}")
    print_dataset_quality(report["dataset_quality"])
    print(
        "按检查面通过率: "
        + ", ".join(
            f"{name}={format_percent(metrics['pass_rate'])}"
            for name, metrics in report["scope_metrics"].items()
        )
    )
    print(f"综合清洗切片质量: {format_percent(report['overall_ingestion_quality_rate'])}")

    failed = [row for row in report["rows"] if not row["pass"]]
    if failed:
        print("\n未达标样本:")
        for row in failed[:10]:
            print(f"- {row['id']}: {row['source_filename']} matched_chunks={row['matched_chunk_count']}")


def print_dataset_quality(quality: dict[str, Any]) -> None:
    print(f"评测集质量门禁: {'通过' if quality['pass'] else '未通过'} ({format_percent(quality['score'])})")
    print(
        "评测集覆盖: "
        f"样本={quality['sample_count']}, 文档={quality['document_count']}, "
        f"每文档最少样本={quality['min_samples_per_document']}, "
        f"文档类={quality['document_class_count']}, 语言={quality['language_count']}, "
        f"scope={quality['evaluation_scope_count']}, hard样本={quality['hard_record_count']}, "
        f"focus标签={quality['focus_tag_count']}, "
        f"表格样本={quality['table_record_count']}, 图片样本={quality['image_record_count']}, "
        f"多模态样本={quality['multimodal_record_count']}, 深切片样本={quality['deep_chunk_record_count']}"
    )
    failed_checks = [name for name, passed in quality["checks"].items() if not passed]
    if failed_checks:
        print("未通过检查:")
        for name in failed_checks:
            print(f"- {name}")


def format_percent(value: float | None) -> str:
    if value is None:
        return "无样本"
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main(sys.argv[1:]))
