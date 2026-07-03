import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge_base import LocalKnowledgeBase
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureVectorStoreTests(unittest.TestCase):
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
                ):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
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
