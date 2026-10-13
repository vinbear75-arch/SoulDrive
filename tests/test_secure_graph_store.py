import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core.graph_extractor import GraphExtractor
from core.rag_engine import RAGEngine
from core.secure_graph_store import SecureGraphStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureGraphStoreTests(unittest.TestCase):
    def test_shared_secure_graph_connection_serializes_concurrent_index_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureGraphStore(
                str(Path(temp_dir) / "secure_graph.sqlite"),
                SimpleNamespace(graph_key=b"g" * 32),
            )
            start = threading.Barrier(4)
            errors = []

            def worker(worker_id):
                try:
                    start.wait(timeout=5)
                    for index in range(12):
                        store.add_entity(
                            f"entity-{worker_id}-{index}",
                            "test",
                            "concurrent graph entity",
                            chunk_ids=[f"chunk-{worker_id}-{index}"],
                        )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            entities = store.get_all_entities()
            store.close()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(entities), 48)

    def test_graph_extractor_passes_chunk_provenance_to_store(self):
        store = _RecordingGraphStore()
        extractor = object.__new__(GraphExtractor)
        extractor.db = store
        extractor.max_chunks_per_document = 8
        extractor.max_chars_per_chunk = 1800
        extractor.extract_and_store = lambda text, chunk_ids=None: store.record(chunk_ids)

        chunks = [
            type("Chunk", (), {"page_content": "Zero Trust requires Device Posture.", "metadata": {"child_ids": ["chunk-a"]}})(),
            type("Chunk", (), {"page_content": "Device Posture is checked locally.", "metadata": {"child_ids": ["chunk-b"]}})(),
        ]
        extractor.extract_chunks(chunks, "policy.pdf")

        self.assertEqual(store.chunk_ids, [["chunk-a"], ["chunk-b"]])

    def test_graph_provenance_recalls_chunks_without_plaintext_graph_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_entity("Zero Trust", "security", "device verification", chunk_ids=["chunk-a"])
                store.add_entity("Device Posture", "security", "endpoint state", chunk_ids=["chunk-b"])
                store.add_relationship(
                    "Zero Trust",
                    "Device Posture",
                    "requires",
                    evidence_chunk_ids=["chunk-a", "chunk-b"],
                )
                candidates = store.recall_chunk_ids("zero trust device", limit=10)
            finally:
                store.close()

            persisted = Path(workspace.secure_graph_store_path).read_bytes()

        self.assertEqual(candidates[:2], ["chunk-a", "chunk-b"])
        self.assertNotIn(b"Zero Trust", persisted)
        self.assertNotIn(b"Device Posture", persisted)
        self.assertNotIn(b"requires", persisted)

    def test_remove_chunk_ids_prunes_stale_graph_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_entity("Zero Trust", "security", "device verification", chunk_ids=["old-child", "kept-child"])
                store.add_entity("Legacy Only", "security", "stale evidence", chunk_ids=["old-child"])
                store.add_entity("Device Posture", "security", "endpoint state", chunk_ids=["kept-child"])
                store.add_relationship(
                    "Zero Trust",
                    "Device Posture",
                    "requires",
                    evidence_chunk_ids=["old-child", "kept-child"],
                )
                store.save_graphrag_index({"community_count": 2})

                store.remove_chunk_ids(["old-child"])
                recalled = store.recall_chunk_ids("zero trust legacy only", limit=10)
                entities = store.get_all_entities()
                invalidated = store.load_graphrag_index()
            finally:
                store.close()

        self.assertEqual(recalled, ["kept-child"])
        self.assertIsNone(invalidated)
        self.assertIn("Zero Trust", entities)
        self.assertNotIn("Legacy Only", entities)

    def test_secure_graph_recall_applies_project_filter_before_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_document(
                    "other.pdf",
                    "Other",
                    "other",
                    chunk_ids=["other-1", "other-2"],
                    project_id="project-other",
                )
                store.add_document(
                    "current.pdf",
                    "Current",
                    "current",
                    chunk_ids=["current-1"],
                    project_id="project-current",
                )
                store.add_entity(
                    "目标实体",
                    "概念",
                    "目标",
                    chunk_ids=["other-1", "other-2", "current-1"],
                )
                project_chunks = store.recall_chunk_ids(
                    "目标实体",
                    limit=1,
                    project_id="project-current",
                )
            finally:
                store.close()

        self.assertEqual(project_chunks, ["current-1"])

    def test_secure_graph_snapshot_keeps_entity_attributes_by_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_entity("同名实体", "项目 A 类型", "项目 A 描述", chunk_ids=["a-1"])
                store.add_entity("同名实体", "项目 B 类型", "项目 B 描述", chunk_ids=["b-1"])
                entity = store.get_graph_snapshot()["entities"][0]
            finally:
                store.close()

        self.assertEqual(
            entity["source_attributes"],
            [
                {"chunk_id": "a-1", "type": "项目 A 类型", "description": "项目 A 描述"},
                {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 描述"},
            ],
        )

    def test_secure_graph_chunk_pruning_removes_entity_attribute_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_entity("同名实体", "项目 A 类型", "项目 A 描述", chunk_ids=["a-1"])
                store.add_entity("同名实体", "项目 B 类型", "项目 B 描述", chunk_ids=["b-1"])
                store.remove_chunk_ids(["a-1"])
                entity = store.get_graph_snapshot()["entities"][0]
            finally:
                store.close()

        self.assertEqual(entity["source_attributes"], [
            {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 描述"},
        ])

    def test_secure_remove_project_derives_chunk_ids_when_caller_omits_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_document("project.pdf", "项目", "项目", chunk_ids=["project-1"], project_id="project-a")
                store.add_document("global.pdf", "全局", "全局", chunk_ids=["global-1"], project_id="")
                store.add_entity("共享实体", "概念", "共享", chunk_ids=["project-1", "global-1"])
                store.add_entity("关联实体", "概念", "关联", chunk_ids=["project-1", "global-1"])
                store.add_relationship(
                    "共享实体",
                    "关联实体",
                    "关联",
                    evidence_chunk_ids=["project-1", "global-1"],
                )

                store.remove_project("project-a")
                snapshot = store.get_graph_snapshot()
            finally:
                store.close()

        self.assertEqual([item["project_id"] for item in snapshot["documents"]], [""])
        self.assertTrue(all(item["chunk_ids"] == ["global-1"] for item in snapshot["entities"]))
        self.assertEqual(snapshot["relationships"][0]["chunk_ids"], ["global-1"])

    def test_secure_graph_documents_keep_same_source_filename_per_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_document(
                    "same.pdf",
                    "项目 A 报告",
                    "A",
                    chunk_ids=["a-1"],
                    project_id="project-a",
                )
                store.add_document(
                    "same.pdf",
                    "项目 B 报告",
                    "B",
                    chunk_ids=["b-1"],
                    project_id="project-b",
                )
                documents = store.get_graph_snapshot()["documents"]
            finally:
                store.close()

        self.assertEqual(
            {(item["project_id"], item["title"], tuple(item["chunk_ids"])) for item in documents},
            {
                ("project-a", "项目 A 报告", ("a-1",)),
                ("project-b", "项目 B 报告", ("b-1",)),
            },
        )
        self.assertEqual(len({item["document_id"] for item in documents}), 2)
        self.assertNotEqual(documents[0]["document_id"], documents[1]["document_id"])

    def test_secure_graph_chunk_pruning_preserves_document_project_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_document(
                    "scoped.pdf",
                    "项目 A 报告",
                    "A",
                    chunk_ids=["keep", "drop"],
                    project_id="project-a",
                )
                store.remove_chunk_ids(["drop"])
                documents_after_prune = store.get_graph_snapshot()["documents"]
                store.remove_project("project-a", ["keep"])
                documents_after_project_removal = store.get_graph_snapshot()["documents"]
            finally:
                store.close()

        self.assertEqual(documents_after_prune[0]["project_id"], "project-a")
        self.assertEqual(documents_after_project_removal, [])

    def test_secure_graph_store_returns_context_without_plaintext_leak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(str(Path(workspace.index_path) / "secure_graph.sqlite"), keys)
            try:
                store.add_entity("GraphRAG", "technique", "graph retrieval")
                store.add_entity("Local Search", "mode", "local graph search")
                store.add_relationship("GraphRAG", "Local Search", "supports")
                context = store.search_context("How does GraphRAG work?")
            finally:
                store.close()

            persisted = Path(workspace.secure_graph_store_path).read_bytes()

        self.assertEqual(context[0], "宸茬煡閫昏緫鍏崇郴: [GraphRAG] --(supports)--> [Local Search]")
        self.assertNotIn(b"GraphRAG", persisted)
        self.assertNotIn(b"Local Search", persisted)
        self.assertNotIn(b"supports", persisted)

    def test_rag_engine_uses_secure_graph_store_when_workspace_is_unlocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.rag_engine.llama_runtime_config", return_value=_FakeRuntimeConfig()), patch(
                    "core.rag_engine.resolve_chat_model_path",
                    return_value=str(Path(temp_dir) / "fake-model.gguf"),
                ), patch("core.rag_engine.os.path.exists", return_value=True), patch(
                    "core.rag_engine.load_llama_model",
                    return_value=_FakeLoadResult(),
                ):
                    engine = RAGEngine(
                        kb=object(),
                        graph_db_path=workspace.graph_db_path,
                        workspace_path=workspace.root_path,
                    )
                    try:
                        graph_db = engine.graph_db
                    finally:
                        engine.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertIsInstance(graph_db, SecureGraphStore)


class _RecordingGraphStore:
    def __init__(self):
        self.chunk_ids = []

    def record(self, chunk_ids=None):
        self.chunk_ids.append(chunk_ids or [])
        return {"entities": [], "relationships": []}


class _FakeRuntimeConfig:
    chat_n_ctx = 256
    temperature = 0.1
    top_p = 0.9
    repeat_penalty = 1.1
    max_tokens = 64


class _FakeLoadResult:
    model = object()
    effective_config = _FakeRuntimeConfig()
    load_ms = 1


if __name__ == "__main__":
    unittest.main()
