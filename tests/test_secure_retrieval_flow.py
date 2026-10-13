import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.knowledge_base import (
    LocalKnowledgeBase,
    _evaluate_project_candidates,
    _project_candidate_relevance,
)
from core.rag_engine import RAGEngine
from core.retrieval import SearchCandidate
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureRetrievalFlowTests(unittest.TestCase):
    def test_project_relevance_rejects_non_finite_dense_distance(self):
        candidate = SearchCandidate(
            id="invalid-dense-score",
            content="实验指标相关说明，数据集版本一致。",
            metadata={"source_filename": "noise.md", "project_id": "p1"},
            vector_distance=float("nan"),
        )

        score = _project_candidate_relevance("实验指标在什么条件下可以比较？", candidate)

        self.assertLess(score, 1.0)

    def test_project_relevance_uses_dense_similarity_for_paraphrased_evidence(self):
        candidate = SearchCandidate(
            id="experiment-alpha",
            content="两个实验只有在指标定义、单位、数据集版本、数据划分和评测脚本兼容时才允许计算数值差异。",
            metadata={"source_filename": "experiment.md", "project_id": "p1"},
            vector_distance=0.493,
        )

        score = _project_candidate_relevance("实验指标在什么条件下可以比较？", candidate)

        self.assertGreaterEqual(score, 0.34)

    def test_project_relevance_recovers_a_semantic_hit_with_multiple_query_anchors(self):
        candidate = SearchCandidate(
            id="mail-policy",
            content="项目安全方案 邮件仅生成草案，发送前需要用户确认。",
            metadata={"source_filename": "security.md", "project_id": "p1"},
            dense_rank=1,
            vector_distance=0.7777778,
        )
        noise = SearchCandidate(
            id="unrelated",
            content="项目安全方案 研发流程和预算审批。",
            metadata={"source_filename": "plan.md", "project_id": "p1"},
            dense_rank=2,
            vector_distance=1.0,
        )

        evaluation = _evaluate_project_candidates(
            "邮件发送是否自动执行？ 当前项目标识：p1",
            [candidate, noise],
        )

        self.assertTrue(evaluation["relevant"])
        self.assertTrue(evaluation["recovery"]["eligible"])

    def test_project_relevance_keeps_explicit_comparison_source_anchor(self):
        candidate = SearchCandidate(
            id="plan-a",
            content="甲方案采用本地加密和设备验证。",
            metadata={
                "source_filename": "plan-a.md",
                "document_title": "甲方案",
                "project_id": "p1",
            },
        )

        score = _project_candidate_relevance("比较甲方案和乙方案在安全策略上的差异", candidate)

        self.assertGreaterEqual(score, 0.34)

    def test_project_fallback_when_project_candidates_are_not_relevant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-noise",
                        [_FakeChunk(
                            "cafeteria menu and parking instructions",
                            {"source_filename": "noise.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "zero trust policy requires device verification",
                            {"source_filename": "global.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "zero trust device",
                        top_k=1,
                        project_id="p1",
                    )
                finally:
                    kb.close()

        self.assertEqual({item["source_filename"] for item in result["metadatas"]}, {"global.pdf"})
        self.assertEqual(result["observability"]["project_focus"]["route"], "global_fallback")

    def test_unbound_search_uses_only_global_documents_without_explicit_cross_project_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-a",
                        [_FakeChunk(
                            "zero trust project A private evidence",
                            {"source_filename": "project-a.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-project-b",
                        [_FakeChunk(
                            "zero trust project B private evidence",
                            {"source_filename": "project-b.pdf", "page": 1, "project_id": "p2"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global",
                        [_FakeChunk(
                            "zero trust global policy evidence",
                            {"source_filename": "global.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "zero trust policy",
                        top_k=3,
                    )
                finally:
                    kb.close()

        self.assertEqual(
            {item["source_filename"] for item in result["metadatas"]},
            {"global.pdf"},
        )
        self.assertTrue(result["observability"]["project_focus"]["unbound_global_only"])

    def test_project_noise_does_not_block_relevant_global_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-noise",
                        [_FakeChunk(
                            "RAG 项目采购和预算审批流程",
                            {"source_filename": "noise.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "RAG 的核心思想是先从外部知识库检索相关证据，再生成回答",
                            {"source_filename": "global.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "RAG 的核心思想是什么？",
                        top_k=1,
                        project_id="p1",
                    )
                finally:
                    kb.close()

        self.assertEqual({item["source_filename"] for item in result["metadatas"]}, {"global.pdf"})
        self.assertEqual(result["observability"]["project_focus"]["route"], "global_fallback")

    def test_image_asset_title_keeps_project_relevance_for_scoped_queries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-screen",
                        [_FakeChunk(
                            "SoulDrive PC project workspace target_binding_accuracy 0.620 -> 0.790 -> 0.850",
                            {
                                "source_filename": "08_selection_ui.png",
                                "document_title": "选区绑定界面示意",
                                "page": 1,
                                "project_id": "p1",
                            },
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global-noise",
                        [_FakeChunk(
                            "unrelated guidance for a different project",
                            {"source_filename": "noise.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "请在当前项目语料中查找‘选区绑定界面示意’的记录。",
                        top_k=1,
                        project_id="p1",
                    )
                finally:
                    kb.close()

        self.assertEqual({item["source_filename"] for item in result["metadatas"]}, {"08_selection_ui.png"})
        self.assertIn(result["observability"]["project_focus"]["route"], {"project_first", "project_first_with_global"})

    def test_global_document_is_available_from_multiple_projects_without_cross_project_leakage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "RAG combines retrieval with generation",
                            {"source_filename": "global-rag.pdf", "page": 1},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-other-project",
                        [_FakeChunk(
                            "cafeteria menu and parking instructions",
                            {"source_filename": "other.pdf", "page": 1, "project_id": "p2"},
                        )],
                    )
                    p1 = kb.search_with_evidence("RAG retrieval generation", top_k=1, project_id="p1")
                    p2 = kb.search_with_evidence("RAG retrieval generation", top_k=1, project_id="p2")
                finally:
                    kb.close()

        self.assertEqual({item["source_filename"] for item in p1["metadatas"]}, {"global-rag.pdf"})
        self.assertEqual({item["source_filename"] for item in p2["metadatas"]}, {"global-rag.pdf"})
        self.assertNotIn("other.pdf", {item["source_filename"] for item in p1["metadatas"]})

    def test_global_evidence_reaches_generated_answer_from_each_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-noise",
                        [_FakeChunk(
                            "cafeteria menu and parking instructions",
                            {"source_filename": "noise.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-other-project-noise",
                        [_FakeChunk(
                            "cafeteria menu and parking instructions",
                            {"source_filename": "other-noise.pdf", "page": 1, "project_id": "p2"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "zero trust policy requires device verification",
                            {"source_filename": "global.pdf", "page": 1},
                        )],
                    )
                    outputs = {}
                    audit_logs = {}
                    for project_id in ("p1", "p2"):
                        engine = RAGEngine.__new__(RAGEngine)
                        engine.kb = kb
                        engine.graph_db = None
                        engine.llm = _StaticRagLlm("zero trust policy requires device verification。[E1]")
                        engine.audit_logger = _NoopAuditLogger()
                        engine.runtime_config = SimpleNamespace(
                            temperature=0.01,
                            top_p=0.85,
                            repeat_penalty=1.12,
                            max_tokens=256,
                        )
                        outputs[project_id] = "".join(engine.generate_response_stream(
                            "zero trust policy requires what?",
                            top_k=1,
                            interaction_context={
                                "active_context": {"project_id": project_id},
                            },
                        ))
                        audit_logs[project_id] = engine.audit_logger.events
                finally:
                    kb.close()

        for output in outputs.values():
            self.assertIn("zero trust policy requires device verification", output)
            self.assertIn("[E1]", output)
            self.assertIn("global.pdf", output)
            self.assertNotIn("noise.pdf", output)
            self.assertNotIn("other-noise.pdf", output)
        for events in audit_logs.values():
            retrieved = next(payload for event_type, payload in events if event_type == "rag.retrieved")
            self.assertEqual(
                retrieved["retrieval_observability"]["project_focus"]["route"],
                "global_fallback",
            )

    def test_new_project_session_without_document_selection_ignores_unrelated_visual_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path, enable_usage_ledger=False)
                try:
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "zero trust policy requires device verification",
                            {"source_filename": "global-rag.pdf", "page": 1},
                        )],
                    )
                    engine = RAGEngine.__new__(RAGEngine)
                    engine.kb = kb
                    engine.graph_db = None
                    engine.llm = _StaticRagLlm(
                        "文档说明 zero trust policy requires device verification。[E1]"
                    )
                    engine.audit_logger = _NoopAuditLogger()
                    engine.runtime_config = SimpleNamespace(
                        temperature=0.01,
                        top_p=0.85,
                        repeat_penalty=1.12,
                        max_tokens=256,
                    )
                    context = {
                        "session_id": "session-new-project",
                        "active_context": {"project_id": "project-new"},
                        "events": [{
                            "event_id": "capture-1",
                            "modality": "image",
                            "text": "本地项目界面",
                            "timestamp_ms": 1000,
                            "project_id": "project-new",
                            "source": "ocr",
                            "confidence": 0.98,
                            "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.3},
                        }],
                    }
                    output = "".join(engine.generate_response_stream(
                        "zero trust policy requires what?",
                        top_k=1,
                        interaction_context=context,
                    ))
                finally:
                    kb.close()

        self.assertIn("global-rag.pdf", output)
        self.assertIn("[E1]", output)
        self.assertNotIn("[V1]", output)
        self.assertNotIn("请选择要处理的文档", output)
        retrieved = next(payload for event_type, payload in engine.audit_logger.events if event_type == "rag.retrieved")
        self.assertEqual(retrieved["retrieval_observability"]["project_focus"]["route"], "global_fallback")
        self.assertEqual(retrieved["visual_evidence"]["decision"], "accept")
        self.assertEqual(retrieved["visual_evidence"]["evidence_count"], 1)

    def test_full_ingestion_routes_ancestor_query_through_persisted_section_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(
                    workspace_path=workspace.root_path,
                    enable_usage_ledger=False,
                )
                try:
                    kb.ingest_chunks(
                        "paper-1",
                        [
                            _FakeChunk(
                                "稠密检索负责找到语义相关的原文证据。",
                                {
                                    "source_filename": "paper.md",
                                    "section_path": ["总览", "检索", "稠密"],
                                    "page": 1,
                                },
                            ),
                            _FakeChunk(
                                "生成阶段只使用通过证据门的内容。",
                                {
                                    "source_filename": "paper.md",
                                    "section_path": ["总览", "生成"],
                                    "page": 1,
                                },
                            ),
                        ],
                    )
                    result = kb.search_with_evidence(
                        "请定位“检索”的原文",
                        top_k=1,
                    )
                finally:
                    kb.close()

        evidence = result["evidence"][0]
        assert evidence["section_route_mode"] == "descendant"
        assert evidence["section_route_node"] == "总览 > 检索"
        assert result["observability"]["section_route"][0]["modes"] == ["descendant"]
        assert result["metadatas"][0]["section_tree_document_id"] == "paper-1"

    def test_bounded_rerank_keeps_full_pool_for_source_diverse_queries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with (
                patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()),
                patch("core.knowledge_base.LocalReranker", return_value=type("Reranker", (), {"ready": False})()),
            ):
                kb = LocalKnowledgeBase(
                    workspace_path=workspace.root_path,
                    enable_usage_ledger=False,
                    rerank_candidate_limit=1,
                )
                try:
                    kb.ingest_chunks(
                        "doc-a",
                        [_FakeChunk("Alpha 方案采用检索增强生成。", {"source_filename": "alpha.pdf", "page": 1})],
                    )
                    kb.ingest_chunks(
                        "doc-b",
                        [_FakeChunk("Beta 方案采用检索增强生成。", {"source_filename": "beta.pdf", "page": 1})],
                    )
                    result = kb.search_with_evidence(
                        "比较 Alpha 与 Beta 两个方案的核心思路",
                        top_k=2,
                    )
                finally:
                    kb.close()

        observability = result["observability"]
        self.assertEqual(observability["rerank_candidate_limit"], 1)
        self.assertEqual(observability["rerank_limit_bypassed_reason"], "source_diversity")
        self.assertEqual(
            observability["stage_counts"]["rerank_input"],
            observability["stage_counts"]["hydrated"],
        )

    def test_relevant_project_candidates_do_not_hide_shared_global_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-project-rag",
                        [_FakeChunk(
                            "RAG 的核心思想在当前项目中用于组织向量检索、关键词检索和权限控制",
                            {"source_filename": "project-rag.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global-rag",
                        [_FakeChunk(
                            "RAG 的核心思想是先从外部知识库检索相关证据，再生成回答",
                            {"source_filename": "global-rag.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "RAG 的核心思想是什么？",
                        top_k=2,
                        project_id="p1",
                    )
                finally:
                    kb.close()

        sources = {item["source_filename"] for item in result["metadatas"]}
        self.assertIn("project-rag.pdf", sources)
        self.assertIn("global-rag.pdf", sources)
        self.assertEqual(result["observability"]["project_focus"]["route"], "project_first_with_global")
        self.assertEqual(result["observability"]["project_focus"]["global_candidate_count"], 1)
        self.assertEqual(result["observability"]["project_focus"]["boost"], 1.12)

    def test_broad_summary_stays_in_current_project_without_explicit_global_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-current",
                        [_FakeChunk(
                            "zero trust architecture current project summary",
                            {"source_filename": "current.pdf", "page": 1, "project_id": "p1"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-other",
                        [_FakeChunk(
                            "zero trust architecture other project summary",
                            {"source_filename": "other.pdf", "page": 1, "project_id": "p2"},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "概述当前项目的 zero trust 方案",
                        top_k=2,
                        project_id="p1",
                    )
                finally:
                    kb.close()

        self.assertEqual({item["project_id"] for item in result["metadatas"]}, {"p1"})
        self.assertIn(result["observability"]["project_focus"]["route"], {"project_first", "project_first_with_global"})

    def test_project_fallback_uses_only_unbound_global_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "doc-other",
                        [_FakeChunk(
                            "zero trust other project evidence",
                            {"source_filename": "other.pdf", "page": 1, "project_id": "p2"},
                        )],
                    )
                    kb.ingest_chunks(
                        "doc-global",
                        [_FakeChunk(
                            "zero trust global policy evidence",
                            {"source_filename": "global.pdf", "page": 1},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "zero trust policy",
                        top_k=2,
                        project_id="missing-project",
                    )
                finally:
                    kb.close()

        self.assertEqual(
            {item["source_filename"] for item in result["metadatas"]},
            {"global.pdf"},
        )
        self.assertEqual(result["observability"]["project_focus"]["route"], "global_fallback")
        self.assertEqual(result["observability"]["project_focus"]["global_candidate_count"], 1)
        self.assertEqual(result["observability"]["project_focus"]["boost"], 1.0)

    def test_project_id_prefers_current_project_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-current",
                            [_FakeChunk(
                                "zero trust architecture requires device verification",
                                {"source_filename": "current.pdf", "page": 1, "chunk_index": 0, "project_id": "project-current"},
                            )],
                        )
                        kb.ingest_chunks(
                            "doc-global",
                            [_FakeChunk(
                                "zero trust device verification is required globally",
                                {"source_filename": "global.pdf", "page": 1, "chunk_index": 0, "project_id": "project-other"},
                            )],
                        )
                        result = kb.search_with_evidence(
                            "zero trust device",
                            top_k=2,
                            project_id="project-current",
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(
            {metadata["project_id"] for metadata in result["metadatas"]},
            {"project-current"},
        )
        self.assertEqual(result["observability"]["project_focus"]["boosted_candidate_count"], 1)
        self.assertEqual(result["observability"]["project_focus"]["global_candidate_count"], 0)
        self.assertEqual(result["observability"]["project_focus"]["indexed_candidate_count"], 2)
        self.assertIn(result["observability"]["project_focus"]["route"], {"project_first", "project_first_with_global"})

    def test_retrieval_observability_counts_same_named_scoped_sources_by_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks(
                        "document-a",
                        [_FakeChunk(
                            "zero trust architecture project A evidence",
                            {"source_filename": "report.pdf", "page": 1, "project_id": "project-a"},
                        )],
                    )
                    kb.ingest_chunks(
                        "document-b",
                        [_FakeChunk(
                            "zero trust architecture project B evidence",
                            {"source_filename": "report.pdf", "page": 1, "project_id": "project-b"},
                        )],
                    )
                    result = kb.search_with_evidence(
                        "比较两个项目的 zero trust architecture",
                        top_k=2,
                        project_id="project-a",
                    )
                finally:
                    kb.close()

        self.assertEqual(result["observability"]["quality_signals"]["evidence_source_count"], 2)
        self.assertEqual(result["observability"]["stage_counts"]["final_sources"], 2)

    def test_cross_project_comparison_explicitly_uses_global_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            (Path(workspace.models_path) / "bge-small-zh-v1.5").mkdir(parents=True, exist_ok=True)
            with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                try:
                    kb.ingest_chunks("doc-a", [_FakeChunk("zero trust current", {"source_filename": "a.pdf", "page": 1, "project_id": "p1"})])
                    kb.ingest_chunks("doc-b", [_FakeChunk("zero trust other", {"source_filename": "b.pdf", "page": 1, "project_id": "p2"})])
                    result = kb.search_with_evidence("比较两个项目的 zero trust 方案", top_k=2, project_id="p1")
                finally:
                    kb.close()

        self.assertEqual({item["project_id"] for item in result["metadatas"]}, {"p1", "p2"})
        self.assertEqual(result["observability"]["project_focus"]["route"], "global_explicit")
        self.assertFalse(result["observability"]["comparison_source_reservation_active"])

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
        self.assertEqual(result["observability"]["schema_revision"], 5)
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

    def test_selected_document_scope_excludes_other_documents_and_graph_recall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-selected",
                            [_FakeChunk(
                                "zero trust selected document evidence",
                                {"source_filename": "selected.pdf", "page": 1, "chunk_index": 0},
                            )],
                        )
                        kb.ingest_chunks(
                            "doc-other",
                            [_FakeChunk(
                                "zero trust other document evidence",
                                {"source_filename": "other.pdf", "page": 1, "chunk_index": 0},
                            )],
                        )

                        class ExplodingGraphStore:
                            def recall_chunk_ids(self, *args, **kwargs):
                                raise AssertionError("selected document retrieval must skip GraphRAG")

                        result = kb.search_with_evidence(
                            "zero trust evidence",
                            graph_db=ExplodingGraphStore(),
                            top_k=2,
                            document_names=["selected.pdf"],
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(
            {item["source_filename"] for item in result["metadatas"]},
            {"selected.pdf"},
        )
        self.assertEqual(result["observability"]["stage_counts"]["graph"], 0)

    def test_selected_document_scope_uses_document_id_for_duplicate_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-object-a",
                            [_FakeChunk(
                                "zero trust duplicate object A evidence",
                                {
                                    "document_id": "secure_object-a",
                                    "source_filename": "same-name.pdf",
                                    "page": 1,
                                    "chunk_index": 0,
                                },
                            )],
                        )
                        kb.ingest_chunks(
                            "doc-object-b",
                            [_FakeChunk(
                                "zero trust duplicate object B evidence",
                                {
                                    "document_id": "secure_object-b",
                                    "source_filename": "same-name.pdf",
                                    "page": 1,
                                    "chunk_index": 0,
                                },
                            )],
                        )

                        class ExplodingGraphStore:
                            def recall_chunk_ids(self, *args, **kwargs):
                                raise AssertionError("selected document retrieval must skip GraphRAG")

                        result = kb.search_with_evidence(
                            "zero trust duplicate object evidence",
                            graph_db=ExplodingGraphStore(),
                            top_k=2,
                            document_names=["same-name.pdf"],
                            document_ids=["object-b"],
                        )
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(
            {item["document_id"] for item in result["metadatas"]},
            {"secure_object-b"},
        )
        self.assertNotIn("object-a", " ".join(result["documents"]))
        self.assertEqual(result["observability"]["stage_counts"]["graph"], 0)
        self.assertEqual(result["observability"]["document_scope"]["mode"], "document_id")
        self.assertEqual(result["observability"]["document_scope"]["requested_id_count"], 1)

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


class _StaticRagLlm:
    def __init__(self, content):
        self.content = content

    def create_chat_completion(self, messages, stream, temperature, top_p, repeat_penalty, max_tokens, stop):
        _ = (messages, stream, temperature, top_p, repeat_penalty, max_tokens, stop)
        return iter([{"choices": [{"delta": {"content": self.content}}]}])


class _NoopAuditLogger:
    def __init__(self):
        self.events = []

    def append_event(self, event_type, payload, trace_id=None):
        _ = trace_id
        self.events.append((event_type, payload))


class _FakeGraphStore:
    def __init__(self, chunk_id):
        self.chunk_id = chunk_id

    def recall_chunk_ids(self, query, limit=40, project_id=None, global_only=False):
        _ = query, project_id, global_only
        return [self.chunk_id][:limit]

    def get_all_entities(self, project_id=None, global_only=False):
        _ = project_id, global_only
        return []

    def get_subgraph(self, entity_name, depth=1, project_id=None, global_only=False):
        _ = entity_name, depth, project_id, global_only
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
