import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.knowledge_base import LocalKnowledgeBase
from core.knowledge_base import _contextualized_child_text
from core.knowledge_base import _merge_query_variants
from core.knowledge_base import _select_parent_context
from core.embedding_models import preferred_embedding_model_path
from core.knowledge_base import _persistent_client
from core.keyword_index import KeywordIndex
from core.workspace_crypto import initialize_keystore
from core.retrieval import SearchCandidate, classify_query_intent
from core.workspace import SoulDriveWorkspace


class KnowledgeBasePathTests(unittest.TestCase):
    def test_keyword_index_document_scope_is_applied_before_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                index.upsert_document(
                    "other",
                    "zero trust device other evidence",
                    {"source_filename": "other.pdf"},
                )
                index.upsert_document(
                    "selected",
                    "zero trust device selected evidence",
                    {"source_filename": "selected.pdf"},
                )
                results = index.search(
                    "zero trust device",
                    limit=1,
                    document_names=["selected.pdf"],
                )
            finally:
                index.close()

        self.assertEqual([item.doc_id for item in results], ["selected"])

    def test_confirmed_query_variant_is_prioritized_without_replacing_original_query(self):
        query = "RAG 的核心思想是什么？"
        confirmed = "RAG 的核心思想是什么？ 检索增强生成 retrieval augmented generation"

        variants = _merge_query_variants(query, [confirmed], limit=5)

        self.assertEqual(variants[0], query)
        self.assertEqual(variants[1], confirmed)
        self.assertLessEqual(len(variants), 5)
        self.assertEqual(len(variants), len(set(variants)))

    def test_knowledge_base_requires_explicit_workspace_or_index_path(self):
        with self.assertRaisesRegex(ValueError, "workspace_path"):
            LocalKnowledgeBase()

    def test_workspace_path_uses_workspace_index_paths_without_local_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client) as persistent_client,
                patch("core.knowledge_base._sentence_transformer"),
            ):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                self.assertTrue(kb.embedding_query_instruction.startswith("为这个句子生成表示"))
                self.assertTrue(kb.enable_usage_ledger)
                self.assertFalse(kb.enable_comparison_source_reservation)
                self.assertFalse(kb.enable_source_profile_recall)
                self.assertIsNone(kb.rerank_candidate_limit)
                self.assertTrue(kb._query_embedding_text("访问控制").endswith("访问控制"))
                kb.close()

        persistent_client.assert_called_once_with(path=workspace.chroma_path)
        chroma_client.close.assert_called_once_with()

    def test_workspace_retrieval_controls_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client),
                patch("core.knowledge_base._sentence_transformer"),
            ):
                kb = LocalKnowledgeBase(
                    workspace_path=workspace.root_path,
                    enable_usage_ledger=False,
                    enable_comparison_source_reservation=False,
                )
                self.assertFalse(kb.enable_usage_ledger)
                self.assertFalse(kb.enable_comparison_source_reservation)
                self.assertFalse(kb.enable_source_profile_recall)
                self.assertIsNone(kb.rerank_candidate_limit)
                kb.close()

    def test_locked_encrypted_index_never_silently_falls_back_to_plaintext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            Path(workspace.secure_vector_store_path).touch()

            with self.assertRaisesRegex(RuntimeError, "keys are not available"):
                LocalKnowledgeBase(workspace_path=workspace.root_path)

    def test_source_profile_recall_returns_representative_child_from_semantic_source_match(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.enable_source_profile_recall = True
        kb._source_profile_cache = {
            "alpha": {
                "profile_text": "Alpha retrieval policy",
                "records": [
                    (
                        "alpha-child",
                        "Alpha retrieval policy evidence",
                        {"source_filename": "alpha.pdf", "document_title": "Alpha"},
                    )
                ],
            },
            "beta": {
                "profile_text": "Beta unrelated notes",
                "records": [
                    (
                        "beta-child",
                        "Beta unrelated notes",
                        {"source_filename": "beta.pdf", "document_title": "Beta"},
                    )
                ],
            },
        }
        kb._source_profile_embeddings = {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
        }
        candidates = kb._source_profile_candidates(
            [[1.0, 0.0]],
            query_variants=["Alpha retrieval policy"],
            limit=1,
        )

        self.assertEqual([candidate.id for candidate in candidates], ["alpha-child"])
        self.assertEqual(candidates[0].metadata["_source_profile_rank"], 1)
        self.assertEqual(candidates[0].metadata["_source_profile_score"], 1.0)

    def test_workspace_can_bound_expensive_rerank_candidate_pool_explicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            with patch("core.knowledge_base._persistent_client", return_value=MagicMock()), patch(
                "core.knowledge_base._sentence_transformer"
            ):
                kb = LocalKnowledgeBase(
                    workspace_path=workspace.root_path,
                    enable_usage_ledger=False,
                    rerank_candidate_limit=24,
                )
                self.assertEqual(kb.rerank_candidate_limit, 24)
                kb.close()

    def test_embedding_model_can_be_loaded_from_workspace_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client),
                patch("core.knowledge_base._sentence_transformer") as sentence_transformer,
            ):
                kb = LocalKnowledgeBase(
                    db_path=workspace.chroma_path,
                    parent_doc_path=workspace.parent_doc_path,
                    keyword_index_path=workspace.keyword_index_path,
                    workspace_path=workspace.root_path,
                )
                kb.close()

        sentence_transformer.assert_not_called()

    def test_qwen3_embedding_is_preferred_when_already_installed_locally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            bge_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            qwen_path = Path(workspace.models_path) / "Qwen3-Embedding-0.6B"
            bge_path.mkdir(parents=True)
            qwen_path.mkdir(parents=True)

            selected = preferred_embedding_model_path(workspace.root_path)

        self.assertEqual(selected, str(qwen_path))

    def test_qwen3_embedding_is_preferred_across_search_dirs(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            bge_path = Path(first_dir) / "bge-small-zh-v1.5"
            qwen_path = Path(second_dir) / "Qwen3-Embedding-0.6B"
            bge_path.mkdir(parents=True)
            qwen_path.mkdir(parents=True)

            with patch(
                "core.embedding_models.model_search_dirs",
                return_value=[Path(first_dir), Path(second_dir)],
            ):
                selected = preferred_embedding_model_path()

        self.assertEqual(selected, str(qwen_path))

    def test_embedding_model_uses_cpu_device(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client),
                patch("core.knowledge_base._sentence_transformer") as sentence_transformer,
            ):
                kb = LocalKnowledgeBase(
                    db_path=workspace.chroma_path,
                    parent_doc_path=workspace.parent_doc_path,
                    keyword_index_path=workspace.keyword_index_path,
                    workspace_path=workspace.root_path,
                )
                kb._ensure_embedding_model()
                kb.close()

        sentence_transformer.assert_called_once_with(
            str(embedding_model_path)
        )

    def test_contextualized_text_does_not_strip_version_from_formal_title(self):
        text, _ = _contextualized_child_text(
            "正文",
            {
                "document_title": "企业数据平台建设方案 v1.0",
                "source_filename": "upload.pdf",
            },
        )

        self.assertIn("[DOCUMENT] 企业数据平台建设方案 v1.0", text)

    def test_contextualized_text_adds_a_short_auditable_chunk_summary(self):
        text, context = _contextualized_child_text(
            "本节说明混合检索如何保留论文的方法与实验关联。后续内容展开实现细节。",
            {"document_title": "RAG 论文", "section_path": ["方法", "检索"]},
        )

        self.assertIn("[CHUNK_SUMMARY] 方法 > 检索：本节说明混合检索如何保留论文的方法与实验关联", text)
        self.assertIn("[CHUNK_SUMMARY]", context)

    def test_contextualized_text_bridges_linked_visual_asset_caption(self):
        text, context = _contextualized_child_text(
            "正文说明模型结构。",
            {
                "document_title": "Transformer 论文",
                "page": 2,
                "image_metadata_json": (
                    '[{"asset_id":"figure-p2-1","page":2,'
                    '"caption":"Decoder architecture diagram",'
                    '"source_locator":"page:2/figure:1",'
                    '"vision_status":"pending_local_model"}]'
                ),
            },
        )

        self.assertIn("[VISUAL_ASSETS]", text)
        self.assertIn("Decoder architecture diagram", text)
        self.assertIn("page:2/figure:1", context)

    def test_section_candidates_route_by_topic_and_cover_each_tree_branch(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb._source_candidate_cache = [
            (
                "noise",
                "无关正文",
                {
                    "source_filename": "paper.pdf",
                    "section_path": ["总览", "背景"],
                    "child_excerpt": "背景说明",
                },
            ),
            (
                "generation",
                "生成正文",
                {
                    "source_filename": "paper.pdf",
                    "section_path": ["总览", "生成"],
                },
            ),
            (
                "retrieval-descendant",
                "BM25 正文",
                {
                    "source_filename": "paper.pdf",
                    "section_path": ["总览", "检索", "稀疏检索"],
                },
            ),
        ]

        candidates = kb._section_candidates(
            "请比较“检索”和“生成”的核心流程",
            limit=2,
        )

        self.assertEqual(
            {candidate.id for candidate in candidates},
            {"retrieval-descendant", "generation"},
        )
        self.assertEqual(
            kb._section_route_diagnostics,
            {
                "topic_count": 2,
                "matched_record_count": 2,
                "selected_record_count": 2,
                "covered_topic_count": 2,
                "modes": ["descendant", "exact_node"],
            },
        )
        self.assertEqual(candidates[0].metadata["_section_route_mode"], "descendant")
        self.assertEqual(candidates[0].metadata["_section_route_node"], "总览 > 检索")

    def test_candidate_merge_keeps_route_metadata_when_keyword_score_is_higher(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)

        merged = kb._merge_candidates(
            [SearchCandidate(
                id="child-1",
                content="dense content",
                metadata={"source_filename": "paper.pdf", "section": "检索"},
                keyword_score=9.0,
            )],
            [SearchCandidate(
                id="child-1",
                content="route content",
                metadata={
                    "source_filename": "paper.pdf",
                    "_section_route_mode": "exact_node",
                    "_section_route_node": "总览 > 检索",
                },
                keyword_score=2.0,
            )],
        )

        self.assertEqual(merged[0].content, "dense content")
        self.assertEqual(merged[0].metadata["_section_route_mode"], "exact_node")
        self.assertEqual(merged[0].metadata["_section_route_node"], "总览 > 检索")

    def test_parent_expansion_keeps_formal_boundary_window(self):
        prefix = "[DOCUMENT] scheme"
        candidate = SearchCandidate(
            id="child-2",
            content=f"{prefix}\n\nPrevious paragraph.\n\nCurrent paragraph.",
            metadata={
                "retrieval_context": prefix,
                "boundary_overlap_chars": len("Previous paragraph."),
                "child_start_char": 0,
                "child_end_char": len("Current paragraph."),
                "content_kind": "prose",
            },
        )

        context, metadata = _select_parent_context(
            "Current paragraph.",
            candidate,
            classify_query_intent("What are the requirements?"),
        )

        self.assertEqual(context, "Previous paragraph.\n\nCurrent paragraph.")
        self.assertEqual(
            metadata["context_expansion_mode"],
            "formal_two_paragraph_boundary_window",
        )

    def test_persistent_client_disables_chroma_anonymized_telemetry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("chromadb.PersistentClient") as persistent_client:
                _persistent_client(temp_dir)

        settings = persistent_client.call_args.kwargs["settings"]
        self.assertFalse(settings.anonymized_telemetry)

    def test_ingest_chunks_returns_stage_observability_summary(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore()
        kb.keyword_index = _FakeKeywordIndex()
        kb.embedding_model = _FakeEmbeddingModel()
        kb.collection = _FakeCollection()
        kb._source_candidate_cache = {"stale": True}

        chunks = [
            SimpleNamespace(
                page_content="\n\n".join([
                    "# 总体方案",
                    "第一段说明建设目标。",
                    "第二段说明技术架构。",
                ]),
                metadata={
                    "source_filename": "scheme.pdf",
                    "section_path": ["总体方案"],
                    "structure_aware": True,
                    "document_number": "企标〔2026〕1号",
                    "document_profile": "project_plan",
                    "clause_ids": "1.1,1.2",
                },
            )
        ]

        summary = kb.ingest_chunks("doc-hash", chunks)

        self.assertEqual(summary["parent_count"], 1)
        self.assertGreaterEqual(summary["child_count"], 1)
        self.assertGreaterEqual(summary["embedding_ms"], 0)
        self.assertGreaterEqual(summary["vector_store_ms"], 0)
        self.assertGreaterEqual(summary["keyword_index_ms"], 0)
        self.assertEqual(summary["section_tree_version"], "section_tree_v1")
        self.assertEqual(summary["section_tree_node_count"], 1)
        self.assertEqual(summary["section_tree_leaf_count"], 1)
        self.assertEqual(kb._source_candidate_cache, None)
        self.assertEqual(kb.collection.upsert_calls, 1)
        self.assertTrue(chunks[0].metadata["child_ids"])
        self.assertTrue(kb.collection.documents[0].startswith("[DOCUMENT] scheme"))
        self.assertIn("[SECTION] 总体方案", kb.collection.documents[0])
        self.assertIn("[DOCUMENT_NUMBER] 企标〔2026〕1号", kb.collection.documents[0])
        self.assertIn("[DOCUMENT_TYPE] 项目方案", kb.collection.documents[0])
        self.assertIn("[CLAUSE] 1.1,1.2", kb.collection.documents[0])
        self.assertTrue(kb.collection.metadatas[0]["section_node_id"])
        self.assertEqual(kb.collection.metadatas[0]["section_node_depth"], 1)
        self.assertEqual(kb.collection.metadatas[0]["section_node_child_count"], 0)
        self.assertTrue(kb.collection.metadatas[0]["section_node_is_leaf"])
        self.assertIn("[DOCUMENT] scheme", kb.keyword_index.documents[0])
        self.assertEqual(
            summary["child_strategy_counts"],
            {"formal_structure_paragraph_window_v2": summary["child_count"]},
        )
        self.assertIn("previous_child_id", kb.collection.metadatas[0])
        self.assertIn("next_child_id", kb.collection.metadatas[0])

    def test_ingest_chunks_enforces_embedding_model_token_limit(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore()
        kb.keyword_index = _FakeKeywordIndex()
        kb.embedding_model = _FakeTokenAwareEmbeddingModel(max_seq_length=128)
        kb.collection = _FakeCollection()
        kb._source_candidate_cache = None
        chunks = [
            SimpleNamespace(
                page_content="\n\n".join(
                    [
                        "# 数据治理方案",
                        "第一段 " + "甲" * 160,
                        "第二段 " + "乙" * 160,
                        "第三段 " + "丙" * 160,
                    ]
                ),
                metadata={
                    "document_title": "数据治理方案",
                    "document_profile": "project_plan",
                    "section_path": ["总体方案"],
                    "structure_aware": True,
                },
            )
        ]

        summary = kb.ingest_chunks("token-safe-doc", chunks)

        self.assertGreater(summary["child_count"], 3)
        self.assertEqual(summary["embedding_token_limit"], 128)
        self.assertLessEqual(summary["max_embedding_input_tokens"], 128)
        self.assertTrue(all(length <= 128 for length in kb.embedding_model.encoded_lengths))

    def test_long_document_summary_falls_back_to_compact_context(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore()
        kb.keyword_index = _FakeKeywordIndex()
        kb.embedding_model = _FakeTokenAwareEmbeddingModel(max_seq_length=128)
        kb.collection = _FakeCollection()
        kb._source_candidate_cache = None
        chunks = [SimpleNamespace(
            page_content="# RAG\n核心技术包括检索、生成和证据校验。",
            metadata={
                "document_title": "RAG 技术研究报告",
                "document_summary": "摘要 " + "长" * 2400,
                "section_path": ["核心技术"],
                "structure_aware": True,
            },
        )]

        summary = kb.ingest_chunks("long-summary-doc", chunks)

        self.assertEqual(summary["embedding_token_limit"], 128)
        self.assertLessEqual(summary["max_embedding_input_tokens"], 128)

    def test_ingest_chunks_overlaps_adjacent_parents_only_within_same_section(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore()
        kb.keyword_index = _FakeKeywordIndex()
        kb.embedding_model = _FakeEmbeddingModel()
        kb.collection = _FakeCollection()
        kb._source_candidate_cache = None
        chunks = [
            SimpleNamespace(
                page_content="First deployment requirement.",
                metadata={
                    "source_filename": "scheme.pdf",
                    "section_path": ["Deployment"],
                    "section": "Deployment",
                    "structure_aware": True,
                    "content_kind": "prose",
                    "page": 1,
                },
            ),
            SimpleNamespace(
                page_content="Second deployment requirement.",
                metadata={
                    "source_filename": "scheme.pdf",
                    "section_path": ["Deployment"],
                    "section": "Deployment",
                    "structure_aware": True,
                    "content_kind": "prose",
                    "page": 1,
                },
            ),
            SimpleNamespace(
                page_content="Acceptance requirement.",
                metadata={
                    "source_filename": "scheme.pdf",
                    "section_path": ["Acceptance"],
                    "section": "Acceptance",
                    "structure_aware": True,
                    "content_kind": "prose",
                    "page": 2,
                },
            ),
        ]

        summary = kb.ingest_chunks("boundary-doc", chunks)

        self.assertEqual(summary["boundary_overlap_opportunity_count"], 1)
        self.assertEqual(summary["boundary_overlap_count"], 1)
        self.assertGreater(kb.collection.metadatas[1]["boundary_overlap_chars"], 0)
        self.assertEqual(kb.collection.metadatas[1]["semantic_unit_count"], 2)
        self.assertIn("First deployment requirement.", kb.collection.documents[1])
        self.assertIn("Second deployment requirement.", kb.collection.documents[1])
        self.assertEqual(kb.collection.metadatas[2]["boundary_overlap_chars"], 0)

    def test_ingest_chunks_rolls_back_vectors_when_parent_write_fails(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FailingParentStore()
        kb.keyword_index = _FakeKeywordIndex()
        kb.embedding_model = _FakeEmbeddingModel()
        kb.collection = _FakeCollection()
        kb._source_candidate_cache = {"stale": True}
        chunk = SimpleNamespace(
            page_content="# 建设方案\n\n系统采用分层架构。",
            metadata={
                "source_filename": "scheme.pdf",
                "structure_aware": True,
            },
        )

        with self.assertRaisesRegex(RuntimeError, "parent write failed"):
            kb.ingest_chunks("failed-doc", [chunk])

        self.assertEqual(kb.collection.ids, [])
        self.assertTrue(kb.collection.deleted_ids)
        self.assertEqual(kb.parent_store.deleted_ids, ["failed-doc_parent_0"])
        self.assertTrue(kb.keyword_index.deleted_ids)
        self.assertEqual(kb.keyword_index.documents, [])
        self.assertNotIn("child_ids", chunk.metadata)
        self.assertEqual(kb._source_candidate_cache, {"stale": True})

    def test_delete_document_indexes_removes_local_graph_provenance(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.collection = _FakeDeleteCollection(["child-1", "child-2"])
        kb.parent_store = _FakeDeleteStore()
        kb.keyword_index = _FakeDeleteStore()
        kb.workspace = SimpleNamespace(graph_db_path="graph.sqlite")
        kb._source_candidate_cache = {"stale": True}

        with (
            patch("core.knowledge_base.os.path.exists", return_value=True),
            patch("core.knowledge_base.LocalGraphDB", return_value=_FakeGraphStore()) as graph_factory,
        ):
            kb.delete_document_indexes("doc-hash")

        graph_store = graph_factory.return_value
        self.assertEqual(graph_store.removed_chunk_ids, [["child-1", "child-2"]])
        self.assertTrue(graph_store.closed)
        self.assertEqual(kb.collection.deleted_ids, ["child-1", "child-2"])
        self.assertEqual(kb.parent_store.deleted_hashes, ["doc-hash"])
        self.assertEqual(kb.keyword_index.deleted_hashes, ["doc-hash"])
        self.assertIsNone(kb._source_candidate_cache)

    def test_delete_index_records_removes_only_requested_local_records(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.collection = _FakeCollection()
        kb.collection.add(
            embeddings=[[0.1], [0.2]],
            documents=["old", "new"],
            metadatas=[{"parent_id": "old-parent"}, {"parent_id": "new-parent"}],
            ids=["old-child", "new-child"],
        )
        kb.parent_store = _FakeParentStore()
        kb.parent_store.parent_ids = ["old-parent", "new-parent"]
        kb.keyword_index = _FakeKeywordIndex()
        kb.keyword_index.ids = ["old-child", "new-child"]
        kb.keyword_index.documents = ["old", "new"]
        kb.workspace = SimpleNamespace(graph_db_path="missing.sqlite")
        kb._source_candidate_cache = {"stale": True}

        with patch("core.knowledge_base.os.path.exists", return_value=False):
            kb.delete_index_records(
                child_ids=["old-child"],
                parent_ids=["old-parent"],
            )

        self.assertEqual(kb.collection.ids, ["new-child"])
        self.assertEqual(kb.parent_store.parent_ids, ["new-parent"])
        self.assertEqual(kb.keyword_index.ids, ["new-child"])
        self.assertIsNone(kb._source_candidate_cache)


class _FakeParentStore:
    def __init__(self):
        self.parent_ids = []
        self.deleted_ids = []

    def upsert_parent(self, parent_id, content, metadata):
        _ = content
        _ = metadata
        self.parent_ids.append(parent_id)

    def delete(self, parent_ids):
        self.deleted_ids.extend(parent_ids)
        deleted = set(parent_ids)
        self.parent_ids = [parent_id for parent_id in self.parent_ids if parent_id not in deleted]


class _FailingParentStore(_FakeParentStore):
    def upsert_parent(self, parent_id, content, metadata):
        _ = parent_id
        _ = content
        _ = metadata
        raise RuntimeError("parent write failed")


class _FakeKeywordIndex:
    def __init__(self):
        self.documents = []
        self.ids = []
        self.deleted_ids = []

    def upsert_document(self, doc_id, content, metadata):
        _ = metadata
        self.ids.append(doc_id)
        self.documents.append(content)

    def delete(self, doc_ids):
        self.deleted_ids.extend(doc_ids)
        deleted = set(doc_ids)
        kept = [
            (doc_id, content)
            for doc_id, content in zip(self.ids, self.documents)
            if doc_id not in deleted
        ]
        self.ids = [doc_id for doc_id, _content in kept]
        self.documents = [content for _doc_id, content in kept]


class _FakeEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        _ = normalize_embeddings
        return _FakeEmbeddings([[0.1, 0.2] for _text in texts])


class _CharacterTokenizer:
    def tokenize(self, text):
        return list(text)


class _FakeTokenAwareEmbeddingModel(_FakeEmbeddingModel):
    def __init__(self, max_seq_length):
        self.max_seq_length = max_seq_length
        self.tokenizer = _CharacterTokenizer()
        self.encoded_lengths = []

    def encode(self, texts, normalize_embeddings=True):
        self.encoded_lengths = [len(self.tokenizer.tokenize(text)) for text in texts]
        if any(length > self.max_seq_length for length in self.encoded_lengths):
            raise AssertionError("embedding input was truncated")
        return super().encode(texts, normalize_embeddings=normalize_embeddings)


class _FakeEmbeddings(list):
    def tolist(self):
        return list(self)


class _FakeCollection:
    def __init__(self):
        self.ids = []
        self.documents = []
        self.metadatas = []
        self.deleted_ids = []
        self.upsert_calls = 0

    def add(self, embeddings, documents, metadatas, ids):
        _ = embeddings
        self.ids.extend(ids)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)

    def upsert(self, embeddings, documents, metadatas, ids):
        self.upsert_calls += 1
        self.delete(ids)
        self.add(embeddings, documents, metadatas, ids)

    def delete(self, ids):
        self.deleted_ids.extend(ids)
        deleted = set(ids)
        kept = [
            (doc_id, document, metadata)
            for doc_id, document, metadata in zip(
                self.ids,
                self.documents,
                self.metadatas,
            )
            if doc_id not in deleted
        ]
        self.ids = [doc_id for doc_id, _document, _metadata in kept]
        self.documents = [document for _doc_id, document, _metadata in kept]
        self.metadatas = [metadata for _doc_id, _document, metadata in kept]


class _FakeDeleteCollection:
    def __init__(self, ids):
        self.ids = ids
        self.deleted_ids = []

    def get(self, where, include):
        _ = where
        _ = include
        return {"ids": self.ids}

    def delete(self, ids):
        self.deleted_ids.extend(ids)


class _FakeDeleteStore:
    def __init__(self):
        self.deleted_hashes = []

    def delete_by_document_hash(self, document_hash):
        self.deleted_hashes.append(document_hash)


class _FakeGraphStore:
    def __init__(self):
        self.removed_chunk_ids = []
        self.closed = False

    def remove_chunk_ids(self, chunk_ids):
        self.removed_chunk_ids.append(chunk_ids)

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
