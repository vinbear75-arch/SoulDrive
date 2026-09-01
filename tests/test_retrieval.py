import unittest
from unittest.mock import patch

from core.knowledge_base import LocalKnowledgeBase
from core.retrieval import (
    SearchCandidate,
    _query_source_hints,
    _select_diverse_sources,
    _select_source_covered_candidates,
    _section_route_match,
    _source_match_score,
    _structure_match_score,
    build_citations,
    classify_query_intent,
    rank_hybrid_candidates,
)


class RetrievalTests(unittest.TestCase):
    def test_query_intent_distinguishes_formal_document_question_types(self):
        clause_intent = classify_query_intent("《数据安全管理办法》第三条规定了什么？")
        table_intent = classify_query_intent("验收指标值、完成比例和期限分别是多少？")
        summary_intent = classify_query_intent("概述《项目实施方案》的核心要点")
        comparison_intent = classify_query_intent("对比《甲方案》和《乙方案》的技术参数")
        document_number_intent = classify_query_intent("某府〔2026〕12号第一条规定了什么？")

        self.assertEqual(clause_intent.name, "clause_lookup")
        self.assertEqual(clause_intent.clause_references, ("第三条",))
        section_intent = classify_query_intent(
            "请定位资料中关于“2.3 问答系统”的关键原文证据。"
        )
        self.assertEqual(table_intent.name, "table_lookup")
        self.assertTrue(table_intent.prefers_table)
        self.assertTrue(classify_query_intent("论文中的架构图说明了什么？").prefers_visual_asset)
        self.assertTrue(classify_query_intent("请看这个界面示意图的按钮布局").prefers_visual_asset)
        self.assertTrue(classify_query_intent("请说明指标图里 target_binding_accuracy 的变化").prefers_visual_asset)
        self.assertEqual(summary_intent.name, "broad_summary")
        self.assertTrue(summary_intent.explicit_source)
        self.assertEqual(comparison_intent.name, "multi_source_comparison")
        self.assertTrue(comparison_intent.prefers_table)
        self.assertEqual(document_number_intent.name, "clause_lookup")
        self.assertTrue(document_number_intent.explicit_source)
        self.assertEqual(section_intent.name, "clause_lookup")
        self.assertEqual(section_intent.clause_references, ("23",))

    def test_query_intent_requires_explicit_global_scope_for_cross_project_search(self):
        project_summary = classify_query_intent("概述当前项目方案的核心要点")
        cross_project = classify_query_intent("比较两个项目的 zero trust 方案")

        self.assertTrue(project_summary.broad_scope)
        self.assertFalse(project_summary.explicit_global_scope)
        self.assertTrue(cross_project.explicit_global_scope)

    def test_hybrid_rank_rewards_keyword_and_graph_hits(self):
        candidates = [
            SearchCandidate(
                id="dense-only",
                content="This paper discusses generic transformer pretraining.",
                metadata={"source_filename": "generic.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="graph-rag",
                content="GraphRAG builds graph communities for paper summarization and local search.",
                metadata={"source_filename": "graphrag.pdf"},
                dense_rank=8,
                graph_score=3,
            ),
        ]

        ranked = rank_hybrid_candidates("GraphRAG paper local search", candidates, top_k=2)

        self.assertEqual(ranked[0].id, "graph-rag")
        self.assertGreater(ranked[0].keyword_score, 0)
        self.assertIn("final", ranked[0].score_breakdown)

    def test_hybrid_rank_fuses_keyword_hits_from_query_variants(self):
        candidates = [
            SearchCandidate(
                id="base-only",
                content="这是一个通用的知识库问答介绍。",
                metadata={"source_filename": "base.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="alias-hit",
                content="检索增强生成 RAG retrieval augmented generation 的核心流程。",
                metadata={"source_filename": "rag.pdf"},
                dense_rank=8,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "检索增强生成的核心流程是什么？",
            candidates,
            top_k=2,
            query_variants=[
                "检索增强生成的核心流程是什么？",
                "RAG retrieval augmented generation core workflow",
            ],
        )

        self.assertEqual(ranked[0].id, "alias-hit")
        self.assertIsNotNone(candidates[1].sparse_rank)
        self.assertGreater(candidates[1].keyword_score, 0)

    def test_hybrid_rank_uses_prefilled_reranker_score(self):
        candidates = [
            SearchCandidate(
                id="lexical-only",
                content="This paper discusses generic transformer pretraining.",
                metadata={"source_filename": "generic.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="reranked-best",
                content="GraphRAG builds graph communities for paper summarization and local search.",
                metadata={"source_filename": "graphrag.pdf"},
                dense_rank=8,
                rerank_score=0.95,
            ),
        ]

        ranked = rank_hybrid_candidates("GraphRAG paper local search", candidates, top_k=2)

        self.assertEqual(ranked[0].id, "reranked-best")
        self.assertGreaterEqual(ranked[0].rerank_score, 0.95)
        self.assertIn("semantic_rerank", ranked[0].score_breakdown)

    def test_hybrid_rank_uses_project_focus_as_a_preference_not_a_filter(self):
        candidates = [
            SearchCandidate(
                id="current-project",
                content="RAG retrieves external knowledge before generation.",
                metadata={"source_filename": "current.pdf", "_knowledge_weight": 1.12, "_project_focus_weight": 1.12},
                dense_rank=2,
            ),
            SearchCandidate(
                id="other-project",
                content="RAG retrieves external knowledge before generation.",
                metadata={"source_filename": "global.pdf", "_knowledge_weight": 1.0, "_project_focus_weight": 1.0},
                dense_rank=1,
            ),
        ]

        ranked = rank_hybrid_candidates("RAG retrieves external knowledge", candidates, top_k=2)

        self.assertEqual([candidate.id for candidate in ranked], ["current-project", "other-project"])
        self.assertEqual(ranked[0].score_breakdown["project_focus"], 1.12)

    def test_build_citations_exposes_source_and_page(self):
        candidate = SearchCandidate(
            id="doc_0",
            content="A paper excerpt about retrieval augmented generation.",
            metadata={
                "source_filename": "rag.pdf",
                "source_path": "E:/rag.pdf",
                "document_title": "企业知识库建设方案",
                "document_number": "企标〔2026〕1号",
                "page": 3,
                "chunk_index": 2,
                "source_locator": "rows:2-5",
                "Header 4": "3.2.1 深层方法",
                "Header 2": "Method",
                "clause_id": "3.2",
                "content_kind": "clause",
            },
            final_score=0.5,
        )

        citations = build_citations([candidate])

        self.assertEqual(citations[0]["id"], "E1")
        self.assertEqual(citations[0]["source_filename"], "rag.pdf")
        self.assertNotIn("source_path", citations[0])
        self.assertEqual(citations[0]["page_label"], "3")
        self.assertEqual(citations[0]["source_locator"], "rows:2-5")
        self.assertEqual(citations[0]["source_locator_kind"], None)
        self.assertEqual(citations[0]["section"], "3.2.1 深层方法")
        self.assertEqual(citations[0]["section_path"], ["Method", "3.2.1 深层方法"])
        self.assertIsNone(citations[0]["section_route_mode"])
        self.assertIsNone(citations[0]["section_route_node"])
        self.assertEqual(citations[0]["document_title"], "企业知识库建设方案")
        self.assertEqual(citations[0]["document_number"], "企标〔2026〕1号")
        self.assertEqual(citations[0]["clause_id"], "3.2")
        self.assertEqual(citations[0]["content_kind"], "clause")

    def test_build_citations_prefers_expanded_source_context_over_child_excerpt(self):
        candidate = SearchCandidate(
            id="doc_0",
            content="## 1.2 知识迁移\n\n大语言模型依赖上下文统计关系，不能直接内化知识。",
            metadata={
                "source_filename": "paper.pdf",
                "child_excerpt": "## 1.2 知识迁移",
                "page": 2,
            },
            final_score=0.5,
        )

        citations = build_citations([candidate])

        self.assertIn("不能直接内化知识", citations[0]["snippet"])
        self.assertNotEqual(citations[0]["snippet"], "## 1.2 知识迁移")

    def test_build_citations_exposes_query_support_without_claiming_probability(self):
        candidate = SearchCandidate(
            id="rag-child",
            content="RAG 的核心流程包括检索、增强上下文和受控生成。",
            metadata={"source_filename": "rag.pdf", "section": "RAG 原理"},
            final_score=0.5,
        )

        citation = build_citations([candidate], query="RAG 的核心流程是什么？")[0]

        self.assertGreater(citation["support_score"], 0.0)
        self.assertIn("核心流程", citation["support_summary"])
        self.assertIn("rag", citation["support_terms"])
        self.assertEqual(citation["support_method"], "extractive_query_overlap_v0.1")

    def test_build_citations_exposes_linked_visual_assets(self):
        candidate = SearchCandidate(
            id="visual-child",
            content="[DOCUMENT] Transformer 论文\n[VISUAL_ASSETS]\nasset=figure-p2-1; page=2; caption=Decoder architecture diagram",
            metadata={
                "source_filename": "paper.pdf",
                "image_count": 1,
                "image_metadata_json": '[{"asset_id":"figure-p2-1","page":2,"caption":"Decoder architecture diagram","source_locator":"page:2/figure:1"}]',
            },
            final_score=0.5,
        )

        citation = build_citations([candidate], query="架构图说明了什么？")[0]

        self.assertEqual(citation["visual_asset_count"], 1)
        self.assertEqual(citation["visual_asset_captions"], ["Decoder architecture diagram"])
        self.assertEqual(citation["visual_assets"][0]["source_locator"], "page:2/figure:1")

    def test_build_citations_falls_back_to_document_title_for_legacy_indexes(self):
        candidate = SearchCandidate(
            id="legacy-child",
            content="企业技术方案正文",
            metadata={"document_title": "企业技术方案", "page": 1},
            final_score=0.5,
        )

        citations = build_citations([candidate])

        self.assertEqual(citations[0]["source_filename"], "企业技术方案")

    def test_source_match_uses_formal_title_and_document_number_not_only_filename(self):
        metadata = {
            "source_filename": "upload_20260728_001.pdf",
            "document_title": "数据安全管理办法",
            "document_number": "某府〔2026〕12号",
        }

        self.assertEqual(_source_match_score("《数据安全管理办法》有哪些要求？", metadata), 1.0)
        self.assertEqual(_source_match_score("某府〔2026〕12号第一条是什么？", metadata), 1.0)

    def test_source_match_does_not_cross_latin_token_boundaries(self):
        metadata = {
            "source_filename": "RAGBench - Explainable Benchmark for RAG Systems.pdf",
            "document_number": "GBench",
        }

        self.assertLess(
            _source_match_score("RAG benchmark 的评测方法", metadata),
            1.0,
        )
        self.assertEqual(
            _source_match_score("RAGBench 的评测方法", metadata),
            1.0,
        )

    def test_merge_candidates_preserves_distinct_children_until_rerank(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)

        merged = kb._merge_candidates(
            [
                SearchCandidate(
                    id="parent-1_child_0",
                    content="dense child excerpt",
                    metadata={
                        "parent_id": "parent-1",
                        "child_excerpt": "dense child excerpt",
                        "source_filename": "paper.pdf",
                    },
                    dense_rank=5,
                    vector_distance=0.3,
                )
            ],
            [
                SearchCandidate(
                    id="parent-1_child_1",
                    content="keyword child excerpt",
                    metadata={
                        "parent_id": "parent-1",
                        "child_excerpt": "keyword child excerpt",
                        "source_filename": "paper.pdf",
                    },
                    sparse_rank=2,
                    keyword_score=3.5,
                )
            ],
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].id, "parent-1_child_0")
        self.assertEqual(merged[0].dense_rank, 5)
        self.assertEqual(merged[1].sparse_rank, 2)
        self.assertEqual(merged[1].keyword_score, 3.5)
        self.assertEqual(merged[1].metadata["child_excerpt"], "keyword child excerpt")

    def test_source_title_candidates_are_created_from_metadata_matches(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.collection = _FakeCollection(
            {
                "ids": ["child-1", "child-2"],
                "documents": ["generic private deployment content", "unrelated body without query tokens"],
                "metadatas": [
                    {"source_filename": "generic.pdf", "parent_id": "p-generic"},
                    {"source_filename": "大模型落地路线图研究报告.pdf", "parent_id": "p-roadmap"},
                ],
            }
        )
        kb._source_candidate_cache = None

        candidates = kb._source_title_candidates("在《大模型落地路线图研究报告》中，RAG 的核心论述是什么？", limit=3)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, "child-2")
        self.assertEqual(candidates[0].metadata["source_filename"], "大模型落地路线图研究报告.pdf")
        self.assertEqual(candidates[0].metadata["parent_id"], "p-roadmap")

    def test_source_title_candidates_keep_distinct_sources_for_multi_title_query(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.collection = _FakeCollection(
            {
                "ids": ["cn-1", "cn-2", "en-1"],
                "documents": ["first Chinese parent", "second Chinese parent", "English survey parent"],
                "metadatas": [
                    {"source_filename": "基于大模型和RAG的知识库问答系统.pdf", "parent_id": "cn-p1"},
                    {"source_filename": "基于大模型和RAG的知识库问答系统.pdf", "parent_id": "cn-p2"},
                    {"source_filename": "Retrieval-Augmented Generation for Large Language Models - A Survey.pdf", "parent_id": "en-p1"},
                ],
            }
        )
        kb._source_candidate_cache = None

        candidates = kb._source_title_candidates(
            "请对比《Retrieval-Augmented Generation for Large Language Models: A Survey》和《基于大模型和RAG的知识库问答系统》在 retrieval / 检索问题上的证据",
            limit=2,
        )

        self.assertEqual(
            {candidate.metadata["source_filename"] for candidate in candidates},
            {
                "基于大模型和RAG的知识库问答系统.pdf",
                "Retrieval-Augmented Generation for Large Language Models - A Survey.pdf",
            },
        )

    def test_hybrid_rank_promotes_explicit_source_title_match(self):
        candidates = [
            SearchCandidate(
                id="generic-private-rag",
                content="私有化 权限 本地化 知识库 问答 RAG 检索增强生成",
                metadata={"source_filename": "知识库问答场景中大语言模型私有化研究与应用实践.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="maxkb-paper",
                content="系统介绍本地化知识库问答流程。",
                metadata={"source_filename": "基于大模型和RAG的知识库问答系统.pdf"},
                dense_rank=18,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "关于《基于大模型和RAG的知识库问答系统》里的私有化 权限 本地化问题",
            candidates,
            top_k=2,
        )

        self.assertEqual([candidate.id for candidate in ranked], ["maxkb-paper"])

    def test_explicit_source_does_not_fill_evidence_from_other_documents(self):
        candidates = [
            SearchCandidate(
                id="target",
                content="目标文档正文。",
                metadata={"source_filename": "target.pdf"},
                dense_rank=8,
            ),
            SearchCandidate(
                id="noise",
                content="其他文档正文。",
                metadata={"source_filename": "noise.pdf"},
                dense_rank=1,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "请只依据《target.pdf》回答这个问题",
            candidates,
            top_k=2,
        )

        self.assertEqual([candidate.id for candidate in ranked], ["target"])

    def test_hybrid_rank_promotes_explicit_document_number_match(self):
        candidates = [
            SearchCandidate(
                id="generic-policy",
                content="第一条规定数据分类分级和访问控制要求。",
                metadata={"source_filename": "其他管理办法.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="numbered-policy",
                content="第一条规定适用范围和责任边界。",
                metadata={
                    "source_filename": "上传文件.pdf",
                    "document_number": "某府〔2026〕12号",
                },
                dense_rank=12,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "某府〔2026〕12号第一条规定了什么？",
            candidates,
            top_k=1,
        )

        self.assertEqual(ranked[0].id, "numbered-policy")
        self.assertEqual(ranked[0].score_breakdown["source"], 1.0)

    def test_hybrid_rank_promotes_exact_clause_for_clause_lookup(self):
        candidates = [
            SearchCandidate(
                id="generic-prose",
                content="本办法介绍数据安全管理要求和适用范围。",
                metadata={
                    "source_filename": "数据安全管理办法.pdf",
                    "content_kind": "prose",
                },
                dense_rank=1,
            ),
            SearchCandidate(
                id="exact-clause",
                content="第三条 企业应当建立数据分类分级与访问控制制度。",
                metadata={
                    "source_filename": "数据安全管理办法.pdf",
                    "content_kind": "clause",
                    "clause_id": "第三条",
                },
                dense_rank=8,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "《数据安全管理办法》第三条规定了什么？",
            candidates,
            top_k=2,
        )

        self.assertEqual(ranked[0].id, "exact-clause")
        self.assertEqual(ranked[0].score_breakdown["structure"], 1.0)

    def test_hybrid_rank_matches_quoted_section_in_child_excerpt(self):
        candidates = [
            SearchCandidate(
                id="generic",
                content="RAG 的一般介绍。",
                metadata={"section": "2 基本原理与系统架构"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="section-hit",
                content="RAG 范式的核心论述。",
                metadata={
                    "section": "2 基本原理与系统架构",
                    "child_excerpt": "## 2.1 RAG 范式\nRAG 是一种融合信息检索与文本生成的框架。",
                },
                dense_rank=8,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "请概括资料中“2.1 RAG 范式”的核心论述。",
            candidates,
            top_k=1,
        )

        self.assertEqual(ranked[0].id, "section-hit")
        self.assertEqual(ranked[0].score_breakdown["structure"], 1.0)

    def test_structure_match_rewards_multi_topic_coverage(self):
        query = "请比较“检索”和“生成”的核心差异"
        intent = classify_query_intent(query)
        partial = SearchCandidate(
            id="partial-section",
            content="检索阶段的说明。",
            metadata={"section": "检索"},
        )
        complete = SearchCandidate(
            id="complete-section",
            content="检索与生成的协同说明。",
            metadata={"section_path": ["检索", "生成"]},
        )

        self.assertEqual(_structure_match_score(query, intent, partial), 0.5)
        self.assertEqual(_structure_match_score(query, intent, complete), 1.0)

    def test_section_route_distinguishes_exact_node_from_excerpt_fallback(self):
        query = "请定位“2.1 RAG 范式”的原文"
        exact = _section_route_match(
            query,
            {"section_path": ["2 基本原理", "2.1 RAG 范式"]},
            "正文未重复章节名。",
        )
        fallback = _section_route_match(
            query,
            {"section_path": ["2 基本原理"], "child_excerpt": "## 2.1 RAG 范式\n正文"},
            "",
        )

        self.assertEqual(exact["mode"], "exact_node")
        self.assertEqual(exact["node_paths"], ("2 基本原理 > 2.1 RAG 范式",))
        self.assertEqual(exact["coverage"], 1.0)
        self.assertEqual(fallback["mode"], "heading_fallback")
        self.assertEqual(fallback["coverage"], 1.0)

    def test_hybrid_rank_promotes_table_for_metric_lookup(self):
        candidates = [
            SearchCandidate(
                id="prose",
                content="本章说明项目验收指标和完成期限。",
                metadata={"source_filename": "方案.pdf", "content_kind": "prose"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="table",
                content="| 验收指标 | 指标值 | 完成期限 |\n| --- | --- | --- |\n| 可用率 | 99.9% | 12月 |",
                metadata={"source_filename": "方案.pdf", "content_kind": "table", "table_count": 1},
                dense_rank=7,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "验收指标值和完成期限分别是多少？",
            candidates,
            top_k=2,
        )

        self.assertEqual(ranked[0].id, "table")
        self.assertGreater(ranked[0].score_breakdown["structure"], 0.9)

    def test_metric_queries_are_classified_as_table_lookups(self):
        for query in (
            "准确率是多少？",
            "召回率和精确率分别是多少？",
            "P95 延迟是多少？",
            "吞吐量指标是多少？",
        ):
            intent = classify_query_intent(query)
            self.assertEqual(intent.name, "table_lookup")
            self.assertTrue(intent.prefers_table)

    def test_hybrid_rank_defers_overlapping_formal_windows(self):
        diagnostics = {}
        candidates = [
            SearchCandidate(
                id="window-0",
                content="第一段安全目标。第二段访问控制。",
                metadata={
                    "source_filename": "制度.pdf",
                    "parent_id": "parent-1",
                    "child_id": "window-0",
                    "child_start_char": 0,
                    "child_end_char": 200,
                },
                dense_rank=1,
            ),
            SearchCandidate(
                id="window-1",
                content="第二段访问控制。第三段审计要求。",
                metadata={
                    "source_filename": "制度.pdf",
                    "parent_id": "parent-1",
                    "child_id": "window-1",
                    "child_start_char": 100,
                    "child_end_char": 300,
                },
                dense_rank=2,
            ),
            SearchCandidate(
                id="window-2",
                content="第四段应急响应。第五段恢复要求。",
                metadata={
                    "source_filename": "制度.pdf",
                    "parent_id": "parent-1",
                    "child_id": "window-2",
                    "child_start_char": 320,
                    "child_end_char": 520,
                },
                dense_rank=3,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "制度安全要求",
            candidates,
            top_k=2,
            diagnostics=diagnostics,
        )

        self.assertEqual([candidate.id for candidate in ranked], ["window-0", "window-2"])
        self.assertEqual(diagnostics["redundant_deferred_count"], 1)

    def test_parent_expansion_uses_local_clause_window_instead_of_full_parent(self):
        paragraphs = [
            "第一条 总则。",
            "第二条 适用范围。",
            "第三条 企业应建立数据分类分级制度。",
            "第四条 企业应配置访问控制措施。",
            "第五条 企业应保存审计记录。",
            "第六条 附则。",
        ]
        parent_content = "\n\n".join(paragraphs)
        target_start = parent_content.index(paragraphs[2])
        target_end = target_start + len(paragraphs[2])
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore(parent_content)
        candidate = SearchCandidate(
            id="child-2",
            content=paragraphs[2],
            metadata={
                "source_filename": "制度.pdf",
                "parent_id": "parent-1",
                "child_start_char": target_start,
                "child_end_char": target_end,
                "content_kind": "clause",
            },
        )

        expanded = kb._expand_parent_context(
            [candidate],
            top_k=1,
            query="第三条规定了什么？",
        )

        self.assertIn("第三条", expanded[0].content)
        self.assertIn("第二条", expanded[0].content)
        self.assertIn("第四条", expanded[0].content)
        self.assertNotIn("第一条", expanded[0].content)
        self.assertNotIn("第五条", expanded[0].content)
        self.assertEqual(
            expanded[0].metadata["context_expansion_mode"],
            "local_parent_window",
        )

    def test_parent_expansion_orders_reserved_comparison_sources_by_score(self):
        kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
        kb.secure_store = None
        kb.parent_store = _FakeParentStore("比较证据正文。")
        alpha = SearchCandidate(
            id="alpha",
            content="Alpha 证据。",
            metadata={"source_filename": "alpha.pdf", "parent_id": "alpha-parent"},
            final_score=0.25,
        )
        beta = SearchCandidate(
            id="beta",
            content="Beta 证据。",
            metadata={"source_filename": "beta.pdf", "parent_id": "beta-parent"},
            final_score=0.75,
        )

        expanded = kb._expand_parent_context(
            [alpha, beta],
            top_k=2,
            query="请比较Alpha与Beta两类资料的差异",
        )

        self.assertEqual([candidate.id for candidate in expanded], ["beta", "alpha"])

    def test_hybrid_rank_keeps_competitive_cross_document_sources(self):
        candidates = [
            SearchCandidate(
                id="alpha-1",
                content="alpha policy retrieval augmented generation evidence",
                metadata={"source_filename": "alpha.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="alpha-2",
                content="alpha policy retrieval augmented generation evidence detail",
                metadata={"source_filename": "alpha.pdf"},
                dense_rank=2,
            ),
            SearchCandidate(
                id="beta-1",
                content="beta policy retrieval augmented generation evidence",
                metadata={"source_filename": "beta.pdf"},
                dense_rank=3,
            ),
        ]

        ranked = rank_hybrid_candidates("compare alpha beta retrieval evidence", candidates, top_k=2)

        self.assertEqual({candidate.metadata["source_filename"] for candidate in ranked}, {"alpha.pdf", "beta.pdf"})

    def test_diverse_source_selection_deduplicates_even_when_candidates_fit_slots(self):
        candidates = [
            SearchCandidate(
                id="alpha-1",
                content="alpha evidence one",
                metadata={"source_filename": "alpha.pdf"},
            ),
            SearchCandidate(
                id="alpha-2",
                content="alpha evidence two",
                metadata={"source_filename": "alpha.pdf"},
            ),
            SearchCandidate(
                id="beta-1",
                content="beta evidence",
                metadata={"source_filename": "beta.pdf"},
            ),
        ]

        selected = _select_diverse_sources(candidates, top_k=3)

        self.assertEqual(
            [candidate.metadata["source_filename"] for candidate in selected],
            ["alpha.pdf", "beta.pdf", "alpha.pdf"],
        )

    def test_source_reservation_prefers_overall_retrieval_within_blind_margin(self):
        candidates = [
            SearchCandidate(
                id="lexical-winner",
                content="evidence",
                metadata={"source_filename": "lexical.pdf"},
                final_score=0.18,
            ),
            SearchCandidate(
                id="retrieval-winner",
                content="evidence",
                metadata={"source_filename": "retrieval.pdf"},
                final_score=0.28,
            ),
        ]

        with patch(
            "core.retrieval._source_group_relevance_score",
            side_effect=lambda candidate, _group: {
                "lexical-winner": 0.55,
                "retrieval-winner": 0.44,
            }[candidate.id],
        ):
            selected = _select_source_covered_candidates(
                candidates,
                top_k=1,
                source_query_groups=[["blind comparison side"]],
            )

        self.assertEqual([candidate.id for candidate in selected], ["retrieval-winner"])

    def test_hybrid_rank_uses_decomposed_source_queries_for_coverage(self):
        query = "请比较Alpha简称与Beta简称两类资料的核心差异"
        candidates = [
            SearchCandidate(
                id="noise",
                content="通用资料的摘要。",
                metadata={"source_filename": "通用资料.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="alpha",
                content="Alpha canonical title 的核心证据。",
                metadata={"source_filename": "Alpha canonical title.pdf"},
                dense_rank=4,
            ),
            SearchCandidate(
                id="beta",
                content="Beta canonical title 的核心证据。",
                metadata={"source_filename": "Beta canonical title.pdf"},
                dense_rank=5,
            ),
        ]

        ranked = rank_hybrid_candidates(
            query,
            candidates,
            top_k=2,
            query_variants=[
                query,
                "Alpha canonical title 核心证据",
                "Beta canonical title 核心证据",
            ],
            source_query_groups=[
                ["Alpha canonical title 核心证据"],
                ["Beta canonical title 核心证据"],
            ],
        )

        self.assertEqual(
            {candidate.metadata["source_filename"] for candidate in ranked},
            {"Alpha canonical title.pdf", "Beta canonical title.pdf"},
        )

    def test_hybrid_rank_reserves_blind_comparison_sources_from_evidence_overlap(self):
        query = "请比较Alpha主题与Beta主题两类资料的核心差异"
        candidates = [
            SearchCandidate(
                id="generic",
                content="通用资料摘要，覆盖多个主题。",
                metadata={"source_filename": "generic.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="alpha",
                content="Alpha主题的核心证据和落地边界。",
                metadata={"source_filename": "a.pdf"},
                dense_rank=4,
            ),
            SearchCandidate(
                id="beta",
                content="Beta主题的核心证据和落地边界。",
                metadata={"source_filename": "b.pdf"},
                dense_rank=5,
            ),
        ]

        ranked = rank_hybrid_candidates(
            query,
            candidates,
            top_k=2,
            query_variants=[query, "Alpha主题 Beta主题 核心证据"],
            source_query_groups=[
                ["Alpha主题 核心证据"],
                ["Beta主题 核心证据"],
            ],
        )

        self.assertEqual(
            {candidate.metadata["source_filename"] for candidate in ranked},
            {"a.pdf", "b.pdf"},
        )

    def test_hybrid_rank_keeps_multiple_sections_for_single_named_document(self):
        candidates = [
            SearchCandidate(
                id="target-1",
                content="建设目标与总体范围",
                metadata={"source_filename": "项目实施方案.pdf"},
                dense_rank=1,
            ),
            SearchCandidate(
                id="target-2",
                content="验收指标与交付要求",
                metadata={"source_filename": "项目实施方案.pdf"},
                dense_rank=2,
            ),
            SearchCandidate(
                id="other-1",
                content="其他项目的验收指标",
                metadata={"source_filename": "其他方案.pdf"},
                dense_rank=3,
            ),
        ]

        ranked = rank_hybrid_candidates(
            "请依据《项目实施方案》说明建设目标和验收指标",
            candidates,
            top_k=2,
        )

        self.assertEqual([candidate.id for candidate in ranked], ["target-1", "target-2"])

    def test_chinese_quoted_document_scope_is_explicit_source(self):
        query = "请查找“七月文本检索基线配置”里关于“retrieval: lexical”的记录"

        self.assertTrue(classify_query_intent(query).explicit_source)
        self.assertEqual(_query_source_hints(query), ["七月文本检索基线配置"])

    def test_final_selection_prefers_distinct_exact_source_matches(self):
        candidates = [
            SearchCandidate(
                id="partial-high-score",
                content="partial overlap",
                metadata={"source_filename": "partial-report.pdf"},
                final_score=0.9,
                score_breakdown={"source": 0.7},
            ),
            SearchCandidate(
                id="exact-alpha",
                content="alpha",
                metadata={"source_filename": "Alpha Report.pdf"},
                final_score=0.4,
                score_breakdown={"source": 1.0},
            ),
            SearchCandidate(
                id="exact-beta",
                content="beta",
                metadata={"source_filename": "Beta Report.pdf"},
                final_score=0.3,
                score_breakdown={"source": 1.0},
            ),
        ]

        selected = _select_diverse_sources(candidates, top_k=2)

        self.assertEqual([candidate.id for candidate in selected], ["exact-alpha", "exact-beta"])

class _FakeCollection:
    def __init__(self, payload):
        self.payload = payload

    def get(self, include=None):
        _ = include
        return self.payload


class _FakeParentStore:
    def __init__(self, content):
        self.content = content

    def get_parent(self, parent_id):
        return {
            "content": self.content,
            "metadata": {"parent_id": parent_id},
        }


if __name__ == "__main__":
    unittest.main()
