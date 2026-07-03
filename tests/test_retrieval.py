import unittest

from core.knowledge_base import LocalKnowledgeBase
from core.retrieval import SearchCandidate, _select_diverse_sources, build_citations, rank_hybrid_candidates


class RetrievalTests(unittest.TestCase):
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

    def test_build_citations_exposes_source_and_page(self):
        candidate = SearchCandidate(
            id="doc_0",
            content="A paper excerpt about retrieval augmented generation.",
            metadata={
                "source_filename": "rag.pdf",
                "source_path": "E:/rag.pdf",
                "page": 3,
                "chunk_index": 2,
                "Header 2": "Method",
            },
            final_score=0.5,
        )

        citations = build_citations([candidate])

        self.assertEqual(citations[0]["id"], "E1")
        self.assertEqual(citations[0]["source_filename"], "rag.pdf")
        self.assertNotIn("source_path", citations[0])
        self.assertEqual(citations[0]["page_label"], "3")
        self.assertEqual(citations[0]["section"], "Method")

    def test_merge_candidates_collapses_children_by_parent_id(self):
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

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].id, "parent-1_child_0")
        self.assertEqual(merged[0].dense_rank, 5)
        self.assertEqual(merged[0].sparse_rank, 2)
        self.assertEqual(merged[0].keyword_score, 3.5)
        self.assertEqual(merged[0].metadata["child_excerpt"], "keyword child excerpt")

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
        self.assertEqual(candidates[0].id, "source_title:child-2")
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

        self.assertEqual(ranked[0].id, "maxkb-paper")
        self.assertGreater(ranked[0].score_breakdown["source"], ranked[1].score_breakdown["source"])

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


if __name__ == "__main__":
    unittest.main()
