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
    args = parser.parse_args(argv)

    records = load_dataset(Path(args.dataset))
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
        rows = conn.execute("SELECT content, metadata_json FROM keyword_documents").fetchall()
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


def summarize(records: list[dict[str, Any]], chunks: list[ChunkRecord | dict[str, Any]]) -> dict[str, Any]:
    normalized_chunks = [normalize_chunk(chunk) for chunk in chunks]
    rows = [score_record(record, normalized_chunks) for record in records]
    all_matched_chunks = [chunk for row in rows for chunk in row["matched_chunks"]]
    table_rows = [row for row in rows if "table" in row["required_modalities"]]
    image_rows = [row for row in rows if "image" in row["required_modalities"]]
    overlap_rows = [row for row in rows if "paragraph_overlap" in row["required_metadata"]]
    section_rows = [row for row in rows if "section" in row["required_metadata"]]

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
        "paragraph_overlap_rate": ratio(sum(row["paragraph_overlap_pass"] for row in overlap_rows), len(overlap_rows)),
        "table_coverage_rate": ratio(sum(row["table_pass"] for row in table_rows), len(table_rows)),
        "image_metadata_rate": ratio(sum(row["image_pass"] for row in image_rows), len(image_rows)),
        "average_chunk_chars": int(sum(len(chunk.content) for chunk in all_matched_chunks) / len(all_matched_chunks)) if all_matched_chunks else None,
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
        if source_matches(str(chunk.metadata.get("source_filename") or ""), source_filename)
    ]
    min_chunk_count = int(record.get("min_chunk_count") or 1)
    document_present = bool(matched_chunks)
    chunk_count_pass = len(matched_chunks) >= min_chunk_count
    page_metadata_pass = all(chunk_has_page(chunk) for chunk in matched_chunks) if matched_chunks else False
    section_metadata_pass = any(chunk_has_section(chunk) for chunk in matched_chunks) if "section" in required_metadata else True
    dynamic_chunk_pass = any(chunk_is_dynamic(chunk) for chunk in matched_chunks) if "dynamic_chunk" in required_metadata else True
    paragraph_overlap_pass = any(chunk_overlap_chars(chunk) > 0 for chunk in matched_chunks) if "paragraph_overlap" in required_metadata else True
    table_pass = any(chunk_has_table(chunk) for chunk in matched_chunks) if "table" in required_modalities else True
    image_pass = any(chunk_has_image_metadata(chunk) for chunk in matched_chunks) if "image" in required_modalities else True
    checks = [
        document_present,
        chunk_count_pass,
        page_metadata_pass if "page" in required_metadata else True,
        section_metadata_pass,
        dynamic_chunk_pass,
        paragraph_overlap_pass,
        table_pass,
        image_pass,
    ]
    return {
        "id": str(record.get("id") or ""),
        "source_filename": source_filename,
        "document_class": str(record.get("document_class") or ""),
        "required_metadata": required_metadata,
        "required_modalities": required_modalities,
        "matched_chunk_count": len(matched_chunks),
        "document_present": document_present,
        "chunk_count_pass": chunk_count_pass,
        "page_metadata_pass": page_metadata_pass,
        "section_metadata_pass": section_metadata_pass,
        "dynamic_chunk_pass": dynamic_chunk_pass,
        "paragraph_overlap_pass": paragraph_overlap_pass,
        "table_pass": table_pass,
        "image_pass": image_pass,
        "pass": all(checks),
        "matched_chunks": matched_chunks,
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
    return str(chunk.metadata.get("chunk_strategy") or "") == "dynamic_structure_overlap_v1"


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
    print(f"段落重叠达标率: {format_percent(report['paragraph_overlap_rate'])}")
    print(f"表格保留覆盖率: {format_percent(report['table_coverage_rate'])}")
    print(f"图片元数据覆盖率: {format_percent(report['image_metadata_rate'])}")
    print(f"平均切片字符数: {report['average_chunk_chars'] if report['average_chunk_chars'] is not None else '无样本'}")
    print(f"综合清洗切片质量: {format_percent(report['overall_ingestion_quality_rate'])}")

    failed = [row for row in report["rows"] if not row["pass"]]
    if failed:
        print("\n未达标样本:")
        for row in failed[:10]:
            print(f"- {row['id']}: {row['source_filename']} matched_chunks={row['matched_chunk_count']}")


def format_percent(value: float | None) -> str:
    if value is None:
        return "无样本"
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main(sys.argv[1:]))
