from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge_base import LocalKnowledgeBase
from core.secure_vector_store import SecureVectorStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


def _capture_exception(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class SecureVectorStoreTests(unittest.TestCase):
    def test_shared_connection_concurrent_chunk_writes_are_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                def write_batch(batch_index):
                    start = batch_index * 20
                    ids = [f"concurrent-child-{index}" for index in range(start, start + 20)]
                    store.add_chunks(
                        ids=ids,
                        documents=[f"concurrent evidence {index}" for index in range(start, start + 20)],
                        metadatas=[
                            {"project_id": "concurrency", "source_filename": "concurrent.pdf"}
                            for _ in ids
                        ],
                        embeddings=[[1.0, 0.0] for _ in ids],
                    )

                with ThreadPoolExecutor(max_workers=4) as pool:
                    errors = [
                        result
                        for result in pool.map(
                            lambda batch: _capture_exception(write_batch, batch),
                            range(4),
                        )
                        if result is not None
                    ]
                self.assertEqual(errors, [])
                self.assertEqual(store.count(), 80)
            finally:
                store.close()

    def test_dense_and_keyword_filters_are_applied_before_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["other-dense-1", "other-dense-2", "current-dense"],
                    documents=[
                        "zero trust device other one",
                        "zero trust device other two",
                        "zero trust device current project",
                    ],
                    metadatas=[
                        {"project_id": "other", "source_filename": "other-1.pdf"},
                        {"project_id": "other", "source_filename": "other-2.pdf"},
                        {"project_id": "current", "source_filename": "current.pdf"},
                    ],
                    embeddings=[[1.0, 0.0], [1.0, 0.0], [0.9, 0.0]],
                )
                dense = store.dense_candidates([1.0, 0.0], limit=1, project_id="current")
                keyword = store.keyword_candidates(
                    "zero trust device",
                    limit=1,
                    project_id="current",
                )
                selected_dense = store.dense_candidates(
                    [1.0, 0.0],
                    limit=1,
                    document_names=["current.pdf"],
                )
                selected_keyword = store.keyword_candidates(
                    "zero trust device",
                    limit=1,
                    document_names=["current.pdf"],
                )
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in dense], ["current-dense"])
        self.assertEqual([item.chunk_id for item in keyword], ["current-dense"])
        self.assertEqual([item.chunk_id for item in selected_dense], ["current-dense"])
        self.assertEqual([item.chunk_id for item in selected_keyword], ["current-dense"])

    def test_dense_and_keyword_filters_can_select_one_duplicate_name_by_document_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["duplicate-a", "duplicate-b"],
                    documents=[
                        "same name object A evidence",
                        "same name object B evidence",
                    ],
                    metadatas=[
                        {
                            "document_id": "secure_object-a",
                            "source_filename": "same-name.txt",
                        },
                        {
                            "document_id": "secure_object-b",
                            "source_filename": "same-name.txt",
                        },
                    ],
                    embeddings=[[1.0, 0.0], [0.9, 0.0]],
                )
                selected_dense = store.dense_candidates(
                    [1.0, 0.0],
                    limit=4,
                    document_names=["same-name.txt"],
                    document_ids=["object-b"],
                )
                selected_keyword = store.keyword_candidates(
                    "same name object evidence",
                    limit=4,
                    document_names=["same-name.txt"],
                    document_ids=["object-b"],
                )
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in selected_dense], ["duplicate-b"])
        self.assertEqual([item.chunk_id for item in selected_keyword], ["duplicate-b"])

    def test_keyword_search_uses_stable_chunk_id_tiebreaker_for_equal_scores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["chunk-b", "chunk-a"],
                    documents=["same retrieval signal", "same retrieval signal"],
                    metadatas=[
                        {"source_filename": "b.pdf"},
                        {"source_filename": "a.pdf"},
                    ],
                    embeddings=[[1.0, 0.0], [1.0, 0.0]],
                )
                results = store.keyword_search("retrieval signal", limit=2)
            finally:
                store.close()

        self.assertEqual([item.doc_id for item in results], ["chunk-a", "chunk-b"])

    def test_dense_candidates_do_not_decrypt_chunk_text_until_loaded_by_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["alpha-child", "beta-child"],
                    documents=["alpha private answer", "beta unrelated"],
                    metadatas=[
                        {"parent_id": "alpha-parent", "source_filename": "alpha.pdf"},
                        {"parent_id": "beta-parent", "source_filename": "beta.pdf"},
                    ],
                    embeddings=[[1.0, 0.0], [0.0, 1.0]],
                )
                candidates = store.dense_candidates([1.0, 0.0], limit=1)
                loaded = store.load_candidates([item.chunk_id for item in candidates])
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in candidates], ["alpha-child"])
        self.assertEqual(candidates[0].content, None)
        self.assertEqual(loaded[0].content, "alpha private answer")
        self.assertEqual(loaded[0].metadata["source_filename"], "alpha.pdf")

    def test_load_candidates_preserves_requested_order_and_skips_missing_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["first", "second"],
                    documents=["first text", "second text"],
                    metadatas=[{"parent_id": "p1"}, {"parent_id": "p2"}],
                    embeddings=[[1.0], [0.5]],
                )
                loaded = store.load_candidates(["second", "missing", "first"])
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in loaded], ["second", "first"])

    def test_keyword_candidates_use_hmac_postings_without_plaintext_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["policy-child", "other-child"],
                    documents=["zero trust policy requires device verification", "lunch menu unrelated"],
                    metadatas=[
                        {"parent_id": "policy-parent", "source_filename": "policy.pdf"},
                        {"parent_id": "other-parent", "source_filename": "other.pdf"},
                    ],
                    embeddings=[[1.0], [0.0]],
                )
                candidates = store.keyword_candidates("zero trust", limit=5)
            finally:
                store.close()

            persisted = Path(workspace.secure_vector_store_path).read_bytes()

        self.assertEqual(candidates[0].chunk_id, "policy-child")
        self.assertNotIn(b"zero trust", persisted)
        self.assertNotIn(b"policy requires", persisted)

    def test_delete_by_document_hash_removes_only_deleted_keyword_postings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.upsert_parent("deleted-parent", "deleted parent content", {"document_hash": "old-hash"})
                store.upsert_parent("kept-parent", "kept parent content", {"document_hash": "kept-hash"})
                store.add_chunks(
                    ids=["deleted-child", "kept-child"],
                    documents=["zero trust deleted", "zero trust kept"],
                    metadatas=[
                        {"parent_id": "deleted-parent", "document_hash": "old-hash"},
                        {"parent_id": "kept-parent", "document_hash": "kept-hash"},
                    ],
                    embeddings=[[1.0], [1.0]],
                )
                store.delete_by_document_hash("old-hash")
                candidates = store.keyword_candidates("zero trust", limit=5)
                loaded = store.load_candidates(["kept-child", "deleted-child"])
                kept_parent = store.get_parent("kept-parent")
                deleted_parent = store.get_parent("deleted-parent")
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in candidates], ["kept-child"])
        self.assertEqual([item.chunk_id for item in loaded], ["kept-child"])
        self.assertIsNotNone(kept_parent)
        self.assertIsNone(deleted_parent)

    def test_delete_child_ids_removes_only_their_keyword_postings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["deleted-child", "kept-child"],
                    documents=["uniquedeleted evidence", "uniquekept evidence"],
                    metadatas=[
                        {"parent_id": "deleted-parent"},
                        {"parent_id": "kept-parent"},
                    ],
                    embeddings=[[1.0], [1.0]],
                )
                store.delete(["deleted-child"])
                deleted_matches = store.keyword_candidates("uniquedeleted", limit=5)
                kept_matches = store.keyword_candidates("uniquekept", limit=5)
            finally:
                store.close()

        self.assertEqual(deleted_matches, [])
        self.assertEqual([item.chunk_id for item in kept_matches], ["kept-child"])

    def test_keyword_candidates_index_terms_beyond_short_token_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                long_prefix = " ".join(f"filler{i}" for i in range(300))
                store.add_chunks(
                    ids=["late-keyword-child"],
                    documents=[f"{long_prefix} latekeyword"],
                    metadatas=[{"parent_id": "late-parent", "source_filename": "late.pdf"}],
                    embeddings=[[1.0]],
                )
                candidates = store.keyword_candidates("latekeyword", limit=5)
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in candidates], ["late-keyword-child"])

    def test_secure_store_persists_chunks_without_plaintext_leak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            self._prepare_embedding_model(workspace)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-hash-1",
                            [
                                _FakeChunk(
                                    "GraphRAG local search improves paper question answering.",
                                    {"source_filename": "graph.pdf", "page": 1, "chunk_index": 0},
                                )
                            ],
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

            persisted_bytes = Path(workspace.secure_vector_store_path).read_bytes()

        self.assertNotIn(b"GraphRAG", persisted_bytes)
        self.assertNotIn(b"graph.pdf", persisted_bytes)

    def test_secure_ingestion_preserves_section_tree_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            self._prepare_embedding_model(workspace)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-hash-tree",
                            [
                                _FakeChunk(
                                    "混合检索负责召回候选证据。",
                                    {
                                        "source_filename": "tree.pdf",
                                        "page": 1,
                                        "section_path": ["总览", "检索"],
                                    },
                                )
                            ],
                        )
                        result = kb.search_with_evidence(
                            "请定位“检索”的原文",
                            top_k=1,
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        metadata = result["metadatas"][0]
        self.assertEqual(metadata["section_tree_version"], "section_tree_v1")
        self.assertEqual(metadata["section_node_title"], "检索")
        self.assertEqual(metadata["section_node_depth"], 2)
        self.assertEqual(metadata["_section_route_source"], "persisted_section_tree_v1")

    def test_secure_store_search_returns_matching_parent_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            self._prepare_embedding_model(workspace)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-hash-graph",
                            [
                                _FakeChunk(
                                    "GraphRAG local search improves paper question answering.",
                                    {"source_filename": "graph.pdf", "page": 1, "chunk_index": 0},
                                )
                            ],
                        )
                        kb.ingest_chunks(
                            "doc-hash-vision",
                            [
                                _FakeChunk(
                                    "Vision transformer image classification baseline.",
                                    {"source_filename": "vision.pdf", "page": 2, "chunk_index": 0},
                                )
                            ],
                        )

                        result = kb.search_with_evidence("GraphRAG paper search", top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(result["metadatas"][0]["source_filename"], "graph.pdf")
        self.assertIn("GraphRAG local search", result["documents"][0])

    def test_search_with_evidence_uses_query_expansion_and_reranker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            self._prepare_embedding_model(workspace)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_PrivacyEmbeddingModel()), patch(
                    "core.knowledge_base.expand_query_variants",
                    return_value=["这个方案怎么保护数据不出域？", "数据安全与隐私保护"],
                ), patch(
                    "core.knowledge_base.LocalReranker",
                    return_value=_FakeReranker(),
                ), patch("core.knowledge_base.document_weight") as usage_weight, patch(
                    "core.knowledge_base.record_document_hits"
                ) as record_hits:
                    kb = LocalKnowledgeBase(
                        workspace_path=workspace.root_path,
                        enable_usage_ledger=False,
                    )
                    try:
                        kb.ingest_chunks(
                            "doc-hash-generic",
                            [
                                _FakeChunk(
                                    "通用技术说明，主要讨论系统结构。",
                                    {"source_filename": "generic.pdf", "page": 1, "chunk_index": 0},
                                )
                            ],
                        )
                        kb.ingest_chunks(
                            "doc-hash-privacy",
                            [
                                _FakeChunk(
                                    "本方案通过数据安全与隐私保护机制保障离线知识库。",
                                    {"source_filename": "privacy.pdf", "page": 2, "chunk_index": 0},
                                )
                            ],
                        )

                        result = kb.search_with_evidence("这个方案怎么保护数据不出域？", top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(result["metadatas"][0]["source_filename"], "privacy.pdf")
        self.assertIn("隐私保护", result["documents"][0])
        usage_weight.assert_not_called()
        record_hits.assert_not_called()
        self.assertFalse(result["observability"]["usage_ledger_enabled"])

    def test_search_with_evidence_uses_source_title_signal_for_named_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            self._prepare_embedding_model(workspace)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_TitleEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-hash-generic-private",
                            [
                                _FakeChunk(
                                    "私有化 权限 本地化 知识库 问答 RAG 检索增强生成",
                                    {"source_filename": "知识库问答场景中大语言模型私有化研究与应用实践.pdf", "page": 1},
                                )
                            ],
                        )
                        kb.ingest_chunks(
                            "doc-hash-maxkb",
                            [
                                _FakeChunk(
                                    "系统介绍本地化知识库问答流程。",
                                    {"source_filename": "基于大模型和RAG的知识库问答系统.pdf", "page": 2},
                                )
                            ],
                        )

                        result = kb.search_with_evidence(
                            "关于《基于大模型和RAG的知识库问答系统》里的私有化 权限 本地化问题",
                            top_k=1,
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(result["metadatas"][0]["source_filename"], "基于大模型和RAG的知识库问答系统.pdf")

    def _prepare_embedding_model(self, workspace: SoulDriveWorkspace):
        embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
        embedding_model_path.mkdir(parents=True, exist_ok=True)


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
            vector = [
                float(lowered.count("graphrag") + lowered.count("graph")),
                float(lowered.count("search") + lowered.count("paper")),
                float(lowered.count("vision") + lowered.count("transformer")),
            ]
            if normalize_embeddings:
                norm = sum(component * component for component in vector) ** 0.5
                if norm > 0:
                    vector = [component / norm for component in vector]
            vectors.append(vector)
        return _EmbeddingResult(vectors)


class _EmbeddingResult(list):
    def tolist(self):
        return list(self)


class _PrivacyEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            lowered = text.lower()
            vector = [
                float("数据安全" in text or "隐私保护" in text),
                float("系统结构" in text or "通用技术" in text or "generic" in lowered),
            ]
            if normalize_embeddings:
                norm = sum(component * component for component in vector) ** 0.5
                if norm > 0:
                    vector = [component / norm for component in vector]
            vectors.append(vector)
        return _EmbeddingResult(vectors)


class _FakeReranker:
    ready = True

    def score(self, query, passages):
        _ = query
        return [0.95 if "隐私保护" in passage else 0.05 for passage in passages]


class _TitleEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            vector = [
                float("私有化" in text or "权限" in text or "本地化" in text),
                float("基于大模型和RAG的知识库问答系统" in text),
            ]
            if normalize_embeddings:
                norm = sum(component * component for component in vector) ** 0.5
                if norm > 0:
                    vector = [component / norm for component in vector]
            vectors.append(vector)
        return _EmbeddingResult(vectors)


if __name__ == "__main__":
    unittest.main()
