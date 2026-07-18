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
from core.runtime_state import update_indexing_status
from core.secure_document_store import SecureDocumentStore
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.paper_importer import safe_pdf_filename

logger = get_logger(__name__)


INDEX_METADATA_SCHEMA_VERSION = 3


def _failure_summary(failures: list[dict]):
    summary: dict[str, int] = {}
    for failure in failures:
        code = str(failure.get("error_code") or "INDEXING_FAILED")
        summary[code] = summary.get(code, 0) + 1
    return summary


class DriveIndexer:
    def __init__(self, graph_extractor_factory=None):
        self.parser = None
        self.graph_extractor = None
        self.graph_extractor_factory = graph_extractor_factory
        self.kb = None
        self.workspace = None
        self._index_source_names = {}

    def _calculate_file_hash(self, file_path: str) -> str:
        """计算文件的 MD5 哈希值，用于精确比对文件内容是否发生物理改变"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            # 分块读取，防止超大 PDF 撑爆内存
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
        """基于 MD5 校验的严格增量向量化管道"""
        from core.knowledge_base import LocalKnowledgeBase

        self.workspace = workspace.ensure()
        run_id = str(uuid.uuid4())
        started_at = time.time()
        disk_report = self.workspace.disk_diagnostics()
        if not disk_report.get("ready"):
            failure = indexing_failure("workspace", "insufficient disk space")
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
            )
            logger.warning("[Indexer] 工作区剩余空间不足，已暂停索引任务。")
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
        )

        files_to_process = []
        skipped_files = 0
        for pdf_path in pdf_files:
            # 1. 计算当前硬盘上文件的真实 MD5
            file_hash = self._calculate_file_hash(pdf_path)
            doc_id = self._source_display_name(pdf_path)

            # 2. 使用哈希值作为查询凭证，向 ChromaDB 探查
            index_status = self._document_index_status(file_hash)
            if index_status != "current":
                # 将哈希值一并存入待处理列表，避免后续重复计算
                should_extract_graph = index_status == "missing"
                files_to_process.append((pdf_path, doc_id, file_hash, should_extract_graph))
            else:
                skipped_files += 1
                logger.info("[Indexer] 状态一致: %s (Hash: %s...) 内容未变更，跳过解析。", doc_id, file_hash[:8])

        if not files_to_process:
            logger.info("[Indexer] U盘内无新增或变更文献，系统保持静默待机。")
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
            )
            self._cleanup_secure_index_temp_dir(secure_index_temp_dir)
            return

        logger.info("[Indexer] 发现 %s 篇新文献或变更文献，开始唤醒解析引擎...", len(files_to_process))
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
            )
            try:
                chunks = self.parser.parse_and_chunk(pdf_path)
                if not chunks:
                    logger.warning("[Indexer] %s 未解析出有效文本块，已跳过向量入库与图谱抽取。", doc_id)
                    parser_error = getattr(self.parser, "last_error", None)
                    failure = dict(parser_error) if parser_error else indexing_failure(doc_id, "no parseable chunks")
                    failure["source_filename"] = doc_id
                    failures.append(failure)
                    update_indexing_status(failures=failures, failure_summary=_failure_summary(failures))
                    continue

                # 关键：在此处将原本的 doc_id 替换为 file_hash 传给知识库
                # 为了保证后续检索时还能知道文件名，我们将文件名强行注入到 metadata 中
                for chunk_index, chunk in enumerate(chunks):
                    chunk.metadata["source_filename"] = doc_id
                    chunk.metadata["source_path"] = os.path.abspath(pdf_path)
                    chunk.metadata["document_hash"] = file_hash
                    chunk.metadata["chunk_index"] = chunk_index
                    chunk.metadata["metadata_schema_version"] = INDEX_METADATA_SCHEMA_VERSION

                self._delete_previous_chunks(doc_id, pdf_path)
                self.kb.ingest_chunks(file_hash, chunks)
                succeeded_files += 1
                chunk_count += len(chunks)
                if should_extract_graph:
                    graph_summary = self._extract_graph_chunks(chunks, doc_id, auth_level)
                else:
                    graph_summary = None
                    logger.info("[Indexer] %s 内容哈希未变，仅升级页码 metadata，跳过图谱重抽取。", doc_id)
                if graph_summary:
                    logger.info(
                        "[Indexer] %s 已完成向量入库与图谱抽取，新增/更新实体 %s 个，关系 %s 条，指纹: %s。",
                        doc_id,
                        graph_summary["entities"],
                        graph_summary["relationships"],
                        file_hash[:8],
                    )
                else:
                    logger.info("[Indexer] %s 已完成向量入库，图谱抽取未产生有效结果，指纹: %s。", doc_id, file_hash[:8])
            except Exception as e:
                failure = indexing_failure(doc_id, str(e))
                failures.append(failure)
                update_indexing_status(failures=failures, failure_summary=_failure_summary(failures))
                logger.exception("[Indexer] 致命错误: %s 解析失败，原因 - %s", doc_id, e)
            finally:
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
        )

    def _delete_previous_chunks(self, doc_id: str, pdf_path: str):
        source_path = os.path.abspath(pdf_path)
        deleted_total = 0

        for where_filter in ({"source_path": source_path}, {"source_filename": doc_id}):
            try:
                existing = self.kb.collection.get(where=where_filter, include=[])
                ids = existing.get("ids", [])
                if ids:
                    existing_with_meta = self.kb.collection.get(ids=ids, include=["metadatas"])
                    for metadata in existing_with_meta.get("metadatas", []) or []:
                        document_hash = metadata.get("document_hash") if metadata else None
                        if document_hash:
                            self.kb.delete_document_indexes(document_hash)
                    self.kb.collection.delete(ids=ids)
                    deleted_total += len(ids)
            except Exception:
                continue

        if deleted_total:
            logger.info("[Indexer] 已清理 %s 的旧向量块 %s 条，避免变更文件重复污染。", doc_id, deleted_total)

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
            if metadata.get("page") is None and metadata.get("page_number") is None:
                return "metadata_upgrade"
        return "current"

    def _get_graph_extractor(self):
        if self.graph_extractor is None:
            if self.graph_extractor_factory:
                self.graph_extractor = self.graph_extractor_factory()
            else:
                # 懒加载避免索引器初始化时立刻挂载第二个 GGUF 模型。
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
            logger.info("[Indexer] 当前授权级别为 %s，已跳过高级图谱抽取。", auth_level)
            return None

        try:
            extractor = self._get_graph_extractor()
            return extractor.extract_chunks(chunks, source_filename)
        except Exception as e:
            logger.warning("[Indexer] 图谱抽取降级: %s 暂未写入图谱，原因 - %s", source_filename, e)
            return None

    def _teardown_parser(self):
        """彻底销毁解析器对象并强行抹除显存占用"""
        if self.parser is not None:
            logger.info("[System] 解析任务完毕，开始执行显存清场与垃圾回收...")
            del self.parser
            self.parser = None
            if self.graph_extractor is not None:
                close_fn = getattr(self.graph_extractor, "close", None)
                if callable(close_fn):
                    close_fn()
                self.graph_extractor = None
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
                    logger.info("[System] VRAM 已被物理级释放，当前显卡净空，随时准备承载大语言模型！")
            except Exception:
                pass

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
