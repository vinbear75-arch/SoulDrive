import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.runtime_state as runtime_state
from core.indexer import DriveIndexer
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
            "metadatas": [{"document_hash": "abc", "metadata_schema_version": 3, "page": 3}],
        })

        self.assertTrue(indexer._document_index_is_current("abc"))


class _FakeKnowledgeBase:
    def __init__(self, payload):
        self.collection = _FakeCollection(payload)


class _FakeCollection:
    def __init__(self, payload):
        self.payload = payload

    def get(self, where=None, include=None):
        _ = where
        _ = include
        return self.payload


if __name__ == "__main__":
    unittest.main()
