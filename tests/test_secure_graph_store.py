import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph_extractor import GraphExtractor
from core.rag_engine import RAGEngine
from core.secure_graph_store import SecureGraphStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureGraphStoreTests(unittest.TestCase):
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

                store.remove_chunk_ids(["old-child"])
                recalled = store.recall_chunk_ids("zero trust legacy only", limit=10)
                entities = store.get_all_entities()
            finally:
                store.close()

        self.assertEqual(recalled, ["kept-child"])
        self.assertIn("Zero Trust", entities)
        self.assertNotIn("Legacy Only", entities)

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
