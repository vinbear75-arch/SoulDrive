import os
import gc
import hashlib
import shutil
import time
import uuid
from pathlib import Path
from core.document_types import SUPPORTED_DOCUMENT_EXTENSIONS, is_image_document
from core.indexing_errors import indexing_failure
from core.logging_config import get_logger
from core.observability import runtime_metrics
from core.runtime_state import update_indexing_status
from core.secure_document_store import SecureDocumentStore
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.paper_importer import file_sha256, safe_document_filename
from core.project_catalog import ProjectCatalog

logger = get_logger(__name__)


# v13 preserves log/template text, persists catalog titles, and rebuilds after
# structure-aware de-fragmentation changes.
INDEX_METADATA_SCHEMA_VERSION = 13
GRAPH_ENABLED_AUTH_LEVELS = {"PRO", "HARDWARE_PLUS_PASSWORD", "DEV", "LOCAL", "LOCAL_PC"}


def _graph_features_enabled(auth_level: str) -> bool:
    """本机解锁态也允许 GraphRAG；BASIC/LITE 仍保持轻量索引。"""
    return str(auth_level or "").strip().upper() in GRAPH_ENABLED_AUTH_LEVELS


def _document_navigation_summary(source_filename: str, chunks: list) -> str:
    """Build an extractive, source-grounded summary before child indexing."""
    sections = []
    excerpts = []
    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        section = str(metadata.get("section") or "").strip()
        if section and "未识别标题" not in section and section not in sections:
            sections.append(section)
        content = " ".join(str(getattr(chunk, "page_content", "") or "").split())
        if content:
            excerpts.append(content[:180])
        if len(sections) >= 6 and len(excerpts) >= 2:
            break
    title = Path(source_filename).stem
    parts = [f"文档《{title}》"]
    if sections:
        parts.append(f"主要章节：{'；'.join(sections[:6])}")
    if excerpts:
        parts.append(f"原文摘录：{' '.join(excerpts[:2])[:360]}")
    return "。".join(parts)[:600]


def _graph_sources_missing_coverage(snapshot: dict, source_filenames: list[str]) -> set[str]:
    """返回没有实体/关系指针覆盖的文档，供增量索引回填图谱。"""
    documents = {
        str(item.get("source_filename") or ""): {
            str(chunk_id)
            for chunk_id in item.get("chunk_ids") or []
        }
        for item in snapshot.get("documents") or []
        if str(item.get("source_filename") or "")
    }
    covered_chunks: set[str] = set()
    for entity in snapshot.get("entities") or []:
        covered_chunks.update(str(chunk_id) for chunk_id in entity.get("chunk_ids") or [])
    for relationship in snapshot.get("relationships") or []:
        covered_chunks.update(str(chunk_id) for chunk_id in relationship.get("chunk_ids") or [])

    missing = set()
    for source_filename in source_filenames:
        source = str(source_filename or "")
        chunk_ids = documents.get(source)
        if not chunk_ids or not (chunk_ids & covered_chunks):
            missing.add(source)
    return missing


def _failure_summary(failures: list[dict]):
    summary: dict[str, int] = {}
    for failure in failures:
        code = str(failure.get("error_code") or "INDEXING_FAILED")
        summary[code] = summary.get(code, 0) + 1
    return summary


def _empty_indexing_observability() -> dict:
    return {
        "schema_version": "indexing_observability_v4",
        "documents": [],
        "totals": {
            "parse_ms": 0,
            "ingest_ms": 0,
            "graph_ms": 0,
            "elapsed_ms": 0,
            "chunk_chars": 0,
            "child_count": 0,
            "embedding_ms": 0,
            "vector_store_ms": 0,
            "keyword_index_ms": 0,
            "parent_store_ms": 0,
            "child_split_ms": 0,
            "raw_items": 0,
            "kept_items": 0,
            "removed_page_artifacts": 0,
            "repeated_page_artifacts": 0,
            "removed_non_content": 0,
            "removed_duplicates": 0,
            "table_items": 0,
            "image_items": 0,
            "ocr_ms": 0,
            "ocr_region_count": 0,
            "ocr_mean_confidence": 0.0,
            "pages": 0,
            "parent_chunks": 0,
            "heading_only_merges": 0,
            "short_same_section_merges": 0,
            "conversion_error_count": 0,
            "converted_pages": 0,
            "failed_page_count": 0,
            "formal_window_count": 0,
            "boundary_overlap_opportunity_count": 0,
            "boundary_overlap_count": 0,
            "boundary_overlap_truncated_count": 0,
            "boundary_overlap_token_skip_count": 0,
            "boundary_overlap_coverage_rate": None,
            "max_child_tokens": 0,
            "max_embedding_input_tokens": 0,
            "embedding_token_limit": 0,
            "child_strategy_counts": {},
            "conversion_status_counts": {},
            "modality_counts": {},
        },
    }


def _elapsed_ms(started_at: float) -> int:
    return int((time.time() - started_at) * 1000)


def _record_document_observation(observability: dict, document: dict) -> dict:
    updated = {
        "schema_version": "indexing_observability_v4",
        "documents": [*observability.get("documents", []), document],
        "totals": dict(observability.get("totals") or {}),
    }
    totals = updated["totals"]
    previous_ocr_region_count = int(totals.get("ocr_region_count") or 0)
    previous_ocr_mean_confidence = float(totals.get("ocr_mean_confidence") or 0.0)
    for key in (
        "parse_ms",
        "ingest_ms",
        "graph_ms",
        "elapsed_ms",
        "chunk_chars",
        "child_count",
        "section_tree_node_count",
        "section_tree_leaf_count",
        "embedding_ms",
        "vector_store_ms",
        "keyword_index_ms",
        "parent_store_ms",
        "child_split_ms",
        "raw_items",
        "kept_items",
        "removed_page_artifacts",
        "repeated_page_artifacts",
        "removed_non_content",
        "removed_duplicates",
        "table_items",
        "image_items",
        "ocr_ms",
        "ocr_region_count",
        "pages",
        "parent_chunks",
        "heading_only_merges",
        "short_same_section_merges",
        "conversion_error_count",
        "converted_pages",
        "failed_page_count",
        "formal_window_count",
        "boundary_overlap_opportunity_count",
        "boundary_overlap_count",
        "boundary_overlap_truncated_count",
        "boundary_overlap_token_skip_count",
    ):
        totals[key] = int(totals.get(key) or 0) + int(document.get(key) or 0)
    document_ocr_region_count = int(document.get("ocr_region_count") or 0)
    total_ocr_region_count = int(totals.get("ocr_region_count") or 0)
    if total_ocr_region_count:
        totals["ocr_mean_confidence"] = round(
            (
                previous_ocr_mean_confidence * previous_ocr_region_count
                + float(document.get("ocr_mean_confidence") or 0.0) * document_ocr_region_count
            )
            / total_ocr_region_count,
            6,
        )
    for key in ("max_child_tokens", "max_embedding_input_tokens", "embedding_token_limit"):
        totals[key] = max(int(totals.get(key) or 0), int(document.get(key) or 0))
    strategy_counts = dict(totals.get("child_strategy_counts") or {})
    for strategy, count in (document.get("child_strategy_counts") or {}).items():
        strategy_counts[str(strategy)] = int(strategy_counts.get(str(strategy)) or 0) + int(count or 0)
    totals["child_strategy_counts"] = strategy_counts
    status_counts = dict(totals.get("conversion_status_counts") or {})
    conversion_status = str(document.get("conversion_status") or "unknown")
    status_counts[conversion_status] = int(status_counts.get(conversion_status) or 0) + 1
    totals["conversion_status_counts"] = status_counts
    modality_counts = dict(totals.get("modality_counts") or {})
    modality = str(document.get("modality") or "document")
    modality_counts[modality] = int(modality_counts.get(modality) or 0) + 1
    totals["modality_counts"] = modality_counts
    opportunities = int(totals.get("boundary_overlap_opportunity_count") or 0)
    totals["boundary_overlap_coverage_rate"] = (
        round(int(totals.get("boundary_overlap_count") or 0) / opportunities, 6)
        if opportunities
        else None
    )
    return updated


class DriveIndexer:
    def __init__(self, graph_extractor_factory=None):
        self.parser = None
        self.image_parser = None
        self.graph_extractor = None
        self.graph_extractor_factory = graph_extractor_factory
        self.kb = None
        self.workspace = None
        self.project_catalog = None
        self._index_source_names = {}

    def _calculate_file_hash(self, file_path: str) -> str:
        """璁＄畻鏂囦欢鐨?MD5 鍝堝笇鍊硷紝鐢ㄤ簬绮剧‘姣斿鏂囦欢鍐呭鏄惁鍙戠敓鐗╃悊鏀瑰彉"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            # 鍒嗗潡璇诲彇锛岄槻姝㈣秴澶?PDF 鎾戠垎鍐呭瓨
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    @staticmethod
    def discover_pdf_files(drive_path: str):
        workspace = SoulDriveWorkspace.from_drive(drive_path).ensure()
        return DriveIndexer.discover_workspace_document_files(workspace)

    @staticmethod
    def discover_workspace_pdf_files(workspace: SoulDriveWorkspace):
        return DriveIndexer.discover_workspace_document_files(workspace)

    @staticmethod
    def discover_workspace_document_files(workspace: SoulDriveWorkspace):
        # Local workspace root is already resolved; do not append SoulDrive again.
        return sorted(
            str(path)
            for path in Path(workspace.papers_path).rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
        )

    def _discover_index_document_files(self):
        configured_source = os.environ.get("SOULDRIVE_INDEX_SOURCE_DIR", "").strip()
        if configured_source:
            source_path = Path(configured_source).expanduser().resolve()
            if not source_path.is_dir():
                raise FileNotFoundError(f"index source directory does not exist: {source_path}")
            return self.discover_workspace_document_files_from_path(source_path), None
        project_files = self._discover_project_document_files()
        workspace_keys = get_workspace_keys(self.workspace.root_path)
        if workspace_keys is None:
            return sorted(set(self.discover_workspace_document_files(self.workspace) + project_files)), None

        temp_dir = Path(self.workspace.runtime_path) / f"secure-index-{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        store = SecureDocumentStore(self.workspace, workspace_keys)
        # Plain workspace documents remain the canonical import path.  A
        # keystore may still exist from an older build, but an empty/legacy
        # secure store must never hide documents in data/papers.
        document_files = list(self.discover_workspace_document_files(self.workspace))
        document_files.extend(project_files)
        try:
            for document in store.iter_documents():
                object_id = document["object_id"]
                suffix = Path(document["name"]).suffix.lower()
                if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
                    continue
                temp_name = f"{object_id}{suffix}"
                temp_path = temp_dir / temp_name
                temp_path.write_bytes(store.read_document_bytes(object_id))
                path_text = str(temp_path)
                document_files.append(path_text)
                self._index_source_names[path_text] = safe_document_filename(document["name"])
        finally:
            store.close()
        return document_files, temp_dir

    def _discover_project_document_files(self) -> list[str]:
        """Discover only user files in registered project folders, excluding derived data."""
        if self.project_catalog is None:
            return []
        rows = self.project_catalog.conn.execute(
            "SELECT project_id, root_path FROM projects WHERE status = 'active' AND root_path IS NOT NULL"
        ).fetchall()
        excluded = {".git", ".souldrive", "index", "runtime", "audit", "models", "skills", "eval", "sealed-test"}
        files: list[str] = []
        for row in rows:
            root = Path(str(row["root_path"])).expanduser().resolve()
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
                    continue
                try:
                    relative_parts = path.resolve().relative_to(root).parts
                except ValueError:
                    continue
                if any(part.lower() in excluded for part in relative_parts):
                    continue
                files.append(str(path))
        return sorted(set(files))

    @staticmethod
    def discover_workspace_document_files_from_path(source_path: Path):
        return sorted(
            str(path)
            for path in source_path.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
        )

    def _cleanup_secure_index_temp_dir(self, temp_dir):
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_prefix = str(temp_dir)
            self._index_source_names = {
                path: name
                for path, name in self._index_source_names.items()
                if not path.startswith(temp_prefix)
            }

    def _source_display_name(self, pdf_path: str):
        if pdf_path in self._index_source_names:
            return self._index_source_names[pdf_path]
        project = self._project_for_path(pdf_path)
        if project:
            project_id, root = project
            return f"{project_id}/{Path(pdf_path).resolve().relative_to(root).as_posix()}"
        return os.path.basename(pdf_path)

    def _project_for_path(self, pdf_path: str):
        if self.project_catalog is None:
            return None
        resolved = Path(pdf_path).resolve()
        rows = self.project_catalog.conn.execute(
            "SELECT project_id, root_path FROM projects WHERE status = 'active' AND root_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            root = Path(str(row["root_path"])).expanduser().resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return str(row["project_id"]), root
        return None

    def _catalog_ref_for_document(self, pdf_path: str, doc_id: str) -> str:
        project = self._project_for_path(pdf_path)
        if project:
            project_id, root = project
            return f"{project_id}/{Path(pdf_path).resolve().relative_to(root).as_posix()}"
        try:
            return Path(pdf_path).resolve().relative_to(
                Path(self.workspace.papers_path).resolve()
            ).as_posix()
        except ValueError:
            return doc_id

    def _catalog_item_for_document(self, pdf_path: str, doc_id: str, file_hash: str | None = None):
        if self.project_catalog is None:
            return None
        catalog_item = self.project_catalog.find_asset_version_for_file(
            self._catalog_ref_for_document(pdf_path, doc_id),
            content_hash=file_hash,
        )
        if catalog_item is not None or not file_hash:
            return catalog_item
        # Existing import paths use MD5 while catalog-first registration uses
        # SHA-256.  Try the canonical file digest before falling back to an
        # unbound document; never bind a path to an unverifiable version.
        digest_path = Path(pdf_path)
        if not digest_path.is_file() and self.workspace is not None:
            digest_path = Path(self.workspace.papers_path) / str(pdf_path)
        if not digest_path.is_file():
            return None
        return self.project_catalog.find_asset_version_for_file(
            self._catalog_ref_for_document(pdf_path, doc_id),
            content_hash=file_sha256(digest_path),
        )

    @staticmethod
    def _index_document_id(file_hash: str, source_filename: str) -> str:
        source_key = hashlib.sha1(source_filename.encode("utf-8")).hexdigest()[:12]
        return f"{file_hash}_{source_key}_schema_{INDEX_METADATA_SCHEMA_VERSION}"

    @staticmethod
    def _stable_document_id(pdf_path: str, catalog_item: dict | None = None) -> str:
        """Return an identity that survives content-version changes."""
        if catalog_item and catalog_item.get("asset_id"):
            return str(catalog_item["asset_id"])
        normalized = str(Path(pdf_path).resolve()).replace("\\", "/").lower()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
        return f"doc_{digest}"

    def sync_drive(self, drive_path: str, auth_level: str = "PRO"):
        workspace = SoulDriveWorkspace.from_drive(drive_path).ensure()
        return self.sync_workspace(workspace, auth_level)

    def sync_workspace(self, workspace: SoulDriveWorkspace, auth_level: str = "PRO"):
        """鍩轰簬 MD5 鏍￠獙鐨勪弗鏍煎閲忓悜閲忓寲绠￠亾"""
        from core.knowledge_base import LocalKnowledgeBase

        self.workspace = workspace.ensure()
        self.project_catalog = ProjectCatalog(self.workspace)
        run_id = str(uuid.uuid4())
        started_at = time.time()
        observability = _empty_indexing_observability()
        disk_report = self.workspace.disk_diagnostics()
        if not disk_report.get("ready"):
            failure = indexing_failure("workspace", "insufficient disk space")
            runtime_metrics.record_indexing_run(_elapsed_ms(started_at), 0, failed=True)
            self.project_catalog.close()
            self.project_catalog = None
            update_indexing_status(
                status="blocked",
                run_id=run_id,
                total_files=0,
                discovered_files=0,
                skipped_files=0,
                succeeded_files=0,
                processed_files=0,
                current_file=None,
                failures=[failure],
                failure_summary=_failure_summary([failure]),
                chunk_count=0,
                started_at=started_at,
                finished_at=time.time(),
                disk=disk_report,
                observability=observability,
            )
            logger.warning("[Indexer] Workspace disk space is insufficient; indexing is paused.")
            return

        self.kb = LocalKnowledgeBase(
            db_path=self.workspace.chroma_path,
            parent_doc_path=self.workspace.parent_doc_path,
            keyword_index_path=self.workspace.keyword_index_path,
            workspace_path=self.workspace.root_path,
        )
        pdf_files, secure_index_temp_dir = self._discover_index_document_files()
        update_indexing_status(
            status="scanning",
            run_id=run_id,
            total_files=len(pdf_files),
            discovered_files=len(pdf_files),
            skipped_files=0,
            succeeded_files=0,
            processed_files=0,
            current_file=None,
            failures=[],
            failure_summary={},
            chunk_count=0,
            started_at=started_at,
            finished_at=None,
            disk=disk_report,
            observability=observability,
        )

        files_to_process = []
        discovered_documents = []
        skipped_files = 0
        for pdf_path in pdf_files:
            # 1. 璁＄畻褰撳墠纭洏涓婃枃浠剁殑鐪熷疄 MD5
            file_hash = self._calculate_file_hash(pdf_path)
            doc_id = self._source_display_name(pdf_path)

            # 2. 浣跨敤鍝堝笇鍊间綔涓烘煡璇㈠嚟璇侊紝鍚?ChromaDB 鎺㈡煡
            index_status = self._document_index_status(file_hash, doc_id, pdf_path)
            discovered_documents.append((pdf_path, doc_id, file_hash))
            if index_status != "current":
                should_extract_graph = True
                files_to_process.append((pdf_path, doc_id, file_hash, should_extract_graph))
            else:
                skipped_files += 1
                logger.info("[Indexer] %s is unchanged (hash: %s); skipping parse.", doc_id, file_hash[:8])

        graph_backfill_sources = self._graph_backfill_sources(
            [doc_id for _, doc_id, _ in discovered_documents],
            auth_level,
        )
        if graph_backfill_sources:
            queued_sources = {doc_id for _, doc_id, _, _ in files_to_process}
            for pdf_path, doc_id, file_hash in discovered_documents:
                if doc_id not in graph_backfill_sources or doc_id in queued_sources:
                    continue
                files_to_process.append((pdf_path, doc_id, file_hash, True))
                skipped_files = max(0, skipped_files - 1)
            logger.info(
                "[Indexer] GraphRAG coverage is missing for %s document(s); queued graph backfill.",
                len(graph_backfill_sources),
            )

        if not files_to_process:
            logger.info("[Indexer] No new or changed documents found.")
            observability["graphrag"] = self._build_graphrag_index(auth_level)
            update_indexing_status(
                status="idle",
                run_id=run_id,
                total_files=0,
                discovered_files=len(pdf_files),
                skipped_files=skipped_files,
                succeeded_files=0,
                processed_files=0,
                current_file=None,
                failures=[],
                failure_summary={},
                chunk_count=0,
                started_at=started_at,
                finished_at=time.time(),
                disk=disk_report,
                observability=observability,
            )
            runtime_metrics.record_indexing_run(_elapsed_ms(started_at), 0, failed=False)
            self._cleanup_secure_index_temp_dir(secure_index_temp_dir)
            if self.kb is not None:
                self.kb.close()
                self.kb = None
            self.project_catalog.close()
            self.project_catalog = None
            return

        logger.info("[Indexer] Found %s new or changed documents; starting parse.", len(files_to_process))
        update_indexing_status(
            status="indexing",
            run_id=run_id,
            total_files=len(files_to_process),
            discovered_files=len(pdf_files),
            skipped_files=skipped_files,
            succeeded_files=0,
            processed_files=0,
            current_file=None,
            failures=[],
            failure_summary={},
            chunk_count=0,
            started_at=started_at,
            finished_at=None,
            disk=disk_report,
            observability=observability,
        )

        failures = []
        succeeded_files = 0
        chunk_count = 0
        for processed_index, (pdf_path, doc_id, file_hash, should_extract_graph) in enumerate(files_to_process, start=1):
            update_indexing_status(
                status="indexing",
                total_files=len(files_to_process),
                discovered_files=len(pdf_files),
                skipped_files=skipped_files,
                succeeded_files=succeeded_files,
                processed_files=processed_index - 1,
                current_file=doc_id,
                chunk_count=chunk_count,
                observability=observability,
            )
            document_started_at = time.time()
            parse_ms = 0
            ingest_ms = 0
            graph_ms = 0
            document_chunk_count = 0
            document_chunk_chars = 0
            ingest_summary = {}
            status = "failed"
            active_parser = None
            try:
                parse_started_at = time.time()
                active_parser = self._parser_for_path(pdf_path)
                chunks = active_parser.parse_and_chunk(pdf_path)
                parse_ms = _elapsed_ms(parse_started_at)
                document_chunk_count = len(chunks)
                document_chunk_chars = sum(len(chunk.page_content or "") for chunk in chunks)
                if not chunks:
                    logger.warning("[Indexer] %s produced no parseable chunks; skipping indexing.", doc_id)
                    self._delete_previous_chunks(
                        doc_id,
                        pdf_path,
                        preserve_ids=set(),
                        file_hash=file_hash,
                    )
                    parser_error = getattr(active_parser, "last_error", None)
                    failure = dict(parser_error) if parser_error else indexing_failure(doc_id, "no parseable chunks")
                    failure["source_filename"] = doc_id
                    failures.append(failure)
                    update_indexing_status(
                        failures=failures,
                        failure_summary=_failure_summary(failures),
                        observability=observability,
                    )
                    continue

                navigation_summary = _document_navigation_summary(doc_id, chunks)
                catalog_item = self._catalog_item_for_document(pdf_path, doc_id, file_hash)
                project = None if catalog_item else self._project_for_path(pdf_path)

                # Keep source provenance on every parent before child indexing.
                for chunk_index, chunk in enumerate(chunks):
                    chunk.metadata["source_filename"] = doc_id
                    chunk.metadata["source_path"] = os.path.abspath(pdf_path)
                    chunk.metadata["document_hash"] = file_hash
                    chunk.metadata["chunk_index"] = chunk_index
                    chunk.metadata["document_summary"] = navigation_summary
                    chunk.metadata["document_summary_method"] = "extractive_pre_index_v1"
                    chunk.metadata["indexed_at"] = time.time()
                    chunk.metadata["metadata_schema_version"] = INDEX_METADATA_SCHEMA_VERSION
                    chunk.metadata["document_id"] = self._stable_document_id(pdf_path, catalog_item)
                    if catalog_item:
                        catalog_title = str(catalog_item.get("title") or "").strip()
                        if catalog_title:
                            chunk.metadata["catalog_title"] = catalog_title
                            chunk.metadata["document_title"] = catalog_title
                        chunk.metadata.update({
                            "project_id": catalog_item["project_id"],
                            "asset_id": catalog_item["asset_id"],
                            "asset_type": catalog_item["asset_type"],
                            "asset_version_id": catalog_item["version_id"],
                            "valid_from": catalog_item["valid_from"],
                            "authored_at": catalog_item["authored_at"],
                            "imported_at": catalog_item["imported_at"],
                            "catalog_file_ref": catalog_item["file_ref"],
                        })
                    else:
                        if project:
                            chunk.metadata["project_id"] = project[0]
                            chunk.metadata["catalog_file_ref"] = self._catalog_ref_for_document(pdf_path, doc_id)
                    chunk.metadata["embedding_model_name"] = str(
                        getattr(self.kb, "embedding_model_name", "") or "unknown"
                    )

                ingest_started_at = time.time()
                index_document_id = self._index_document_id(file_hash, doc_id)
                ingest_summary = self.kb.ingest_chunks(index_document_id, chunks) or {}
                ingest_ms = _elapsed_ms(ingest_started_at)
                current_child_ids = {
                    child_id
                    for chunk in chunks
                    for child_id in chunk.metadata.get("child_ids", [])
                }
                self._delete_previous_chunks(
                    doc_id,
                    pdf_path,
                    preserve_ids=current_child_ids,
                    file_hash=file_hash,
                )
                succeeded_files += 1
                chunk_count += len(chunks)
                if should_extract_graph:
                    graph_started_at = time.time()
                    graph_summary = self._extract_graph_chunks(chunks, doc_id, auth_level)
                    graph_ms = _elapsed_ms(graph_started_at)
                else:
                    graph_summary = None
                    logger.info("[Indexer] %s content hash is unchanged; refreshing metadata only.", doc_id)
                status = "indexed"
                if graph_summary:
                    logger.info(
                        "[Indexer] %s indexed with graph summary: %s entities, %s relationships, hash: %s.",
                        doc_id,
                        graph_summary["entities"],
                        graph_summary["relationships"],
                        file_hash[:8],
                    )
                else:
                    logger.info("[Indexer] %s indexed without graph summary, hash: %s.", doc_id, file_hash[:8])
            except Exception as e:
                failure = indexing_failure(doc_id, str(e))
                failures.append(failure)
                update_indexing_status(
                    failures=failures,
                    failure_summary=_failure_summary(failures),
                    observability=observability,
                )
                logger.exception("[Indexer] Fatal error while parsing %s: %s", doc_id, e)
            finally:
                parse_stats = dict(getattr(active_parser, "last_parse_stats", {}) or {})
                observability = _record_document_observation(observability, {
                    "source_filename": doc_id,
                    "status": status,
                    "chunk_count": document_chunk_count,
                    "chunk_chars": document_chunk_chars,
                    "parse_ms": parse_ms,
                    "ingest_ms": ingest_ms,
                    "graph_ms": graph_ms,
                    "elapsed_ms": _elapsed_ms(document_started_at),
                    "graph_attempted": bool(should_extract_graph),
                     "child_count": int(ingest_summary.get("child_count") or 0),
                     "section_tree_node_count": int(
                         ingest_summary.get("section_tree_node_count") or 0
                     ),
                     "section_tree_leaf_count": int(
                         ingest_summary.get("section_tree_leaf_count") or 0
                     ),
                     "embedding_ms": int(ingest_summary.get("embedding_ms") or 0),
                    "vector_store_ms": int(ingest_summary.get("vector_store_ms") or 0),
                    "keyword_index_ms": int(ingest_summary.get("keyword_index_ms") or 0),
                    "parent_store_ms": int(ingest_summary.get("parent_store_ms") or 0),
                    "child_split_ms": int(ingest_summary.get("child_split_ms") or 0),
                    "raw_items": int(parse_stats.get("raw_items") or 0),
                    "kept_items": int(parse_stats.get("kept_items") or 0),
                    "removed_page_artifacts": int(parse_stats.get("removed_page_artifacts") or 0),
                    "repeated_page_artifacts": int(parse_stats.get("repeated_page_artifacts") or 0),
                    "removed_non_content": int(parse_stats.get("removed_non_content") or 0),
                    "removed_duplicates": int(parse_stats.get("removed_duplicates") or 0),
                    "table_items": int(parse_stats.get("table_items") or 0),
                    "image_items": int(parse_stats.get("image_items") or 0),
                    "ocr_ms": int(parse_stats.get("ocr_ms") or 0),
                    "ocr_region_count": int(parse_stats.get("ocr_region_count") or 0),
                    "ocr_mean_confidence": float(parse_stats.get("ocr_mean_confidence") or 0.0),
                    "modality": str(parse_stats.get("modality") or "document"),
                    "pages": int(parse_stats.get("pages") or 0),
                    "parent_chunks": int(parse_stats.get("parent_chunks") or 0),
                    "heading_only_merges": int(parse_stats.get("heading_only_merges") or 0),
                    "short_same_section_merges": int(parse_stats.get("short_same_section_merges") or 0),
                    "conversion_status": str(
                        parse_stats.get("conversion_status") or "unknown"
                    ),
                    "conversion_error_count": int(
                        parse_stats.get("conversion_error_count") or 0
                    ),
                    "converted_pages": int(
                        parse_stats.get("converted_pages") or 0
                    ),
                    "failed_pages": list(parse_stats.get("failed_pages") or []),
                    "failed_page_count": len(parse_stats.get("failed_pages") or []),
                    "formal_window_count": int(ingest_summary.get("formal_window_count") or 0),
                    "boundary_overlap_opportunity_count": int(
                        ingest_summary.get("boundary_overlap_opportunity_count") or 0
                    ),
                    "boundary_overlap_count": int(
                        ingest_summary.get("boundary_overlap_count") or 0
                    ),
                    "boundary_overlap_truncated_count": int(
                        ingest_summary.get("boundary_overlap_truncated_count") or 0
                    ),
                    "boundary_overlap_token_skip_count": int(
                        ingest_summary.get("boundary_overlap_token_skip_count") or 0
                    ),
                    "max_child_tokens": int(ingest_summary.get("max_child_tokens") or 0),
                    "max_embedding_input_tokens": int(
                        ingest_summary.get("max_embedding_input_tokens") or 0
                    ),
                    "embedding_token_limit": int(ingest_summary.get("embedding_token_limit") or 0),
                    "child_strategy_counts": dict(
                        ingest_summary.get("child_strategy_counts") or {}
                    ),
                })
                update_indexing_status(
                    status="indexing",
                    total_files=len(files_to_process),
                    discovered_files=len(pdf_files),
                    skipped_files=skipped_files,
                    succeeded_files=succeeded_files,
                    processed_files=processed_index,
                    current_file=None,
                    failures=failures,
                    failure_summary=_failure_summary(failures),
                    chunk_count=chunk_count,
                    observability=observability,
                )

        observability["graphrag"] = self._build_graphrag_index(auth_level)
        self._teardown_parser()
        self._cleanup_secure_index_temp_dir(secure_index_temp_dir)
        update_indexing_status(
            status="completed",
            total_files=len(files_to_process),
            discovered_files=len(pdf_files),
            skipped_files=skipped_files,
            succeeded_files=succeeded_files,
            processed_files=len(files_to_process),
            current_file=None,
            failures=failures,
            failure_summary=_failure_summary(failures),
            chunk_count=chunk_count,
            finished_at=time.time(),
            disk=self.workspace.disk_diagnostics(),
            observability=observability,
        )
        runtime_metrics.record_indexing_run(_elapsed_ms(started_at), chunk_count, failed=bool(failures))
        self.project_catalog.close()
        self.project_catalog = None

    def _delete_previous_chunks(
        self,
        doc_id: str,
        pdf_path: str,
        preserve_ids: set[str] | None = None,
        file_hash: str | None = None,
    ):
        source_path = os.path.abspath(pdf_path)
        preserve_ids = preserve_ids or set()
        previous_records: dict[str, dict] = {}

        for where_filter in ({"source_path": source_path}, {"source_filename": doc_id}):
            try:
                existing = self.kb.collection.get(
                    where=where_filter,
                    include=["metadatas"],
                )
                ids = list(existing.get("ids") or [])
                metadatas = list(existing.get("metadatas") or [])
                for index, child_id in enumerate(ids):
                    previous_records[str(child_id)] = (
                        dict(metadatas[index] or {})
                        if index < len(metadatas)
                        else {}
                    )
            except Exception as exc:
                logger.warning(
                    "[Indexer] Failed to inspect stale chunks for %s: %s",
                    doc_id,
                    exc,
                )

        if file_hash:
            hash_prefix = f"{file_hash}_"
            try:
                existing = self.kb.collection.get(include=["metadatas"])
                ids = list(existing.get("ids") or [])
                metadatas = list(existing.get("metadatas") or [])
                for index, child_id in enumerate(ids):
                    child_id = str(child_id)
                    if child_id.startswith(hash_prefix) and "_parent_" in child_id:
                        metadata = (
                            dict(metadatas[index] or {})
                            if index < len(metadatas)
                            else {}
                        )
                        if metadata.get("source_filename") not in {None, "", doc_id}:
                            continue
                        previous_records[child_id] = metadata
            except Exception as exc:
                logger.warning(
                    "[Indexer] Failed to inspect legacy chunks for %s: %s",
                    doc_id,
                    exc,
                )

        stale_ids = [
            child_id
            for child_id in previous_records
            if child_id not in preserve_ids
        ]
        if not stale_ids:
            return
        stale_parent_ids = sorted({
            str(previous_records[child_id].get("parent_id") or "")
            for child_id in stale_ids
            if previous_records[child_id].get("parent_id")
        })
        self.kb.delete_index_records(
            child_ids=stale_ids,
            parent_ids=stale_parent_ids,
        )
        logger.info("[Indexer] Removed %s stale chunks for %s.", len(stale_ids), doc_id)

    def _document_index_is_current(
        self,
        file_hash: str,
        source_filename: str | None = None,
        file_path: str | None = None,
    ) -> bool:
        return self._document_index_status(file_hash, source_filename, file_path) == "current"

    def _document_index_status(
        self,
        file_hash: str,
        source_filename: str | None = None,
        file_path: str | None = None,
    ) -> str:
        try:
            existing = self.kb.collection.get(where={"document_hash": file_hash}, include=["metadatas"])
        except Exception:
            return "missing"

        if not existing.get("ids"):
            return "missing"

        metadatas = existing.get("metadatas") or []
        if source_filename is not None:
            metadatas = [
                metadata
                for metadata in metadatas
                if str((metadata or {}).get("source_filename") or "") == source_filename
            ]
            if not metadatas:
                return "missing"
        if not metadatas:
            return "metadata_upgrade"

        for metadata in metadatas:
            if not metadata:
                return "metadata_upgrade"
            if int(metadata.get("metadata_schema_version") or 0) < INDEX_METADATA_SCHEMA_VERSION:
                return "metadata_upgrade"
            expected_embedding_model = str(
                getattr(self.kb, "embedding_model_name", "") or ""
            )
            if (
                expected_embedding_model
                and str(metadata.get("embedding_model_name") or "")
                != expected_embedding_model
            ):
                return "embedding_model_upgrade"
            if metadata.get("page") is None and metadata.get("page_number") is None:
                return "metadata_upgrade"
        if self.project_catalog is not None:
            catalog_item = self._catalog_item_for_document(
                file_path or self._index_source_names.get(source_filename, source_filename),
                source_filename,
                file_hash,
            )
            if catalog_item:
                expected = {
                    "project_id": str(catalog_item["project_id"]),
                    "asset_id": str(catalog_item["asset_id"]),
                    "asset_version_id": str(catalog_item["version_id"]),
                }
                if any(str((metadata or {}).get(key) or "") != value for key, value in expected.items()):
                    return "metadata_upgrade"
            else:
                project = self._project_for_path(
                    file_path or self._index_source_names.get(source_filename, source_filename)
                )
                expected_project_id = str(project[0]) if project else ""
                catalog_fields = (
                    "asset_id",
                    "asset_version_id",
                    "asset_type",
                    "catalog_file_ref",
                    "catalog_title",
                    "valid_from",
                    "authored_at",
                    "imported_at",
                )
                for metadata in metadatas:
                    if str((metadata or {}).get("project_id") or "") != expected_project_id:
                        return "metadata_upgrade"
                    if any(str((metadata or {}).get(key) or "").strip() for key in catalog_fields):
                        return "metadata_upgrade"
        return "current"

    def _parser_for_path(self, document_path: str):
        if is_image_document(document_path):
            if self.image_parser is None:
                from core.image_ocr import LocalImageOcrParser

                self.image_parser = LocalImageOcrParser()
            return self.image_parser
        if Path(document_path).suffix.lower() != ".pdf":
            if not hasattr(self, "plain_parser") or self.plain_parser is None:
                from core.plain_document_parser import PlainDocumentParser

                self.plain_parser = PlainDocumentParser()
            return self.plain_parser
        if self.parser is None:
            from core.document_parser import AdvancedDocumentParser

            self.parser = AdvancedDocumentParser()
        return self.parser

    def _get_graph_extractor(self):
        if self.graph_extractor is None:
            if self.graph_extractor_factory:
                self.graph_extractor = self.graph_extractor_factory()
            else:
                from core.graph_extractor import GraphExtractor
                self.graph_extractor = GraphExtractor(
                    graph_db_path=self.workspace.graph_db_path,
                    workspace_path=self.workspace.root_path,
                )
        return self.graph_extractor

    def _graph_backfill_sources(self, source_filenames: list[str], auth_level: str) -> set[str]:
        if not source_filenames or not _graph_features_enabled(auth_level):
            return set()

        graph_store = None
        try:
            from core.graph_db import LocalGraphDB
            from core.secure_graph_store import SecureGraphStore

            workspace_keys = get_workspace_keys(self.workspace.root_path)
            if workspace_keys is not None:
                graph_store = SecureGraphStore(self.workspace.secure_graph_store_path, workspace_keys)
            else:
                graph_store = LocalGraphDB(self.workspace.graph_db_path)
            return _graph_sources_missing_coverage(
                graph_store.get_graph_snapshot(),
                source_filenames,
            )
        except Exception as exc:
            logger.warning("[Indexer] Could not inspect GraphRAG coverage: %s", exc)
            return set()
        finally:
            if graph_store is not None:
                close_fn = getattr(graph_store, "close", None)
                if callable(close_fn):
                    close_fn()

    def _extract_graph_chunks(self, chunks: list, source_filename: str, auth_level: str):
        if not chunks:
            return None

        if not _graph_features_enabled(auth_level):
            logger.info("[Indexer] Auth level is %s; skipping graph extraction.", auth_level)
            return None

        try:
            extractor = self._get_graph_extractor()
            return extractor.extract_chunks(chunks, source_filename)
        except Exception as e:
            logger.warning("[Indexer] Graph extraction degraded for %s: %s", source_filename, e)
            return None

    def _build_graphrag_index(self, auth_level: str) -> dict:
        if not _graph_features_enabled(auth_level):
            return {
                "status": "skipped",
                "reason": "auth_level_not_graph_enabled",
                "algorithm": "leiden_v1_local_move_refine",
            }

        graph_store = None
        close_store = False
        try:
            extractor = self.graph_extractor
            if extractor is not None and getattr(extractor, "db", None) is not None:
                graph_store = extractor.db
                report_generator = getattr(extractor, "generate_community_report", None)
            else:
                from core.graph_db import LocalGraphDB
                from core.secure_graph_store import SecureGraphStore

                workspace_keys = get_workspace_keys(self.workspace.root_path)
                if workspace_keys is not None:
                    graph_store = SecureGraphStore(self.workspace.secure_graph_store_path, workspace_keys)
                else:
                    graph_store = LocalGraphDB(self.workspace.graph_db_path)
                report_generator = None
                close_store = True

            from core.graph_rag import GraphRAGBuilder

            started_at = time.time()
            builder = GraphRAGBuilder(
                graph_store,
                report_generator=report_generator,
                min_communities=3,
                max_communities=4,
                max_documents=10,
            )
            payload = builder.build()
            project_indexes = []
            shared_global_payload = {}
            try:
                snapshot = graph_store.get_graph_snapshot()
                chunk_ids = [
                    str(chunk_id)
                    for document in snapshot.get("documents") or []
                    for chunk_id in document.get("chunk_ids") or []
                    if str(chunk_id)
                ]
                chunk_project_ids = (
                    self.kb.metadata_for_chunk_ids(chunk_ids)
                    if self.kb is not None and hasattr(self.kb, "metadata_for_chunk_ids")
                    else {}
                )
                try:
                    shared_global_payload = GraphRAGBuilder(
                        graph_store,
                        report_generator=report_generator,
                        min_communities=3,
                        max_communities=4,
                        max_documents=10,
                        global_only=True,
                        index_name="global_shared",
                        chunk_project_ids=chunk_project_ids,
                    ).build()
                except Exception as exc:
                    shared_global_payload = {
                        "status": "failed",
                        "error": str(exc)[:240],
                    }
                project_ids = sorted({
                    str(metadata.get("project_id") or "").strip()
                    for metadata in chunk_project_ids.values()
                    if str(metadata.get("project_id") or "").strip()
                })
                for project_id in project_ids:
                    try:
                        project_payload = GraphRAGBuilder(
                            graph_store,
                            report_generator=report_generator,
                            min_communities=3,
                            max_communities=4,
                            max_documents=10,
                            project_id=project_id,
                            chunk_project_ids={
                                chunk_id: str(metadata.get("project_id") or "")
                                for chunk_id, metadata in chunk_project_ids.items()
                            },
                        ).build()
                        project_indexes.append({
                            "project_id": project_id,
                            "status": "completed",
                            "document_count": project_payload.get("document_count", 0),
                            "community_count": project_payload.get("community_count", 0),
                            "target_range_ok": project_payload.get("target_range_ok", False),
                        })
                    except Exception as exc:
                        project_indexes.append({
                            "project_id": project_id,
                            "status": "failed",
                            "error": str(exc)[:240],
                        })
            except Exception as exc:
                project_indexes.append({"status": "failed", "error": str(exc)[:240]})
            return {
                "status": "completed",
                "algorithm": payload.get("algorithm"),
                "elapsed_ms": _elapsed_ms(started_at),
                "document_count": payload.get("document_count", 0),
                "entity_count": payload.get("entity_count", 0),
                "relationship_count": payload.get("relationship_count", 0),
                "community_count": payload.get("community_count", 0),
                "target_range_ok": payload.get("target_range_ok", False),
                "project_indexes": project_indexes,
                "shared_global": {
                    "status": shared_global_payload.get("status", "completed"),
                    "index_name": "global_shared",
                    "document_count": shared_global_payload.get("document_count", 0),
                    "community_count": shared_global_payload.get("community_count", 0),
                    "target_range_ok": shared_global_payload.get("target_range_ok", False),
                },
                "report_methods": sorted({
                    str(item.get("report_method") or "unknown")
                    for item in payload.get("communities") or []
                }),
            }
        except Exception as exc:
            logger.warning("[Indexer] GraphRAG community build degraded: %s", exc)
            return {
                "status": "failed",
                "algorithm": "leiden_v1_local_move_refine",
                "error": str(exc)[:240],
            }
        finally:
            if close_store and graph_store is not None:
                close_fn = getattr(graph_store, "close", None)
                if callable(close_fn):
                    close_fn()

    def _teardown_parser(self):
        """Release parser resources after indexing."""
        if self.parser is not None or self.image_parser is not None:
            logger.info("[System] Parser task finished; releasing resources.")
            if self.parser is not None:
                del self.parser
            self.parser = None
            if self.image_parser is not None:
                del self.image_parser
            self.image_parser = None
            if self.graph_extractor is not None:
                close_fn = getattr(self.graph_extractor, "close", None)
                if callable(close_fn):
                    close_fn()
                self.graph_extractor = None
            gc.collect()

    def close(self):
        self._teardown_parser()
        if self.graph_extractor is not None:
            close_fn = getattr(self.graph_extractor, "close", None)
            if callable(close_fn):
                close_fn()
            self.graph_extractor = None
        if self.kb is not None:
            close_fn = getattr(self.kb, "close", None)
            if callable(close_fn):
                close_fn()
            self.kb = None
        if self.project_catalog is not None:
            self.project_catalog.close()
            self.project_catalog = None

default_indexer = DriveIndexer()
