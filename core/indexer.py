import os
import glob
import gc
import hashlib
import shutil
import time
import uuid
from pathlib import Path
from core.indexing_errors import indexing_failure
from core.logging_config import get_logger
from core.observability import runtime_metrics
from core.runtime_state import update_indexing_status
from core.secure_document_store import SecureDocumentStore
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.paper_importer import safe_pdf_filename

logger = get_logger(__name__)


INDEX_METADATA_SCHEMA_VERSION = 7


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
            "removed_non_content": 0,
            "removed_duplicates": 0,
            "table_items": 0,
            "image_items": 0,
            "pages": 0,
            "parent_chunks": 0,
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
    for key in (
        "parse_ms",
        "ingest_ms",
        "graph_ms",
        "elapsed_ms",
        "chunk_chars",
        "child_count",
        "embedding_ms",
        "vector_store_ms",
        "keyword_index_ms",
        "parent_store_ms",
        "child_split_ms",
        "raw_items",
        "kept_items",
        "removed_page_artifacts",
        "removed_non_content",
        "removed_duplicates",
        "table_items",
        "image_items",
        "pages",
        "parent_chunks",
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
        self.graph_extractor = None
        self.graph_extractor_factory = graph_extractor_factory
        self.kb = None
        self.workspace = None
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
        return DriveIndexer.discover_workspace_pdf_files(workspace)

    @staticmethod
    def discover_workspace_pdf_files(workspace: SoulDriveWorkspace):
        # Local workspace root is already resolved; do not append SoulDrive again.
        return glob.glob(os.path.join(workspace.papers_path, "**/*.pdf"), recursive=True)

    def _discover_index_pdf_files(self):
        workspace_keys = get_workspace_keys(self.workspace.root_path)
        if workspace_keys is None:
            return self.discover_workspace_pdf_files(self.workspace), None

        temp_dir = Path(self.workspace.runtime_path) / f"secure-index-{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        store = SecureDocumentStore(self.workspace, workspace_keys)
        pdf_files = []
        try:
            for document in store.iter_documents():
                object_id = document["object_id"]
                temp_name = f"{object_id}.pdf"
                temp_path = temp_dir / temp_name
                temp_path.write_bytes(store.read_document_bytes(object_id))
                path_text = str(temp_path)
                pdf_files.append(path_text)
                self._index_source_names[path_text] = safe_pdf_filename(document["name"])
        finally:
            store.close()
        return pdf_files, temp_dir

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
        return self._index_source_names.get(pdf_path, os.path.basename(pdf_path))

    def sync_drive(self, drive_path: str, auth_level: str = "PRO"):
        workspace = SoulDriveWorkspace.from_drive(drive_path).ensure()
        return self.sync_workspace(workspace, auth_level)

    def sync_workspace(self, workspace: SoulDriveWorkspace, auth_level: str = "PRO"):
        """鍩轰簬 MD5 鏍￠獙鐨勪弗鏍煎閲忓悜閲忓寲绠￠亾"""
        from core.knowledge_base import LocalKnowledgeBase

        self.workspace = workspace.ensure()
        run_id = str(uuid.uuid4())
        started_at = time.time()
        observability = _empty_indexing_observability()
        disk_report = self.workspace.disk_diagnostics()
        if not disk_report.get("ready"):
            failure = indexing_failure("workspace", "insufficient disk space")
            runtime_metrics.record_indexing_run(_elapsed_ms(started_at), 0, failed=True)
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
        pdf_files, secure_index_temp_dir = self._discover_index_pdf_files()
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
        skipped_files = 0
        for pdf_path in pdf_files:
            # 1. 璁＄畻褰撳墠纭洏涓婃枃浠剁殑鐪熷疄 MD5
            file_hash = self._calculate_file_hash(pdf_path)
            doc_id = self._source_display_name(pdf_path)

            # 2. 浣跨敤鍝堝笇鍊间綔涓烘煡璇㈠嚟璇侊紝鍚?ChromaDB 鎺㈡煡
            index_status = self._document_index_status(file_hash)
            if index_status != "current":
                should_extract_graph = True
                files_to_process.append((pdf_path, doc_id, file_hash, should_extract_graph))
            else:
                skipped_files += 1
                logger.info("[Indexer] %s is unchanged (hash: %s); skipping parse.", doc_id, file_hash[:8])

        if not files_to_process:
            logger.info("[Indexer] No new or changed documents found.")
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

        if self.parser is None:
            from core.document_parser import AdvancedDocumentParser

            self.parser = AdvancedDocumentParser()

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
            try:
                parse_started_at = time.time()
                chunks = self.parser.parse_and_chunk(pdf_path)
                parse_ms = _elapsed_ms(parse_started_at)
                document_chunk_count = len(chunks)
                document_chunk_chars = sum(len(chunk.page_content or "") for chunk in chunks)
                if not chunks:
                    logger.warning("[Indexer] %s produced no parseable chunks; skipping indexing.", doc_id)
                    parser_error = getattr(self.parser, "last_error", None)
                    failure = dict(parser_error) if parser_error else indexing_failure(doc_id, "no parseable chunks")
                    failure["source_filename"] = doc_id
                    failures.append(failure)
                    update_indexing_status(
                        failures=failures,
                        failure_summary=_failure_summary(failures),
                        observability=observability,
                    )
                    continue

                # Keep source provenance on every parent before child indexing.
                for chunk_index, chunk in enumerate(chunks):
                    chunk.metadata["source_filename"] = doc_id
                    chunk.metadata["source_path"] = os.path.abspath(pdf_path)
                    chunk.metadata["document_hash"] = file_hash
                    chunk.metadata["chunk_index"] = chunk_index
                    chunk.metadata["metadata_schema_version"] = INDEX_METADATA_SCHEMA_VERSION
                    chunk.metadata["embedding_model_name"] = str(
                        getattr(self.kb, "embedding_model_name", "") or "unknown"
                    )

                ingest_started_at = time.time()
                index_document_id = (
                    f"{file_hash}_schema_{INDEX_METADATA_SCHEMA_VERSION}"
                )
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
                parse_stats = dict(getattr(self.parser, "last_parse_stats", {}) or {})
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
                    "embedding_ms": int(ingest_summary.get("embedding_ms") or 0),
                    "vector_store_ms": int(ingest_summary.get("vector_store_ms") or 0),
                    "keyword_index_ms": int(ingest_summary.get("keyword_index_ms") or 0),
                    "parent_store_ms": int(ingest_summary.get("parent_store_ms") or 0),
                    "child_split_ms": int(ingest_summary.get("child_split_ms") or 0),
                    "raw_items": int(parse_stats.get("raw_items") or 0),
                    "kept_items": int(parse_stats.get("kept_items") or 0),
                    "removed_page_artifacts": int(parse_stats.get("removed_page_artifacts") or 0),
                    "removed_non_content": int(parse_stats.get("removed_non_content") or 0),
                    "removed_duplicates": int(parse_stats.get("removed_duplicates") or 0),
                    "table_items": int(parse_stats.get("table_items") or 0),
                    "image_items": int(parse_stats.get("image_items") or 0),
                    "pages": int(parse_stats.get("pages") or 0),
                    "parent_chunks": int(parse_stats.get("parent_chunks") or 0),
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
                        previous_records[child_id] = (
                            dict(metadatas[index] or {})
                            if index < len(metadatas)
                            else {}
                        )
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

    def _document_index_is_current(self, file_hash: str) -> bool:
        return self._document_index_status(file_hash) == "current"

    def _document_index_status(self, file_hash: str) -> str:
        try:
            existing = self.kb.collection.get(where={"document_hash": file_hash}, include=["metadatas"])
        except Exception:
            return "missing"

        if not existing.get("ids"):
            return "missing"

        metadatas = existing.get("metadatas") or []
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
        return "current"

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

    def _extract_graph_chunks(self, chunks: list, source_filename: str, auth_level: str):
        if not chunks:
            return None
        if auth_level != "PRO":
            logger.info("[Indexer] Auth level is %s; skipping graph extraction.", auth_level)
            return None

        try:
            extractor = self._get_graph_extractor()
            return extractor.extract_chunks(chunks, source_filename)
        except Exception as e:
            logger.warning("[Indexer] Graph extraction degraded for %s: %s", source_filename, e)
            return None

    def _teardown_parser(self):
        """Release parser resources after indexing."""
        if self.parser is not None:
            logger.info("[System] Parser task finished; releasing resources.")
            del self.parser
            self.parser = None
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

default_indexer = DriveIndexer()
