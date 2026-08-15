import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge_base import LocalKnowledgeBase
from core.retrieval import SearchCandidate
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureRetrievalFlowTests(unittest.TestCase):
    def test_secure_retrieval_decrypts_only_merged_candidate_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            load_calls = []
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    original_load = kb.secure_store.load_candidates

                    def recording_load(chunk_ids):
                        load_calls.append(list(chunk_ids))
                        return original_load(chunk_ids)

                    kb.secure_store.load_candidates = recording_load
                    try:
                        kb.ingest_chunks(
                            "doc-private",
                            [
                                _FakeChunk(
                                    "zero trust architecture requires device verification",
                                    {"source_filename": "policy.pdf", "page": 1, "chunk_index": 0},
                                ),
                                _FakeChunk(
                                    "cafeteria menu and parking instructions",
                                    {"source_filename": "misc.pdf", "page": 1, "chunk_index": 1},
                                ),
                            ],
                        )
                        result = kb.search_with_evidence("zero trust device", top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        loaded_ids = [chunk_id for batch in load_calls for chunk_id in batch]
        self.assertTrue(loaded_ids)
        self.assertLessEqual(len(set(loaded_ids)), 4)
        self.assertEqual(result["metadatas"][0]["source_filename"], "policy.pdf")
        self.assertEqual(result["observability"]["schema_version"], "retrieval_observability_v1")
        self.assertEqual(result["observability"]["schema_revision"], 3)
        self.assertEqual(result["observability"]["stage_counts"]["final"], 1)
        self.assertEqual(result["observability"]["query_intent"], "factual_lookup")
        self.assertIn("redundant_deferred_count", result["observability"]["ranking"])
        self.assertIn("mode_counts", result["observability"]["context_expansion"])
        self.assertIn("query_token_coverage", result["observability"]["quality_signals"])
        self.assertIn(
            result["observability"]["sufficiency"]["decision"],
            {"accept", "retry", "reject"},
        )
        self.assertIn("total", result["observability"]["timings_ms"])
        self.assertIn("zero trust architecture", result["documents"][0])

    def test_secure_retrieval_merges_graph_chunk_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_GraphRecallEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        chunks = [
                            _FakeChunk(
                                "graph evidence only",
                                {"source_filename": "graph.pdf", "page": 1, "chunk_index": 0},
                            )
                        ]
                        for index in range(30):
                            chunks.append(
                                _FakeChunk(
                                    f"dense filler document {index}",
                                    {"source_filename": f"dense-{index}.pdf", "page": 1, "chunk_index": index + 1},
                                )
                            )
                        kb.ingest_chunks(
                            "doc-graph",
                            chunks,
                        )
                        target_id = kb.secure_store.collection.get(where={"source_filename": "graph.pdf"}, include=[])["ids"][0]
                        graph_db = _FakeGraphStore(target_id)
                        result = kb.search_with_evidence("alpha query", graph_db=graph_db, top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(result["metadatas"][0]["source_filename"], "graph.pdf")

    def test_candidate_merge_preserves_graph_score_for_existing_dense_candidate(self):
        kb = object.__new__(LocalKnowledgeBase)
        merged = kb._merge_candidates(
            [
                SearchCandidate(
                    id="shared-child",
                    content="",
                    metadata={},
                    dense_rank=1,
                )
            ],
            [
                SearchCandidate(
                    id="shared-child",
                    content="",
                    metadata={},
                    graph_score=7.0,
                )
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].graph_score, 7.0)

    def test_delete_document_indexes_removes_secure_graph_provenance(self):
        kb = object.__new__(LocalKnowledgeBase)
        kb.secure_store = _FakeSecureStoreForDelete(["old-child", "kept-child"])
        kb.workspace = type("Workspace", (), {
            "root_path": "workspace-root",
            "secure_graph_store_path": "secure-graph.sqlite",
        })()

        with patch("core.knowledge_base.get_workspace_keys", return_value=object()), patch(
            "core.knowledge_base.SecureGraphStore",
            return_value=_RecordingSecureGraphStore(),
        ) as graph_store_factory:
            kb.delete_document_indexes("old-hash")

        graph_store = graph_store_factory.return_value
        self.assertEqual(kb.secure_store.deleted_hashes, ["old-hash"])
        self.assertEqual(graph_store.removed_chunk_ids, [["old-child"]])
        self.assertTrue(graph_store.closed)


class _FakeChunk:
    def __init__(self, page_content: str, metadata: dict):
        self.page_content = page_content
        self.metadata = metadata


class _FakeEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([
                float("zero" in lowered or "trust" in lowered or "device" in lowered),
                float("cafeteria" in lowered or "parking" in lowered),
            ])
        return _EmbeddingResult(vectors)


class _GraphRecallEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "graph evidence only" in lowered:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([1.0, 0.0])
        return _EmbeddingResult(vectors)


class _EmbeddingResult(list):
    def tolist(self):
        return list(self)


class _FakeGraphStore:
    def __init__(self, chunk_id):
        self.chunk_id = chunk_id

    def recall_chunk_ids(self, query, limit=40):
        _ = query
        return [self.chunk_id][:limit]

    def get_all_entities(self):
        return []

    def get_subgraph(self, entity_name, depth=1):
        return []


class _FakeSecureStoreForDelete:
    def __init__(self, ids):
        self.ids = ids
        self.deleted_hashes = []
        self.collection = self

    def get(self, where=None, include=None):
        _ = include
        if where == {"document_hash": "old-hash"}:
            return {"ids": [self.ids[0]]}
        return {"ids": []}

    def delete_by_document_hash(self, document_hash):
        self.deleted_hashes.append(document_hash)


class _RecordingSecureGraphStore:
    def __init__(self):
        self.removed_chunk_ids = []
        self.closed = False

    def remove_chunk_ids(self, chunk_ids):
        self.removed_chunk_ids.append(list(chunk_ids))

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
