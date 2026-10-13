import tempfile
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.documents import Document

import core.runtime_state as runtime_state
from core.indexer import (
    INDEX_METADATA_SCHEMA_VERSION,
    DriveIndexer,
    _document_navigation_summary,
    _graph_sources_missing_coverage,
)
from core.workspace import SoulDriveWorkspace


class IndexerScopeTests(unittest.TestCase):
    def test_pre_index_summary_preserves_title_sections_and_source_excerpt(self):
        summary = _document_navigation_summary("中文RAG报告.pdf", [
            Document(page_content="RAG 通过检索外部知识增强生成。", metadata={"section": "1 引言"}),
            Document(page_content="重排序用于提高证据相关性。", metadata={"section": "2 方法"}),
        ])

        self.assertIn("中文RAG报告", summary)
        self.assertIn("1 引言", summary)
        self.assertIn("检索外部知识", summary)

    def test_graph_backfill_detects_indexed_documents_without_graph_coverage(self):
        snapshot = {
            "documents": [
                {"source_filename": "covered.pdf", "chunk_ids": ["c1", "c2"]},
                {"source_filename": "missing.pdf", "chunk_ids": ["c3", "c4"]},
            ],
            "entities": [{"name": "RAG", "chunk_ids": ["c1"]}],
            "relationships": [],
        }

        self.assertEqual(
            _graph_sources_missing_coverage(
                snapshot,
                ["covered.pdf", "missing.pdf", "not-in-graph.pdf"],
            ),
            {"missing.pdf", "not-in-graph.pdf"},
        )

    def test_graph_backfill_is_disabled_for_lightweight_auth_levels(self):
        indexer = DriveIndexer()
        indexer.workspace = SimpleNamespace(root_path="unused")

        with patch("core.indexer.get_workspace_keys") as get_keys:
            self.assertEqual(
                indexer._graph_backfill_sources(["paper.pdf"], "BASIC"),
                set(),
            )
        get_keys.assert_not_called()

    def test_indexer_scans_only_managed_papers_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            managed_pdf = Path(workspace.papers_path) / "managed.pdf"
            unmanaged_pdf = Path(temp_dir) / "loose.pdf"
            managed_pdf.write_bytes(b"%PDF managed")
            unmanaged_pdf.write_bytes(b"%PDF unmanaged")

            files = DriveIndexer.discover_pdf_files(temp_dir)

        self.assertIn(str(managed_pdf), files)
        self.assertNotIn(str(unmanaged_pdf), files)

    def test_indexer_discovers_supported_project_assets_but_not_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            image = Path(workspace.papers_path) / "screen.png"
            note = Path(workspace.papers_path) / "note.txt"
            unrelated = Path(workspace.papers_path) / "archive.exe"
            image.write_bytes(b"image")
            note.write_text("note", encoding="utf-8")
            unrelated.write_bytes(b"binary")

            files = DriveIndexer.discover_workspace_document_files(workspace)

        self.assertIn(str(image), files)
        self.assertIn(str(note), files)
        self.assertNotIn(str(unrelated), files)

    def test_indexer_can_explicitly_use_original_source_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source_materials" / "rag_cn"
            source.mkdir(parents=True)
            raw_pdf = source / "raw.pdf"
            raw_pdf.write_bytes(b"%PDF raw")
            with patch.dict("os.environ", {"SOULDRIVE_INDEX_SOURCE_DIR": str(source)}):
                indexer = DriveIndexer()
                indexer.workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
                files, temp_index = indexer._discover_index_document_files()

        self.assertEqual(files, [str(raw_pdf)])
        self.assertIsNone(temp_index)

    def test_default_graph_extractor_is_bound_to_active_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            indexer = DriveIndexer()
            indexer.workspace = workspace

            with patch("core.graph_extractor.GraphExtractor") as graph_extractor:
                indexer._get_graph_extractor()

        graph_extractor.assert_called_once_with(
            graph_db_path=workspace.graph_db_path,
            workspace_path=workspace.root_path,
        )

    def test_graphrag_global_index_reads_chunk_scope_before_first_write(self):
        class FakeGraphStore:
            def get_graph_snapshot(self):
                return {
                    "documents": [{
                        "source_filename": "project-a.pdf",
                        "title": "项目 A",
                        "summary": "A",
                        "keywords": [],
                        "chunk_ids": ["chunk-a"],
                        "project_id": "",
                    }],
                    "entities": [],
                    "relationships": [],
                }

            def close(self):
                return None

        class FakeKnowledgeBase:
            def metadata_for_chunk_ids(self, chunk_ids):
                self.requested_chunk_ids = list(chunk_ids)
                return {"chunk-a": {"project_id": "project-a"}}

        class RecordingBuilder:
            calls = []

            def __init__(self, _graph_store, **kwargs):
                self.kwargs = kwargs
                self.__class__.calls.append(dict(kwargs))

            def build(self):
                return {
                    "status": "completed",
                    "algorithm": "test",
                    "document_count": 0,
                    "entity_count": 0,
                    "relationship_count": 0,
                    "community_count": 0,
                    "target_range_ok": True,
                }

        indexer = DriveIndexer()
        indexer.workspace = SimpleNamespace(
            root_path="workspace",
            graph_db_path="graph.db",
            secure_graph_store_path="secure-graph.db",
        )
        indexer.kb = FakeKnowledgeBase()
        graph_store = FakeGraphStore()
        with patch("core.indexer.get_workspace_keys", return_value=None), patch(
            "core.graph_db.LocalGraphDB", return_value=graph_store
        ), patch("core.graph_rag.GraphRAGBuilder", RecordingBuilder):
            result = indexer._build_graphrag_index("HARDWARE_PLUS_PASSWORD")

        global_calls = [
            call for call in RecordingBuilder.calls
            if call.get("index_name") == "global_shared"
        ]
        self.assertTrue(global_calls)
        self.assertEqual(
            global_calls[0].get("chunk_project_ids"),
            {"chunk-a": {"project_id": "project-a"}},
        )
        all_projects_calls = [
            call for call in RecordingBuilder.calls
            if call.get("index_name") == "all_projects"
        ]
        self.assertTrue(all_projects_calls)
        self.assertEqual(
            all_projects_calls[0].get("chunk_project_ids"),
            {"chunk-a": "project-a"},
        )
        self.assertEqual(result["all_projects"]["status"], "completed")
        self.assertTrue(result["all_projects"]["scope_manifest_complete"])
        self.assertEqual(result["all_projects"]["scope_metadata_count"], 1)
        self.assertEqual(result["all_projects"]["project_ids"], ["project-a"])

    def test_graphrag_build_marks_project_failure_as_degraded(self):
        class FakeGraphStore:
            def get_graph_snapshot(self):
                return {
                    "documents": [{"source_filename": "project-a.pdf", "chunk_ids": ["chunk-a"]}],
                    "entities": [],
                    "relationships": [],
                }

            def close(self):
                return None

        class FakeKnowledgeBase:
            def metadata_for_chunk_ids(self, _chunk_ids):
                return {"chunk-a": {"project_id": "project-a"}}

        class Builder:
            def __init__(self, _graph_store, **kwargs):
                self.project_id = kwargs.get("project_id")

            def build(self):
                if self.project_id:
                    raise RuntimeError("project graph failed secret=graph-token")
                return {"status": "completed", "algorithm": "test"}

        indexer = DriveIndexer()
        indexer.workspace = SimpleNamespace(
            root_path="workspace",
            graph_db_path="graph.db",
            secure_graph_store_path="secure-graph.db",
        )
        indexer.kb = FakeKnowledgeBase()
        with patch("core.indexer.get_workspace_keys", return_value=None), patch(
            "core.graph_db.LocalGraphDB", return_value=FakeGraphStore()
        ), patch("core.graph_rag.GraphRAGBuilder", Builder):
            result = indexer._build_graphrag_index("HARDWARE_PLUS_PASSWORD")

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["failed_project_ids"], ["project-a"])
        project_observation = result["project_indexes"][0]
        self.assertNotIn("error", project_observation)
        self.assertEqual(project_observation["error_type"], "RuntimeError")
        self.assertEqual(project_observation["error_chars"], len("project graph failed secret=graph-token"))
        self.assertNotIn("graph-token", str(result))

    def test_catalog_lookup_accepts_sha256_registration_after_md5_miss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "report.txt"
            document.write_text("catalog-bound report", encoding="utf-8")
            catalog = Mock()
            catalog.find_asset_version_for_file.side_effect = [
                None,
                {"title": "规范报告"},
            ]
            indexer = DriveIndexer()
            indexer.project_catalog = catalog

            with patch.object(indexer, "_catalog_ref_for_document", return_value="report.txt"):
                result = indexer._catalog_item_for_document(
                    str(document),
                    "report.txt",
                    "legacy-md5",
                )

        self.assertEqual(result["title"], "规范报告")
        self.assertEqual(catalog.find_asset_version_for_file.call_count, 2)

    def test_indexer_blocks_when_workspace_disk_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                SoulDriveWorkspace,
                "disk_diagnostics",
                return_value={"ready": False, "free_bytes": 1, "minimum_free_bytes": 1024},
            ):
                DriveIndexer().sync_drive(temp_dir)
                state = runtime_state.get_runtime_state()

        self.assertEqual(state["indexing"]["status"], "blocked")
        self.assertEqual(state["indexing"]["failure_summary"], {"INSUFFICIENT_DISK_SPACE": 1})
        self.assertEqual(state["indexing"]["disk"]["free_bytes"], 1)

    def test_document_index_is_not_current_without_page_metadata(self):
        indexer = DriveIndexer()
        indexer.kb = _FakeKnowledgeBase({
            "ids": ["old_child"],
            "metadatas": [{"document_hash": "abc", "metadata_schema_version": 1}],
        })

        self.assertFalse(indexer._document_index_is_current("abc"))

    def test_document_index_is_current_with_schema_and_page_metadata(self):
        indexer = DriveIndexer()
        indexer.kb = _FakeKnowledgeBase({
            "ids": ["new_child"],
            "metadatas": [{
                "document_hash": "abc",
                "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                "page": 3,
            }],
        })

        self.assertTrue(indexer._document_index_is_current("abc"))

    def test_document_index_rebuilds_when_new_hash_has_stale_previous_records(self):
        indexer = DriveIndexer()
        indexer.kb = _StaleIndexKnowledgeBase()

        self.assertEqual(
            indexer._document_index_status(
                "new-hash",
                "report.pdf",
                "D:/papers/report.pdf",
            ),
            "stale_cleanup",
        )

    def test_document_index_rebuilds_when_derived_projection_is_incomplete(self):
        indexer = DriveIndexer()
        indexer.kb = _IncompleteDerivedIndexKnowledgeBase()

        self.assertEqual(
            indexer._document_index_status("abc", "report.pdf"),
            "parent_store_incomplete",
        )

    def test_same_content_different_sources_are_indexed_independently(self):
        indexer = DriveIndexer()
        indexer.kb = _FakeKnowledgeBase({
            "ids": ["existing_child"],
            "metadatas": [{
                "document_hash": "abc",
                "source_filename": "first.png",
                "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                "page": 1,
            }],
        })

        self.assertTrue(indexer._document_index_is_current("abc", "first.png"))
        self.assertFalse(indexer._document_index_is_current("abc", "second.png"))
        self.assertNotEqual(
            indexer._index_document_id("abc", "first.png"),
            indexer._index_document_id("abc", "second.png"),
        )

    def test_document_index_is_rebuilt_when_embedding_model_changes(self):
        indexer = DriveIndexer()
        indexer.kb = _FakeKnowledgeBase({
            "ids": ["old_child"],
            "metadatas": [{
                "document_hash": "abc",
                "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                "embedding_model_name": "bge-small-zh-v1.5",
                "page": 3,
            }],
        })
        indexer.kb.embedding_model_name = "Qwen3-Embedding-0.6B"

        self.assertEqual(
            indexer._document_index_status("abc"),
            "embedding_model_upgrade",
        )

    def test_document_index_is_rebuilt_when_project_binding_is_removed(self):
        indexer = DriveIndexer()
        indexer.kb = _FakeKnowledgeBase({
            "ids": ["project_child"],
            "metadatas": [{
                "document_hash": "abc",
                "source_filename": "paper.pdf",
                "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                "embedding_model_name": "bge-small-zh-v1.5",
                "page": 1,
                "project_id": "rag-cn-demo-20260824",
                "asset_id": "asset-a",
                "asset_version_id": "version-a",
                "asset_type": "document",
                "catalog_file_ref": "paper.pdf",
            }],
        })
        indexer.project_catalog = Mock()
        indexer.project_catalog.find_asset_version_for_file.return_value = None

        with patch.object(indexer, "_catalog_ref_for_document", return_value="paper.pdf"), patch.object(
            indexer, "_project_for_path", return_value=None
        ):
            self.assertEqual(
                indexer._document_index_status("abc", "paper.pdf", "D:/papers/paper.pdf"),
                "metadata_upgrade",
            )

    def test_stale_cleanup_preserves_new_ids_and_deletes_exact_old_records(self):
        indexer = DriveIndexer()
        indexer.kb = _StaleCleanupKnowledgeBase()

        indexer._delete_previous_chunks(
            "scheme.pdf",
            "D:/papers/scheme.pdf",
            preserve_ids={"new-child"},
        )

        self.assertEqual(
            indexer.kb.deleted_records,
            [(["old-child"], ["old-parent"])],
        )

    def test_stale_cleanup_removes_legacy_hash_prefixed_records_without_source_metadata(self):
        indexer = DriveIndexer()
        indexer.kb = _LegacyStaleCleanupKnowledgeBase()

        indexer._delete_previous_chunks(
            "scheme.pdf",
            "D:/papers/scheme.pdf",
            preserve_ids={"abc_schema_6_parent_0_child_0"},
            file_hash="abc",
        )

        self.assertEqual(
            indexer.kb.deleted_records,
            [(["abc_parent_0_child_0"], ["abc_parent_0"])],
        )

    def test_stale_cleanup_does_not_delete_same_named_foreign_project_records(self):
        indexer = DriveIndexer()
        indexer.kb = _CrossProjectStaleCleanupKnowledgeBase()

        indexer._delete_previous_chunks(
            "scheme.pdf",
            "D:/papers/project-a/scheme.pdf",
            preserve_ids={"new-child"},
            project_id="project-a",
        )

        self.assertEqual(
            indexer.kb.deleted_records,
            [(["old-project-a"], ["old-parent-a"])],
        )

    def test_sync_workspace_records_indexing_observability(self):
        original_state_path = runtime_state.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                state_path = str(Path(temp_dir) / "runtime_state.json")
                workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
                pdf_path = Path(workspace.papers_path) / "scheme.pdf"
                pdf_path.write_bytes(b"%PDF fake")

                indexer = DriveIndexer()
                indexer.parser = _FakeParser()
                catalog_item = {
                    "project_id": "project-a",
                    "asset_id": "asset-a",
                    "asset_type": "report",
                    "title": "规范报告标题",
                    "version_id": "version-a",
                    "valid_from": "2026-08-01T00:00:00+00:00",
                    "authored_at": None,
                    "imported_at": "2026-08-01T00:00:00+00:00",
                    "file_ref": "scheme.pdf",
                }
                with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                    "core.knowledge_base.LocalKnowledgeBase",
                    _IngestRecordingKnowledgeBase,
                ), patch.object(
                    indexer, "_catalog_item_for_document", return_value=catalog_item
                ) as catalog_lookup:
                    indexer.sync_workspace(
                        workspace,
                        auth_level="BASIC",
                        run_id="api-index-run",
                        idempotency_key="api-index-key",
                    )
                    state = runtime_state.get_runtime_state()
        finally:
            runtime_state.STATE_PATH = original_state_path

        observability = state["indexing"]["observability"]
        self.assertEqual(state["indexing"]["run_id"], "api-index-run")
        self.assertEqual(state["indexing"]["idempotency_key"], "api-index-key")
        self.assertEqual(state["indexing"]["status"], "completed")
        self.assertEqual(observability["schema_version"], "indexing_observability_v4")
        self.assertEqual(len(observability["documents"]), 1)
        self.assertEqual(observability["documents"][0]["source_filename"], "scheme.pdf")
        self.assertEqual(observability["documents"][0]["status"], "indexed")
        self.assertEqual(observability["documents"][0]["chunk_count"], 2)
        self.assertEqual(observability["documents"][0]["child_count"], 3)
        self.assertEqual(observability["documents"][0]["section_tree_node_count"], 4)
        self.assertEqual(observability["totals"]["section_tree_leaf_count"], 2)
        self.assertEqual(observability["totals"]["embedding_ms"], 4)
        self.assertEqual(observability["totals"]["vector_store_ms"], 5)
        self.assertEqual(observability["totals"]["keyword_index_ms"], 6)
        self.assertEqual(observability["totals"]["removed_page_artifacts"], 2)
        self.assertEqual(observability["totals"]["formal_window_count"], 3)
        self.assertEqual(observability["totals"]["boundary_overlap_count"], 2)
        self.assertEqual(observability["totals"]["boundary_overlap_coverage_rate"], 1.0)
        self.assertEqual(observability["totals"]["max_embedding_input_tokens"], 420)
        self.assertEqual(observability["totals"]["conversion_error_count"], 0)
        self.assertEqual(observability["totals"]["converted_pages"], 2)
        self.assertEqual(observability["totals"]["failed_page_count"], 0)
        self.assertEqual(
            observability["totals"]["conversion_status_counts"],
            {"success": 1},
        )
        self.assertEqual(
            observability["totals"]["child_strategy_counts"],
            {"formal_structure_paragraph_window_v2": 3},
        )
        self.assertGreater(observability["totals"]["chunk_chars"], 0)
        self.assertEqual(catalog_lookup.call_count, 1)
        ingested_document_id, ingested_chunks = (
            _IngestRecordingKnowledgeBase.last_instance.ingested[0]
        )
        self.assertIn(f"_schema_{INDEX_METADATA_SCHEMA_VERSION}", ingested_document_id)
        for chunk_index, chunk in enumerate(ingested_chunks):
            self.assertEqual(chunk.metadata["source_filename"], "scheme.pdf")
            self.assertTrue(Path(chunk.metadata["source_path"]).is_absolute())
            self.assertTrue(chunk.metadata["document_hash"])
            self.assertEqual(
                chunk.metadata["metadata_schema_version"],
                INDEX_METADATA_SCHEMA_VERSION,
            )
            self.assertEqual(
                chunk.metadata["embedding_model_name"],
                "bge-small-zh-v1.5",
            )
            self.assertEqual(chunk.metadata["chunk_index"], chunk_index)
            self.assertEqual(chunk.metadata["project_id"], "project-a")
            self.assertEqual(chunk.metadata["asset_id"], "asset-a")
            self.assertEqual(chunk.metadata["asset_version_id"], "version-a")
            self.assertEqual(chunk.metadata["catalog_title"], "规范报告标题")
            self.assertEqual(chunk.metadata["document_title"], "规范报告标题")


class _FakeKnowledgeBase:
    def __init__(self, payload):
        self.collection = _FakeCollection(payload)


class _StaleIndexKnowledgeBase:
    def __init__(self):
        self.collection = _StaleIndexCollection()
        self.embedding_model_name = "bge-small-zh-v1.5"


class _IncompleteDerivedIndexKnowledgeBase:
    embedding_model_name = "bge-small-zh-v1.5"

    def __init__(self):
        self.collection = _FakeCollection({
            "ids": ["child"],
            "metadatas": [{
                "document_hash": "abc",
                "source_filename": "report.pdf",
                "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                "embedding_model_name": "bge-small-zh-v1.5",
                "page": 1,
            }],
        })

    def check_index_consistency(self, _document_hash, _source_filename):
        return "parent_store_incomplete"


class _StaleIndexCollection:
    def get(self, where=None, include=None):
        _ = include
        if where == {"document_hash": "new-hash"}:
            return {
                "ids": ["new-child"],
                "metadatas": [{
                    "document_hash": "new-hash",
                    "source_filename": "report.pdf",
                    "source_path": "D:/papers/report.pdf",
                    "metadata_schema_version": INDEX_METADATA_SCHEMA_VERSION,
                    "embedding_model_name": "bge-small-zh-v1.5",
                    "page": 1,
                }],
            }
        if where and "source_path" in where:
            return {
                "ids": ["old-child", "new-child"],
                "metadatas": [
                    {"document_hash": "old-hash", "source_filename": "report.pdf"},
                    {"document_hash": "new-hash", "source_filename": "report.pdf"},
                ],
            }
        return {"ids": [], "metadatas": []}


class _IngestRecordingKnowledgeBase:
    last_instance = None

    def __init__(self, *args, **kwargs):
        _ = args
        _ = kwargs
        self.collection = _FakeCollection({"ids": [], "metadatas": []})
        self.ingested = []
        self.embedding_model_name = "bge-small-zh-v1.5"
        type(self).last_instance = self

    def ingest_chunks(self, document_hash, chunks):
        self.ingested.append((document_hash, chunks))
        return {
            "child_count": 3,
            "section_tree_node_count": 4,
            "section_tree_leaf_count": 2,
            "embedding_ms": 4,
            "vector_store_ms": 5,
            "keyword_index_ms": 6,
            "parent_store_ms": 7,
            "child_split_ms": 8,
            "formal_window_count": 3,
            "boundary_overlap_opportunity_count": 2,
            "boundary_overlap_count": 2,
            "boundary_overlap_truncated_count": 0,
            "boundary_overlap_token_skip_count": 0,
            "max_child_tokens": 390,
            "max_embedding_input_tokens": 420,
            "embedding_token_limit": 512,
            "child_strategy_counts": {"formal_structure_paragraph_window_v2": 3},
        }

    def close(self):
        return None


class _FakeParser:
    last_parse_stats = {
        "raw_items": 8,
        "kept_items": 6,
        "removed_page_artifacts": 2,
        "removed_non_content": 0,
        "removed_duplicates": 0,
        "table_items": 0,
        "image_items": 0,
        "pages": 2,
        "parent_chunks": 2,
        "conversion_status": "success",
        "conversion_error_count": 0,
        "converted_pages": 2,
        "failed_pages": [],
    }

    def parse_and_chunk(self, pdf_path):
        _ = pdf_path
        return [
            SimpleNamespace(page_content="# 总体方案\n第一段内容。", metadata={"page": 1}),
            SimpleNamespace(page_content="第二段内容。", metadata={"page": 2}),
        ]


class _FakeCollection:
    def __init__(self, payload):
        self.payload = payload

    def get(self, where=None, include=None):
        _ = where
        _ = include
        return self.payload


class _StaleCleanupCollection:
    def get(self, where=None, include=None):
        _ = where
        _ = include
        return {
            "ids": ["old-child", "new-child"],
            "metadatas": [
                {"parent_id": "old-parent"},
                {"parent_id": "new-parent"},
            ],
        }


class _StaleCleanupKnowledgeBase:
    def __init__(self):
        self.collection = _StaleCleanupCollection()
        self.deleted_records = []

    def delete_index_records(self, *, child_ids, parent_ids):
        self.deleted_records.append((child_ids, parent_ids))


class _LegacyStaleCleanupCollection:
    def get(self, where=None, include=None):
        _ = include
        if where:
            return {
                "ids": ["abc_schema_6_parent_0_child_0"],
                "metadatas": [{"parent_id": "abc_schema_6_parent_0"}],
            }
        return {
            "ids": [
                "abc_parent_0_child_0",
                "abc_schema_6_parent_0_child_0",
                "other_parent_0_child_0",
            ],
            "metadatas": [
                {"parent_id": "abc_parent_0"},
                {"parent_id": "abc_schema_6_parent_0"},
                {"parent_id": "other_parent_0"},
            ],
        }


class _LegacyStaleCleanupKnowledgeBase:
    def __init__(self):
        self.collection = _LegacyStaleCleanupCollection()
        self.deleted_records = []

    def delete_index_records(self, *, child_ids, parent_ids):
        self.deleted_records.append((child_ids, parent_ids))


class _CrossProjectStaleCleanupCollection:
    def get(self, where=None, include=None):
        _ = include
        if where and "source_path" in where:
            return {
                "ids": ["old-project-a", "new-child"],
                "metadatas": [
                    {"parent_id": "old-parent-a", "project_id": "project-a"},
                    {"parent_id": "new-parent", "project_id": "project-a"},
                ],
            }
        return {
            "ids": ["old-project-a", "old-project-b"],
            "metadatas": [
                {"parent_id": "old-parent-a", "project_id": "project-a"},
                {"parent_id": "old-parent-b", "project_id": "project-b"},
            ],
        }


class _CrossProjectStaleCleanupKnowledgeBase:
    def __init__(self):
        self.collection = _CrossProjectStaleCleanupCollection()
        self.deleted_records = []

    def delete_index_records(self, *, child_ids, parent_ids):
        self.deleted_records.append((child_ids, parent_ids))


if __name__ == "__main__":
    unittest.main()
