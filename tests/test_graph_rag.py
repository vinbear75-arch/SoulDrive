import unittest
from types import SimpleNamespace

from core.graph_rag import (
    GraphRAGBuilder,
    GraphRAGIndex,
    _community_payload,
    _is_graph_entity_noise,
    _scope_graph_snapshot,
    leiden_partition,
    route_graph_query,
)
from core.indexer import _graph_features_enabled
from core.rag_engine import RAGEngine, _prioritize_context_evidence, gate_retrieval_context
from core.retrieval import classify_query_intent


class GraphRagTests(unittest.TestCase):
    def test_engine_forwards_document_id_scope_to_retriever(self):
        class RecordingKnowledgeBase:
            def __init__(self):
                self.kwargs = None

            def search_with_evidence(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "documents": [],
                    "metadatas": [],
                    "evidence": [],
                    "graph_context": [],
                    "matched_entities": [],
                }

        knowledge_base = RecordingKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.kb = knowledge_base

        result = engine._search_with_optional_project_focus(
            "同名文档中的证据",
            4,
            "project-a",
            document_names=["same-name.pdf"],
            document_ids=["object-b"],
        )

        self.assertEqual(result["documents"], [])
        self.assertEqual(knowledge_base.kwargs["document_names"], ["same-name.pdf"])
        self.assertEqual(knowledge_base.kwargs["document_ids"], ["object-b"])

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

    def test_query_router_prefers_local_relation_over_broad_global_marker(self):
        route = route_graph_query("请分析本项目整体方案中实体A和实体B的影响链")

        self.assertTrue(route.use_graph)
        self.assertEqual(route.mode, "local")
        self.assertIn("影响链", route.signals)

    def test_local_graph_retrieval_scores_relationship_text(self):
        payload = {
            "communities": [
                {
                    "community_id": "C002",
                    "title": "实体 A / 实体 B",
                    "entity_names": ["实体 A", "实体 B"],
                    "relationships": ["实体 A --(依赖)--> 实体 B"],
                    "source_filenames": ["关系.pdf"],
                    "report": "实体 A 与实体 B 组成一个主题。",
                    "chunk_ids": ["relation-child"],
                },
                {
                    "community_id": "C001",
                    "title": "实体 X / 实体 Y",
                    "entity_names": ["实体 X", "实体 Y"],
                    "relationships": ["实体 X --(使用)--> 实体 Y"],
                    "source_filenames": ["other.pdf"],
                    "report": "实体 X 与实体 Y 组成另一个主题。",
                    "chunk_ids": ["other-child"],
                },
            ]
        }

        result = GraphRAGIndex._retrieve_payload(
            payload,
            query="依赖",
            mode="local",
            limit=1,
            index_name="project:demo",
            shared_fallback=False,
        )

        self.assertEqual(result["communities"], ["C002"])
        self.assertEqual(result["chunk_ids"], ["relation-child"])

    def test_local_graph_retrieval_expands_one_hop_community_relationship(self):
        payload = {
            "communities": [
                {
                    "community_id": "C001",
                    "title": "Alpha",
                    "entity_names": ["Alpha"],
                    "relationships": [],
                    "source_filenames": ["a.pdf"],
                    "report": "Alpha 是起点。",
                    "chunk_ids": ["a-child"],
                },
                {
                    "community_id": "C002",
                    "title": "Beta",
                    "entity_names": ["Beta"],
                    "relationships": [],
                    "source_filenames": ["b.pdf"],
                    "report": "Beta 是下游节点。",
                    "chunk_ids": ["b-child"],
                },
                {
                    "community_id": "C003",
                    "title": "Gamma",
                    "entity_names": ["Gamma"],
                    "relationships": [],
                    "source_filenames": ["x.pdf"],
                    "report": "Gamma 与问题无关。",
                    "chunk_ids": ["x-child"],
                },
            ],
            "community_relationships": [
                {
                    "source_community_id": "C001",
                    "target_community_id": "C002",
                    "relationships": ["Alpha --(影响)--> Beta"],
                },
            ],
        }

        result = GraphRAGIndex._retrieve_payload(
            payload,
            query="Alpha 的影响链",
            mode="local",
            limit=2,
            index_name="project:demo",
            shared_fallback=False,
        )

        self.assertEqual(result["communities"], ["C001", "C002"])
        self.assertEqual(result["chunk_ids"], ["a-child", "b-child"])
        self.assertEqual(result["graph_expansion"]["mode"], "one_hop")
        self.assertEqual(result["graph_expansion"]["expanded_communities"], ["C002"])

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
            global_only=True,
            index_name="global_shared",
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
        self.assertTrue(all(item["source_document_ids"] for item in diagnostics["communities"]))
        self.assertTrue(all(item["report"] for item in diagnostics["communities"]))
        self.assertEqual(len(result["communities"]), 3)
        self.assertTrue(result["chunk_ids"])
        self.assertTrue(all(item["derived_index"] for item in result["evidence"]))
        levels = payload["levels"]
        self.assertEqual(levels[0]["community_id"], "ROOT")
        self.assertTrue(any(item["level"] == 1 and item["community_id"].startswith("T") for item in levels))
        self.assertTrue(any(item["level"] == 2 and item["community_id"].startswith("C") for item in levels))

    def test_graph_diagnostics_can_select_the_all_projects_index(self):
        store = _MemoryGraphStore(_graph_snapshot())
        GraphRAGBuilder(
            store,
            report_generator=lambda community: f"全语料社区摘要：{community['title']}",
            min_communities=3,
            max_communities=4,
            global_only=False,
            index_name="all_projects",
        ).build()

        diagnostics = GraphRAGIndex(store).diagnostics(index_name="all_projects")

        self.assertTrue(diagnostics["ready"])
        self.assertEqual(diagnostics["index_name"], "all_projects")
        self.assertIsNone(diagnostics["project_id"])

    def test_graph_diagnostics_counts_same_named_sources_by_stable_document_identity(self):
        store = _MemoryGraphStore({"documents": [], "entities": [], "relationships": []})
        store.saved_indexes["global_shared"] = {
            "schema_version": "souldrive_graphrag_index_v4",
            "communities": [{
                "community_id": "C001",
                "title": "同名来源",
                "entity_names": ["共享主题"],
                "source_filenames": ["report.pdf"],
                "source_document_ids": ["doc-a", "doc-b"],
                "report": "同名来源的社区摘要",
            }],
        }
        diagnostics = GraphRAGIndex(store).diagnostics()

        self.assertEqual(diagnostics["communities"][0]["source_count"], 2)
        self.assertEqual(
            diagnostics["communities"][0]["source_document_ids"],
            ["doc-a", "doc-b"],
        )

    def test_graph_community_summaries_keep_same_named_documents_by_chunk_provenance(self):
        payload = _community_payload(
            community_id="C001",
            entity_names=["项目 A 主题"],
            entities={"项目 A 主题": {"chunk_ids": ["chunk-a"]}},
            relationships=[],
            documents=[
                {
                    "document_id": "doc-a",
                    "source_filename": "report.pdf",
                    "summary": "项目 A 摘要",
                    "keywords": ["A"],
                    "chunk_ids": ["chunk-a"],
                    "project_id": "project-a",
                },
                {
                    "document_id": "doc-b",
                    "source_filename": "report.pdf",
                    "summary": "项目 B 摘要",
                    "keywords": ["B"],
                    "chunk_ids": ["chunk-b"],
                    "project_id": "project-b",
                },
            ],
        )

        self.assertEqual(payload["source_document_ids"], ["doc-a"])
        self.assertEqual(
            [item["document_id"] for item in payload["document_summaries"]],
            ["doc-a"],
        )

    def test_builder_rejects_unrelated_llm_community_report(self):
        store = _MemoryGraphStore(_graph_snapshot())

        payload = GraphRAGBuilder(
            store,
            report_generator=lambda _community: "今天北京天气晴朗，气温舒适。",
            min_communities=3,
            max_communities=4,
            global_only=True,
            index_name="global_shared",
        ).build()

        self.assertTrue(payload["communities"])
        self.assertTrue(all(
            item["report_method"] == "structured_template"
            for item in payload["communities"]
        ))

    def test_builder_can_use_one_authoritative_source_snapshot(self):
        store = _MemoryGraphStore(_graph_snapshot())
        source_snapshot = _graph_snapshot()
        source_snapshot["documents"] = source_snapshot["documents"][:1]

        payload = GraphRAGBuilder(
            store,
            source_snapshot=source_snapshot,
            report_generator=lambda community: f"社区摘要：{community['title']}",
            min_communities=1,
            max_communities=1,
            global_only=True,
            index_name="global_shared",
        ).build()

        self.assertEqual(payload["document_count"], 1)

    def test_unscoped_graph_retrieval_uses_shared_global_index_not_mixed_default(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b" if index == 2 else ""
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)

        # The legacy/default index is deliberately mixed: it contains both
        # project material and the shared corpus.  A global-only index is the
        # safe source for a request without a project binding.
        GraphRAGBuilder(
            store,
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

        result = GraphRAGIndex(store).retrieve(
            "请总结这些论文的共同主题",
            mode="global",
            project_id=None,
        )

        self.assertEqual(result["index_name"], "global_shared")
        self.assertTrue(result["evidence"])
        self.assertTrue(all(
            source == "论文2.pdf"
            for item in result["evidence"]
            for source in item.get("source_filenames") or []
        ))

    def test_graph_index_refuses_stale_payload_after_source_snapshot_changes(self):
        store = _MemoryGraphStore(_graph_snapshot())
        GraphRAGBuilder(
            store,
            min_communities=3,
            max_communities=4,
            global_only=True,
            index_name="global_shared",
        ).build()
        index = GraphRAGIndex(store)
        store.snapshot["documents"][0]["summary"] = "源文档已更新，旧社区摘要不再可信"

        result = index.retrieve("请总结这些论文的共同主题", mode="global", limit=4)
        diagnostics = index.diagnostics()

        self.assertEqual(result["reason"], "graphrag_index_stale")
        self.assertEqual(result["communities"], [])
        self.assertFalse(diagnostics["ready"])
        self.assertEqual(diagnostics["status"], "stale")

    def test_graph_index_refuses_stale_project_scope_after_chunk_metadata_changes(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b"
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        current_mapping = dict(chunk_project_ids)
        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: current_mapping[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in current_mapping
            },
        )
        self.assertTrue(index.diagnostics(project_id="project-a")["ready"])

        current_mapping[snapshot["documents"][0]["chunk_ids"][0]] = "project-b"
        result = index.retrieve(
            "请总结检索和向量的关系",
            mode="global",
            project_id="project-a",
        )
        diagnostics = index.diagnostics(project_id="project-a")

        self.assertEqual(result["reason"], "graphrag_index_stale")
        self.assertEqual(result["communities"], [])
        self.assertEqual(diagnostics["status"], "stale")
        self.assertEqual(diagnostics["scope_freshness"], "stale")

    def test_legacy_project_index_without_scope_metadata_fails_closed(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        snapshot["documents"][0]["project_id"] = "project-a"
        for chunk_id in snapshot["documents"][0]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-a"
        store = _MemoryGraphStore(snapshot)
        payload = GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        payload.pop("source_scope_digest", None)
        payload.pop("scope_metadata_required", None)

        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )

        diagnostics = index.diagnostics(project_id="project-a")
        result = index.retrieve(
            "请总结检索和向量的关系",
            mode="global",
            project_id="project-a",
        )

        self.assertFalse(diagnostics["ready"])
        self.assertEqual(diagnostics["status"], "unverified")
        self.assertEqual(diagnostics["reason"], "graphrag_scope_metadata_unverified")
        self.assertEqual(result["reason"], "graphrag_scope_metadata_unverified")

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
        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )
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

    def test_project_scope_drops_entity_description_without_matching_provenance(self):
        snapshot = {
            "entities": [{
                "name": "同名实体",
                "type": "概念",
                "description": "项目 B 的私有属性，不应进入项目 A 图索引。",
                "chunk_ids": ["a-1", "b-1"],
            }],
            "relationships": [],
            "documents": [
                {
                    "source_filename": "a.pdf",
                    "title": "项目 A",
                    "summary": "A",
                    "keywords": [],
                    "chunk_ids": ["a-1"],
                    "project_id": "project-a",
                },
                {
                    "source_filename": "b.pdf",
                    "title": "项目 B",
                    "summary": "B",
                    "keywords": [],
                    "chunk_ids": ["b-1"],
                    "project_id": "project-b",
                },
            ],
        }

        scoped = _scope_graph_snapshot(
            snapshot,
            max_documents=10,
            project_id="project-a",
            chunk_project_ids={"a-1": "project-a", "b-1": "project-b"},
        )

        self.assertEqual(scoped["entities"][0]["chunk_ids"], ["a-1"])
        self.assertEqual(scoped["entities"][0]["description"], "")

    def test_project_scope_selects_entity_attributes_with_chunk_provenance(self):
        snapshot = {
            "entities": [{
                "name": "同名实体",
                "type": "最后写入的类型",
                "description": "最后写入的描述",
                "chunk_ids": ["a-1", "b-1"],
                "source_attributes": [
                    {"chunk_id": "a-1", "type": "项目 A 类型", "description": "项目 A 描述"},
                    {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 描述"},
                ],
            }],
            "relationships": [],
            "documents": [
                {
                    "source_filename": "a.pdf",
                    "title": "项目 A",
                    "summary": "A",
                    "keywords": [],
                    "chunk_ids": ["a-1"],
                    "project_id": "project-a",
                },
                {
                    "source_filename": "b.pdf",
                    "title": "项目 B",
                    "summary": "B",
                    "keywords": [],
                    "chunk_ids": ["b-1"],
                    "project_id": "project-b",
                },
            ],
        }

        scoped = _scope_graph_snapshot(
            snapshot,
            max_documents=10,
            project_id="project-a",
            chunk_project_ids={"a-1": "project-a", "b-1": "project-b"},
        )

        self.assertEqual(scoped["entities"][0]["type"], "项目 A 类型")
        self.assertEqual(scoped["entities"][0]["description"], "项目 A 描述")
        self.assertEqual(scoped["entities"][0]["source_attributes"], [
            {"chunk_id": "a-1", "type": "项目 A 类型", "description": "项目 A 描述"},
        ])

    def test_project_community_report_uses_scoped_entity_attributes(self):
        snapshot = {
            "entities": [{
                "name": "同名实体",
                "type": "最后写入的类型",
                "description": "最后写入的描述",
                "chunk_ids": ["a-1", "b-1"],
                "source_attributes": [
                    {"chunk_id": "a-1", "type": "项目 A 类型", "description": "项目 A 属性"},
                    {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 属性"},
                ],
            }],
            "relationships": [],
            "documents": [
                {
                    "source_filename": "a.pdf",
                    "title": "项目 A",
                    "summary": "项目 A 原文摘要",
                    "keywords": [],
                    "chunk_ids": ["a-1"],
                    "project_id": "project-a",
                },
                {
                    "source_filename": "b.pdf",
                    "title": "项目 B",
                    "summary": "项目 B 原文摘要",
                    "keywords": [],
                    "chunk_ids": ["b-1"],
                    "project_id": "project-b",
                },
            ],
        }
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(
            store,
            project_id="project-a",
            chunk_project_ids={"a-1": "project-a", "b-1": "project-b"},
            min_communities=1,
            max_communities=1,
        ).build()

        community = payload["communities"][0]
        self.assertEqual(community["entity_attributes"], [{
            "name": "同名实体",
            "type": "项目 A 类型",
            "description": "项目 A 属性",
        }])
        self.assertIn("项目 A 属性", community["report"])
        self.assertNotIn("项目 B 属性", community["report"])

    def test_project_graph_retrieval_keeps_shared_global_communities(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        snapshot["documents"][0]["project_id"] = "project-a"
        for chunk_id in snapshot["documents"][0]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-a"
        snapshot["documents"][2]["project_id"] = "project-b"
        for chunk_id in snapshot["documents"][2]["chunk_ids"]:
            chunk_project_ids[chunk_id] = "project-b"
        for document in snapshot["documents"][1:2]:
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = ""
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

        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )
        result = index.retrieve_project_and_global(
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
            else:
                # An explicit empty project_id is the global-scope marker;
                # omitted child metadata is no longer treated as global.
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = ""
        store = _MemoryGraphStore(snapshot)

        payload = GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()
        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )

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

    def test_scoped_graph_diagnostics_fail_closed_without_scope_provider(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b"
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)

        GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        diagnostics = GraphRAGIndex(store).diagnostics()

        self.assertFalse(diagnostics["ready"])
        self.assertEqual(diagnostics["status"], "unverified")
        self.assertEqual(diagnostics["reason"], "graphrag_scope_metadata_unverified")
        self.assertEqual(diagnostics["scope_freshness"], "unverified")

    def test_graph_diagnostics_fingerprint_snapshot_failure_without_raw_error(self):
        snapshot = _graph_snapshot()

        class BrokenSnapshotStore(_MemoryGraphStore):
            def get_graph_snapshot(self):
                raise RuntimeError("private graph path secret=graph-token")

        store = BrokenSnapshotStore(snapshot)
        GraphRAGBuilder(
            store,
            global_only=True,
            index_name="global_shared",
            min_communities=1,
            max_communities=4,
            source_snapshot=snapshot,
        ).build()

        diagnostics = GraphRAGIndex(store).diagnostics()

        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertNotIn("error", diagnostics)
        self.assertEqual(diagnostics["error_type"], "RuntimeError")
        self.assertEqual(diagnostics["error_chars"], len("private graph path secret=graph-token"))
        self.assertRegex(diagnostics["error_digest"], r"^[0-9a-f]{16}$")
        self.assertNotIn("graph-token", str(diagnostics))

    def test_graph_diagnostics_fingerprint_scope_provider_failure_without_raw_error(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {
            chunk_id: {"project_id": "project-a"}
            for document in snapshot["documents"]
            for chunk_id in document["chunk_ids"]
        }
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            project_id="project-a",
            index_name="project:project-a",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
            source_snapshot=snapshot,
        ).build()

        def broken_scope_provider(_chunk_ids):
            raise RuntimeError("scope backend secret=scope-token")

        diagnostics = GraphRAGIndex(
            store,
            scope_metadata_provider=broken_scope_provider,
        ).diagnostics(project_id="project-a")

        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertNotIn("error", diagnostics)
        self.assertEqual(diagnostics["error_type"], "RuntimeError")
        self.assertRegex(diagnostics["error_digest"], r"^[0-9a-f]{16}$")
        self.assertNotIn("scope-token", str(diagnostics))

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

    def test_shared_global_scope_drops_chunks_missing_from_partial_scope_manifest(self):
        snapshot = {
            "entities": [],
            "relationships": [],
            "documents": [
                {
                    "source_filename": "明确全局.pdf",
                    "title": "明确全局",
                    "summary": "有明确全局归属",
                    "keywords": [],
                    "chunk_ids": ["global-1"],
                    "project_id": "legacy-project",
                },
                {
                    "source_filename": "缺少归属.pdf",
                    "title": "缺少归属",
                    "summary": "没有当前 chunk 归属元数据",
                    "keywords": [],
                    "chunk_ids": ["unknown-1"],
                    "project_id": "",
                },
                {
                    "source_filename": "项目私有.pdf",
                    "title": "项目私有",
                    "summary": "不应进入共享索引",
                    "keywords": [],
                    "chunk_ids": ["project-1"],
                    "project_id": "project-a",
                },
            ],
        }

        scoped = _scope_graph_snapshot(
            snapshot,
            max_documents=10,
            global_only=True,
            chunk_project_ids={
                "global-1": "",
                "project-1": "project-a",
            },
        )

        self.assertEqual(
            [item["source_filename"] for item in scoped["documents"]],
            ["明确全局.pdf"],
        )

    def test_cross_project_graph_retrieval_uses_explicit_all_projects_index(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b" if index == 2 else ""
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)

        GraphRAGBuilder(
            store,
            global_only=False,
            index_name="all_projects",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        index = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )
        result = index.retrieve_all_projects(
            "请分析跨项目检索和图谱的共同思路",
            mode="global",
            limit=4,
        )

        self.assertEqual(result["index_name"], "all_projects")
        self.assertTrue(result["evidence"])
        self.assertTrue({
            source
            for item in result["evidence"]
            for source in item.get("source_filenames") or []
        } & {"论文1.pdf", "论文3.pdf"})

    def test_all_projects_graph_index_requires_scope_metadata_provider(self):
        snapshot = _graph_snapshot()
        chunk_project_ids = {}
        for index, document in enumerate(snapshot["documents"]):
            project_id = "project-a" if index == 0 else "project-b" if index == 2 else ""
            document["project_id"] = project_id
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = project_id
        store = _MemoryGraphStore(snapshot)
        GraphRAGBuilder(
            store,
            index_name="all_projects",
            chunk_project_ids=chunk_project_ids,
            min_communities=1,
            max_communities=4,
        ).build()

        result = GraphRAGIndex(store).retrieve_all_projects(
            "请分析跨项目检索和图谱的共同思路",
            mode="global",
            limit=4,
        )

        self.assertEqual(result["reason"], "graphrag_scope_metadata_unverified")
        self.assertEqual(result["evidence"], [])

    def test_missing_all_projects_graph_index_reports_explicit_scope_gap(self):
        result = GraphRAGIndex(_MemoryGraphStore(_graph_snapshot())).retrieve_all_projects(
            "请分析跨项目检索和图谱的共同思路",
            mode="global",
            limit=4,
        )

        self.assertEqual(result["reason"], "cross_project_graph_index_not_built")
        self.assertEqual(result["evidence"], [])

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
            else:
                # An explicit empty project_id is the global-scope marker;
                # omitted child metadata is no longer treated as global.
                for chunk_id in document["chunk_ids"]:
                    chunk_project_ids[chunk_id] = ""
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
        for document in snapshot["documents"][1:2]:
            for chunk_id in document["chunk_ids"]:
                chunk_project_ids[chunk_id] = ""
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
        engine.graph_rag = GraphRAGIndex(
            store,
            scope_metadata_provider=lambda chunk_ids: {
                chunk_id: chunk_project_ids[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in chunk_project_ids
            },
        )
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
        self.assertEqual(
            result["observability"]["graph_rag"]["graph_expansion"]["expanded_count"],
            1,
        )

    def test_graph_observability_counts_same_named_scoped_sources_by_identity(self):
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = SimpleNamespace(
            retrieve=lambda query, mode="global", limit=4, project_id=None: {
                "communities": ["C001"],
                "graph_context": ["同名资产的图谱导航"],
                "evidence": [],
                "chunk_ids": ["child-a", "child-b"],
                "matched_entities": [],
            }
        )
        engine.kb = SimpleNamespace(
            evidence_for_chunk_ids=lambda chunk_ids, **kwargs: {
                "documents": ["项目A原始证据", "项目B原始证据"],
                "metadatas": [
                    {
                        "source_filename": "report.pdf",
                        "document_id": "doc-a",
                        "project_id": "project-a",
                    },
                    {
                        "source_filename": "report.pdf",
                        "document_id": "doc-b",
                        "project_id": "project-b",
                    },
                ],
                "evidence": [
                    {
                        "candidate_id": "child-a",
                        "source_filename": "report.pdf",
                        "document_id": "doc-a",
                        "project_id": "project-a",
                        "score": 0.3,
                    },
                    {
                        "candidate_id": "child-b",
                        "source_filename": "report.pdf",
                        "document_id": "doc-b",
                        "project_id": "project-b",
                        "score": 0.29,
                    },
                ],
            }
        )

        result = engine._apply_graph_rag_route(
            {
                "documents": [],
                "metadatas": [],
                "evidence": [],
                "graph_context": [],
                "matched_entities": [],
                "retrieval_mode": "hybrid",
            },
            route_graph_query("总结这些项目的共同安全思路"),
            "总结这些项目的共同安全思路",
        )

        self.assertEqual(result["observability"]["graph_rag"]["source_count"], 2)

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

    def test_graph_merge_treats_non_finite_score_as_zero(self):
        documents, _metadatas, evidence = _prioritize_context_evidence(
            ["异常分数证据", "正常高分证据"],
            [{"source_filename": "nan.pdf"}, {"source_filename": "finite.pdf"}],
            [
                {"candidate_id": "nan", "score": float("nan")},
                {"candidate_id": "finite", "score": 0.2},
            ],
        )

        assert documents == ["正常高分证据", "异常分数证据"]
        assert [item["candidate_id"] for item in evidence] == ["finite", "nan"]

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

        self.assertEqual(graph_index.cross_project_calls, 1)
        self.assertEqual(graph_index.project_ids, [])
        self.assertEqual(source_loader.project_ids, [None])
        self.assertEqual(source_loader.requests[-1][1:], (None, False))

    def test_engine_uses_global_graphrag_index_for_cross_project_local_relation_query(self):
        graph_index = _ScopedFakeGraphIndex()
        source_loader = _RecordingSourceKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = graph_index
        engine.kb = source_loader

        query = "跨项目分析实体A和实体B的影响链"
        route = route_graph_query(query)
        self.assertEqual(route.mode, "local")
        self.assertTrue(classify_query_intent(query).explicit_global_scope)
        engine._apply_graph_rag_route(
            {
                "documents": [],
                "metadatas": [],
                "evidence": [],
                "graph_context": [],
                "matched_entities": [],
                "retrieval_mode": "hybrid",
            },
            route,
            query,
            project_id="project-current",
        )

        self.assertEqual(graph_index.cross_project_calls, 1)
        self.assertEqual(graph_index.project_ids, [])
        self.assertEqual(source_loader.project_ids, [None])
        self.assertEqual(source_loader.requests[-1][1:], (None, False))

    def test_engine_keeps_unbound_graphrag_source_evidence_global_only(self):
        graph_index = _UnboundGraphIndex()
        source_loader = _RecordingSourceKnowledgeBase()
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = graph_index
        engine.kb = source_loader

        query = "请总结图谱中的零信任主题"
        result = engine._apply_graph_rag_route(
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
            project_id=None,
        )

        self.assertEqual(source_loader.requests[-1][1:], (None, True))
        self.assertTrue(result["observability"]["graph_rag"]["global_only"])

    def test_unbound_graphrag_backend_without_scope_contract_fails_closed(self):
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = _LegacyUnscopedGraphIndex()
        engine.kb = _RecordingSourceKnowledgeBase()
        query = "请总结图谱中的零信任主题"

        result = engine._apply_graph_rag_route(
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
            project_id=None,
        )

        self.assertEqual(result["graph_context"], [])
        self.assertEqual(result["evidence"], [])
        self.assertFalse(result["observability"]["graph_rag"]["enabled"])

    def test_unbound_source_loader_without_scope_contract_fails_closed(self):
        engine = RAGEngine.__new__(RAGEngine)
        engine.graph_rag = _UnboundGraphIndex()
        engine.kb = _LegacyUnscopedSourceKnowledgeBase()
        query = "请总结图谱中的零信任主题"

        result = engine._apply_graph_rag_route(
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
            project_id=None,
        )

        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["observability"]["graph_rag"]["source_evidence_count"], 0)

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
    def retrieve(self, query, mode="global", limit=4, project_id=None):
        _ = query, mode, limit, project_id
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
            "graph_expansion": {
                "mode": "one_hop",
                "expanded_communities": ["C002"],
                "expanded_count": 1,
            },
        }


class _ScopedFakeGraphIndex:
    def __init__(self):
        self.project_ids = []
        self.cross_project_calls = 0

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

    def retrieve_all_projects(self, query, mode="global", limit=4):
        _ = query, mode, limit
        self.cross_project_calls += 1
        return {
            "communities": [],
            "graph_context": ["跨项目社区摘要"],
            "evidence": [],
            "chunk_ids": [],
            "matched_entities": [],
            "index_name": "all_projects",
        }


class _UnboundGraphIndex:
    def retrieve(self, query, mode="global", limit=4, project_id=None):
        _ = query, mode, limit
        if project_id is not None:
            raise AssertionError("unbound retrieval must not receive a project")
        return {
            "communities": ["global-community"],
            "graph_context": ["全局社区摘要"],
            "evidence": [],
            "chunk_ids": ["global-child"],
            "matched_entities": [],
            "index_name": "global_shared",
        }


class _LegacyUnscopedGraphIndex:
    def retrieve(self, query, mode="global", limit=4):
        _ = query, mode, limit
        return {
            "communities": ["mixed-community"],
            "graph_context": ["混合项目的未隔离社区摘要"],
            "evidence": [],
            "chunk_ids": ["project-b-child"],
            "matched_entities": [],
            "index_name": "default",
        }


class _RecordingSourceKnowledgeBase:
    def __init__(self):
        self.project_ids = []
        self.requests = []

    def evidence_for_chunk_ids(
        self,
        chunk_ids,
        *,
        top_k=4,
        query="",
        project_id=None,
        global_only=False,
    ):
        self.requests.append((list(chunk_ids), project_id, global_only))
        _ = top_k, query
        self.project_ids.append(project_id)
        return {"documents": [], "metadatas": [], "evidence": []}


class _GlobalSourceKnowledgeBase:
    def __init__(self):
        self.project_ids = []

    def evidence_for_chunk_ids(
        self,
        chunk_ids,
        *,
        top_k=4,
        query="",
        project_id=None,
        global_only=False,
    ):
        _ = chunk_ids, top_k, query, global_only
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


class _LegacyUnscopedSourceKnowledgeBase:
    def evidence_for_chunk_ids(self, chunk_ids, *, top_k=4, query="", project_id=None):
        _ = chunk_ids, top_k, query, project_id
        return {
            "documents": ["另一个项目的未隔离原始证据"],
            "metadatas": [{"source_filename": "project-b.pdf", "project_id": "project-b"}],
            "evidence": [{
                "candidate_id": "project-b-child",
                "source_filename": "project-b.pdf",
                "project_id": "project-b",
                "score": 0.9,
                "snippet": "另一个项目的未隔离原始证据",
            }],
        }


class _FakeSourceKnowledgeBase:
    def evidence_for_chunk_ids(
        self,
        chunk_ids,
        *,
        top_k=4,
        query="",
        project_id=None,
        global_only=False,
    ):
        _ = chunk_ids, top_k, query, project_id, global_only
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
