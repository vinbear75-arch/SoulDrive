import unittest
from types import SimpleNamespace

from core.graph_rag import (
    GraphRAGBuilder,
    GraphRAGIndex,
    _is_graph_entity_noise,
    leiden_partition,
    route_graph_query,
)
from core.indexer import _graph_features_enabled
from core.rag_engine import RAGEngine, _prioritize_context_evidence, gate_retrieval_context


class GraphRagTests(unittest.TestCase):
    def test_query_router_keeps_simple_fact_on_hybrid_path(self):
        route = route_graph_query("RAG 的核心思想是什么？")

        self.assertFalse(route.use_graph)
        self.assertEqual(route.mode, "hybrid")

    def test_query_router_selects_global_and_local_modes(self):
        global_route = route_graph_query("请总结这些论文关于检索纠错的共同思路")
        local_route = route_graph_query("GraphRAG 中实体之间的影响链和上下游关系是什么？")

        self.assertTrue(global_route.use_graph)
        self.assertEqual(global_route.mode, "global")
        self.assertTrue(local_route.use_graph)
        self.assertEqual(local_route.mode, "local")

    def test_local_unlocked_pc_can_build_graph_without_pro_license_label(self):
        self.assertTrue(_graph_features_enabled("HARDWARE_PLUS_PASSWORD"))
        self.assertFalse(_graph_features_enabled("BASIC"))

    def test_topic_community_filters_filenames_pages_and_author_initials(self):
        self.assertTrue(_is_graph_entity_noise("paper.pdf"))
        self.assertTrue(_is_graph_entity_noise("102600"))
        self.assertTrue(_is_graph_entity_noise("8/8"))
        self.assertTrue(_is_graph_entity_noise("ABOOTORABI M M"))
        self.assertFalse(_is_graph_entity_noise("GraphRAG"))

    def test_leiden_is_deterministic_on_three_related_communities(self):
        groups = [
            ("rag", "retrieval", "embedding"),
            ("graph", "leiden", "community"),
            ("agent", "memory", "tool"),
        ]
        adjacency = {node: {} for group in groups for node in group}
        for group in groups:
            for source, target in zip(group, group[1:]):
                adjacency[source][target] = 1.0
                adjacency[target][source] = 1.0

        first = leiden_partition(adjacency, seed=17)
        second = leiden_partition(adjacency, seed=17)

        self.assertEqual(
            [sorted(group) for group in first],
            [sorted(group) for group in second],
        )
        self.assertEqual(len(first), 3)

    def test_builder_persists_reports_and_index_can_reload_them(self):
        store = _MemoryGraphStore(_graph_snapshot())
        payload = GraphRAGBuilder(
            store,
            report_generator=lambda community: f"社区摘要：{community['title']}",
            min_communities=3,
            max_communities=4,
        ).build()

        self.assertTrue(payload["target_range_ok"])
        self.assertEqual(payload["community_count"], 3)
        self.assertTrue(all(item["report_method"] == "llm" for item in payload["communities"]))
        self.assertEqual(store.saved["memory_policy"], "document_derived_index_only; user_memory_is_not_graph_data")

        index = GraphRAGIndex(store)
        diagnostics = index.diagnostics()
        result = index.retrieve("请总结这些论文的共同主题", mode="global", limit=4)

        self.assertTrue(diagnostics["ready"])
        self.assertEqual(diagnostics["community_count"], 3)
        self.assertTrue(all(item["entity_names"] for item in diagnostics["communities"]))
        self.assertTrue(all(item["source_filenames"] for item in diagnostics["communities"]))
        self.assertTrue(all(item["report"] for item in diagnostics["communities"]))
        self.assertEqual(len(result["communities"]), 3)
        self.assertTrue(result["chunk_ids"])
        self.assertTrue(all(item["derived_index"] for item in result["evidence"]))
        levels = payload["levels"]
        self.assertEqual(levels[0]["community_id"], "ROOT")
        self.assertTrue(any(item["level"] == 1 and item["community_id"].startswith("T") for item in levels))
        self.assertTrue(any(item["level"] == 2 and item["community_id"].startswith("C") for item in levels))

    def test_builder_caps_graph_corpus_without_removing_raw_documents(self):
        snapshot = _graph_snapshot()
        snapshot["documents"].extend(
            {
                "source_filename": f"英文论文{index}.pdf",
                "title": "RAG paper",
                "summary": "English RAG paper",
                "keywords": ["RAG"],
                "chunk_ids": [],
            }
            for index in range(10)
        )
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(store, max_documents=4).build()

        self.assertEqual(payload["document_count"], 4)
        self.assertEqual(len(snapshot["documents"]), 13)
        self.assertTrue(all(item["source_filenames"] for item in payload["communities"]))

    def test_project_index_is_saved_and_retrieved_without_global_sources(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b"
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        index = GraphRAGIndex(store)
        diagnostics = index.diagnostics(project_id="project-a")
        result = index.retrieve("请总结检索和向量的关系", mode="global", project_id="project-a")

        self.assertEqual(payload["index_name"], "project:project-a")
        self.assertEqual(diagnostics["project_id"], "project-a")
        self.assertEqual(diagnostics["document_count"], 1)
        self.assertTrue(result["communities"])
        self.assertTrue(all(
            source == "论文1.pdf"
            for evidence in result["evidence"]
            for source in evidence.get("source_filenames") or []
        ))

    def test_project_graph_retrieval_keeps_shared_global_communities(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        snapshot["documents"][0]["project_id"] = "project-a"
        for chunk_id in snapshot["documents"][0]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-a"
        snapshot["documents"][2]["project_id"] = "project-b"
        for chunk_id in snapshot["documents"][2]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-b"
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        result = GraphRAGIndex(store).retrieve_project_and_global(
            "请总结检索和图谱的共同思路",
            mode="global",
            project_id="project-a",
        )

        self.assertEqual(result["scope_fallback"], "project_plus_global")
        self.assertTrue(result["project_chunk_ids"])
        self.assertTrue(result["global_chunk_ids"])
        self.assertTrue(any(item["graph_scope"] == "project" for item in result["evidence"]))
        self.assertTrue(any(item["graph_scope"] == "global_shared" for item in result["evidence"]))

    def test_shared_global_index_is_project_safe_fallback(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            if index == 0:
                document["project_id"] = "project-a"
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = "project-a"
            elif index == 2:
                document["project_id"] = "project-b"
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = "project-b"
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        index = GraphRAGIndex(store)

        diagnostics = index.diagnostics(project_id="project-a")
        result = index.retrieve(
            "请总结图谱和社区的关系",
            mode="global",
            project_id="project-a",
        )

        self.assertEqual(payload["document_count"], 1)
        self.assertTrue(diagnostics["ready"])
        self.assertEqual(diagnostics["scope_fallback"], "global_shared")
        self.assertEqual(diagnostics["project_id"], "project-a")
        self.assertTrue(result["scope_fallback"] == "global_shared")
        self.assertTrue(all(
            source == "论文2.pdf"
            for evidence in result["evidence"]
            for source in evidence.get("source_filenames") or []
        ))

    def test_shared_global_scope_uses_current_chunk_metadata_over_stale_document_scope(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for document in snapshot["documents"]:
            document["project_id"] = "legacy-project"
            for chunk_id in document["chunk_ids"]:
                # Empty project_id is meaningful: it is an explicitly global
                # chunk, not missing metadata.
                chunk_project_ids[chunk_id] = {"project_id": ""}
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        self.assertEqual(payload["document_count"], len(snapshot["documents"]))

    def test_engine_rereads_shared_global_sources_without_project_filter(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            if index == 0:
                document["project_id"] = "project-a"
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = "project-a"
            elif index == 2:
                document["project_id"] = "project-b"
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = "project-b"
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        source_loader = _GlobalSourceKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = GraphRAGIndex(store)
        engine.kb = source_loader
        query = "请总结图谱和社区的关系"
        result = engine._apply_graph_rag_route(
            {"documents": [], "metadatas": [], "evidence": [], "graph_context": [], "matched_entities": []},
            route_graph_query(query),
            query,
            project_id="project-a",
        )

        self.assertEqual(source_loader.project_ids, [None])
        self.assertEqual(result["observability"]["graph_rag"]["scope_fallback"], "global_shared")
        self.assertEqual(result["evidence"][0]["source_filename"], "论文2.pdf")
        self.assertNotIn("论文1.pdf", {item["source_filename"] for item in result["metadatas"]})
        self.assertNotIn("论文3.pdf", {item["source_filename"] for item in result["metadatas"]})

    def test_engine_rereads_project_and_global_graph_sources_separately(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        snapshot["documents"][0]["project_id"] = "project-a"
        for chunk_id in snapshot["documents"][0]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-a"
        snapshot["documents"][2]["project_id"] = "project-b"
        for chunk_id in snapshot["documents"][2]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-b"
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        source_loader = _ProjectAndGlobalSourceKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = GraphRAGIndex(store)
        engine.kb = source_loader
        query = "请总结检索和图谱的共同思路"

        result = engine._apply_graph_rag_route(
            {"documents": [], "metadatas": [], "evidence": [], "graph_context": [], "matched_entities": []},
            route_graph_query(query),
            query,
            project_id="project-a",
        )

        self.assertEqual(result["observability"]["graph_rag"]["scope_fallback"], "project_plus_global")
        self.assertEqual(len(source_loader.requests), 2)
        self.assertEqual(source_loader.requests[0][1:], ("project-a", False))
        self.assertEqual(source_loader.requests[1][1:], (None, True))
        self.assertEqual(
            {item["source_filename"] for item in result["evidence"]},
            {"项目方案.pdf", "全局标准.pdf"},
        )

    def test_engine_uses_reports_for_navigation_and_raw_chunks_for_evidence(self):
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = _FakeGraphIndex()
        engine.kb = _FakeSourceKnowledgeBase()
        result = engine._apply_graph_rag_route(
            {
                "documents": ["普通检索证据"],
                "metadatas": [{"source_filename": "普通.pdf"}],
                "evidence": [{"candidate_id": "ordinary", "score": 0.2, "source_filename": "普通.pdf"}],
                "graph_context": [],
                "matched_entities": [],
                "retrieval_mode": "hybrid",
            },
            route_graph_query("总结这些论文的共同思路"),
            "总结这些论文的共同思路",
        )

        self.assertTrue(any("索引期社区摘要" in item for item in result["graph_context"]))
        self.assertEqual(result["evidence"][0]["candidate_id"], "raw-child-1")
        self.assertNotIn("GraphRAG社区报告", result["evidence"][0]["source_filename"])
        self.assertEqual(result["observability"]["graph_rag"]["source_evidence_count"], 1)
        self.assertEqual(result["observability"]["graph_rag"]["source_count"], 1)

    def test_graph_and_hybrid_merge_deduplicates_candidates_and_reassigns_evidence_ids(self):
        documents, metadatas, evidence = _prioritize_context_evidence(
            ["图谱回读原文", "普通检索同一原文", "另一份原文"],
            [
                {"source_filename": "same.pdf", "project_id": "p1"},
                {"source_filename": "same.pdf", "project_id": "p1"},
                {"source_filename": "other.pdf", "project_id": "p1"},
            ],
            [
                {"id": "E1", "candidate_id": "same-child", "score": 0.6},
                {"id": "E1", "candidate_id": "same-child", "score": 0.4},
                {"id": "E1", "candidate_id": "other-child", "score": 0.5},
            ],
        )

        assert documents == ["图谱回读原文", "另一份原文"]
        assert [item["candidate_id"] for item in evidence] == ["same-child", "other-child"]
        assert [item["id"] for item in evidence] == ["E1", "E2"]

    def test_graph_noise_cannot_displace_higher_scored_hybrid_evidence(self):
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = SimpleNamespace(
            retrieve=lambda query, mode="local", limit=4, project_id=None: {
                "communities": ["C001"],
                "graph_context": ["项目图谱导航"],
                "evidence": [],
                "chunk_ids": ["project-noise"],
                "matched_entities": [],
            }
        )
        engine.kb = SimpleNamespace(
            evidence_for_chunk_ids=lambda chunk_ids, top_k=4, query="", project_id=None: {
                "documents": [
                    "这是当前项目的一段较长但与问题无关的流程说明，包含项目背景、采购和审批信息。"
                ],
                "metadatas": [{"source_filename": "project-noise.pdf", "project_id": "p1"}],
                "evidence": [{
                    "candidate_id": "project-noise",
                    "source_filename": "project-noise.pdf",
                    "score": 0.22,
                    "snippet": "这是当前项目的一段较长但与问题无关的流程说明",
                }],
            }
        )
        base_result = {
            "documents": ["图谱关系表示实体之间的上下游联系，可以用于组织知识"],
            "metadatas": [{"source_filename": "global-rag.pdf"}],
            "evidence": [{
                "candidate_id": "global-rag",
                "source_filename": "global-rag.pdf",
                "score": 0.60,
                "snippet": "图谱关系表示实体之间的上下游联系，可以用于组织知识",
            }],
            "graph_context": [],
            "matched_entities": [],
            "retrieval_mode": "hybrid",
        }

        query = "请总结图谱关系"
        merged = engine._apply_graph_rag_route(
            base_result,
            route_graph_query(query),
            query,
            project_id="p1",
        )
        gated, gate = gate_retrieval_context(query, merged, top_k=1)

        self.assertEqual([item["source_filename"] for item in gated["evidence"]], ["global-rag.pdf"])
        self.assertEqual(gate.decision, "accept")

    def test_engine_uses_global_graphrag_index_for_explicit_cross_project_query(self):
        graph_index = _ScopedFakeGraphIndex()
        source_loader = _RecordingSourceKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = graph_index
        engine.kb = source_loader

        query = "比较两个项目的方案并分析共同思路"
        engine._apply_graph_rag_route(
            {
                "documents": [],
                "metadatas": [],
                "evidence": [],
                "graph_context": [],
                "matched_entities": [],
                "retrieval_mode": "hybrid",
            },
            route_graph_query(query),
            query,
            project_id="project-current",
        )

        self.assertIsNone(graph_index.project_ids[-1])
        self.assertIsNone(source_loader.project_ids[-1])

    def test_graph_source_evidence_is_ranked_by_query_before_context_gate(self):
        from core.knowledge_base import LocalKnowledgeBase

        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = _FakeSecureStore(
            [
                SimpleNamespace(
                    chunk_id="unrelated",
                    content="项目说明与窗口绑定。",
                    metadata={"source_filename": "项目说明.md"},
                ),
                SimpleNamespace(
                    chunk_id="rag",
                    content="RAG 检索相关信息经过生成模型整合。",
                    metadata={"source_filename": "rag.pdf"},
                ),
            ]
        )
        result = kb.evidence_for_chunk_ids(
            ["unrelated", "rag"],
            top_k=2,
            query="请总结 RAG 检索和生成的共同思路",
        )

        self.assertEqual(result["evidence"][0]["candidate_id"], "rag")

    def test_graph_source_evidence_respects_project_scope(self):
        from core.knowledge_base import LocalKnowledgeBase

        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = _FakeSecureStore(
            [
                SimpleNamespace(chunk_id="current", content="当前项目证据", metadata={"project_id": "p1", "source_filename": "current.pdf"}),
                SimpleNamespace(chunk_id="other", content="其他项目证据", metadata={"project_id": "p2", "source_filename": "other.pdf"}),
            ]
        )
        result = kb.evidence_for_chunk_ids(["other", "current"], project_id="p1")

        self.assertEqual([item["source_filename"] for item in result["evidence"]], ["current.pdf"])

    def test_simple_fact_search_skips_graph_recall_when_router_is_hybrid(self):
        from core.knowledge_base import LocalKnowledgeBase

        class _FakeEmbeddings:
            def encode(self, texts, normalize_embeddings=True):
                _ = normalize_embeddings
                return _FakeEncoded(len(texts))

        class _FakeEncoded:
            def __init__(self, count):
                self.count = count

            def tolist(self):
                return [[0.0] for _ in range(self.count)]

        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.embedding_model = _FakeEmbeddings()
        kb.enable_source_profile_recall = False
        kb.enable_comparison_source_reservation = False
        kb.enable_usage_ledger = False
        kb.rerank_candidate_limit = None
        kb.secure_store = None
        kb.workspace = SimpleNamespace(root_path="")
        kb.reranker = SimpleNamespace(ready=False)
        kb._ensure_embedding_model = lambda: None
        kb._query_embedding_text = lambda text: text
        kb._dense_candidates = lambda *args, **kwargs: []
        kb._keyword_candidates = lambda *args, **kwargs: []
        kb._source_title_candidates = lambda *args, **kwargs: []
        kb._source_profile_candidates = lambda *args, **kwargs: []
        kb._section_candidates = lambda *args, **kwargs: []
        kb._merge_candidates = lambda *args: []
        kb._hydrate_secure_candidates = lambda candidates, limit: []
        kb._annotate_source_profile_scores = lambda candidates: None
        kb._indexed_candidate_count = lambda count: count
        kb._build_graph_context = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("graph should not be called"))
        kb._graph_candidates = lambda *args, **kwargs: []
        kb._expand_parent_context = lambda candidates, top_k, query=None: []

        result = kb.search_with_evidence("RAG 的核心思想是什么？", graph_db=object(), top_k=1)

        self.assertEqual(result["observability"]["stage_counts"]["graph"], 0)
        self.assertEqual(result["graph_context"], [])


class _MemoryGraphStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.saved = None
        self.saved_indexes = {}

    def get_graph_snapshot(self):
        return self.snapshot

    def save_graphrag_index(self, payload, index_name="default"):
        self.saved = payload
        self.saved_indexes[index_name] = payload

    def load_graphrag_index(self, index_name="default"):
        return self.saved_indexes.get(index_name)


class _FakeGraphIndex:
    def retrieve(self, query, mode="global", limit=4):
        _ = query, mode, limit
        return {
            "communities": ["C001"],
            "graph_context": ["索引期社区摘要：检索、图谱和社区发现"],
            "evidence": [
                {
                    "candidate_id": "graphrag:C001",
                    "source_filename": "GraphRAG社区报告/C001",
                    "snippet": "摘要层",
                    "source_filenames": ["论文A.pdf"],
                    "chunk_ids": ["raw-child-1"],
                }
            ],
            "chunk_ids": ["raw-child-1"],
            "matched_entities": ["GraphRAG"],
        }


class _ScopedFakeGraphIndex:
    def __init__(self):
        self.project_ids = []

    def retrieve(self, query, mode="global", limit=4, project_id=None):
        _ = query, mode, limit
        self.project_ids.append(project_id)
        return {
            "communities": [],
            "graph_context": ["全局社区摘要"],
            "evidence": [],
            "chunk_ids": [],
            "matched_entities": [],
        }


class _RecordingSourceKnowledgeBase:
    def __init__(self):
        self.project_ids = []

    def evidence_for_chunk_ids(self, chunk_ids, *, top_k=4, query="", project_id=None):
        _ = chunk_ids, top_k, query
        self.project_ids.append(project_id)
        return {"documents": [], "metadatas": [], "evidence": []}


class _GlobalSourceKnowledgeBase:
    def __init__(self):
        self.project_ids = []

    def evidence_for_chunk_ids(self, chunk_ids, *, top_k=4, query="", project_id=None):
        _ = chunk_ids, top_k, query
        self.project_ids.append(project_id)
        return {
            "documents": ["论文2.pdf 的全局共享原始证据"],
            "metadatas": [{"source_filename": "论文2.pdf", "project_id": None}],
            "evidence": [{
                "candidate_id": "global-child",
                "source_filename": "论文2.pdf",
                "snippet": "论文2.pdf 的全局共享原始证据",
                "source_filenames": ["论文2.pdf"],
            }],
        }


class _ProjectAndGlobalSourceKnowledgeBase:
    def __init__(self):
        self.requests = []

    def evidence_for_chunk_ids(self, chunk_ids, *, top_k=4, query="", project_id=None, global_only=False):
        _ = top_k, query
        self.requests.append((list(chunk_ids), project_id, global_only))
        if global_only:
            return {
                "documents": ["全局标准原始证据"],
                "metadatas": [{"source_filename": "全局标准.pdf"}],
                "evidence": [{
                    "candidate_id": "global-child",
                    "source_filename": "全局标准.pdf",
                    "score": 0.5,
                    "snippet": "全局标准原始证据",
                }],
            }
        return {
            "documents": ["当前项目原始证据"],
            "metadatas": [{"source_filename": "项目方案.pdf", "project_id": project_id}],
            "evidence": [{
                "candidate_id": "project-child",
                "source_filename": "项目方案.pdf",
                "score": 0.4,
                "snippet": "当前项目原始证据",
            }],
        }


class _FakeSourceKnowledgeBase:
    def evidence_for_chunk_ids(self, chunk_ids, *, top_k=4, query="", project_id=None):
        _ = chunk_ids, top_k, query, project_id
        return {
            "documents": ["原始文献切片"],
            "metadatas": [{"source_filename": "论文A.pdf", "page": 2}],
            "evidence": [
                {
                    "candidate_id": "raw-child-1",
                    "source_filename": "论文A.pdf",
                    "score": 0.22,
                    "snippet": "原始文献切片",
                    "source_filenames": ["论文A.pdf"],
                }
            ],
        }




class _FakeSecureStore:
    def __init__(self, candidates):
        self.candidates = candidates

    def load_candidates(self, chunk_ids):
        allowed = set(chunk_ids)
        return [item for item in self.candidates if item.chunk_id in allowed]


def _graph_snapshot():
    groups = [
        ("检索", "向量", "嵌入"),
        ("图谱", "Leiden", "社区"),
        ("智能体", "记忆", "工具"),
    ]
    entities = []
    relationships = []
    documents = []
    for group_index, group in enumerate(groups, 1):
        chunk_ids = []
        for entity_index, name in enumerate(group, 1):
            chunk_id = f"c{group_index}{entity_index}"
            chunk_ids.append(chunk_id)
            entities.append({"name": name, "type": "概念", "description": name, "chunk_ids": [chunk_id]})
        for source, target in zip(group, group[1:]):
            relationships.append({"source": source, "target": target, "relation": "相关", "chunk_ids": chunk_ids})
        documents.append({
            "source_filename": f"论文{group_index}.pdf",
            "title": f"论文{group_index}",
            "summary": f"关于{group[0]}、{group[1]}和{group[2]}的中文研究。",
            "keywords": list(group),
            "chunk_ids": chunk_ids,
        })
    return {"entities": entities, "relationships": relationships, "documents": documents}


if __name__ == "__main__":
    unittest.main()
