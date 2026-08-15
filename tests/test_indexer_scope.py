import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core.runtime_state as runtime_state
from core.indexer import INDEX_METADATA_SCHEMA_VERSION, DriveIndexer
from core.workspace import SoulDriveWorkspace


class IndexerScopeTests(unittest.TestCase):
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
                with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                    "core.knowledge_base.LocalKnowledgeBase",
                    _IngestRecordingKnowledgeBase,
                ):
                    indexer.sync_workspace(workspace, auth_level="BASIC")
                    state = runtime_state.get_runtime_state()
        finally:
            runtime_state.STATE_PATH = original_state_path

        observability = state["indexing"]["observability"]
        self.assertEqual(observability["schema_version"], "indexing_observability_v4")
        self.assertEqual(len(observability["documents"]), 1)
        self.assertEqual(observability["documents"][0]["source_filename"], "scheme.pdf")
        self.assertEqual(observability["documents"][0]["status"], "indexed")
        self.assertEqual(observability["documents"][0]["chunk_count"], 2)
        self.assertEqual(observability["documents"][0]["child_count"], 3)
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


class _FakeKnowledgeBase:
    def __init__(self, payload):
        self.collection = _FakeCollection(payload)


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


if __name__ == "__main__":
    unittest.main()
