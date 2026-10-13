import unittest

from core.context_gate import plan_retrieval
from core.query_expansion import (
    expand_query_variants,
    query_requires_source_diversity,
    split_comparison_query,
)


class QueryExpansionTests(unittest.TestCase):
    def test_expand_query_variants_keeps_original_query_first(self):
        variants = expand_query_variants("这个方案怎么保证隐私？")

        self.assertGreaterEqual(len(variants), 1)
        self.assertEqual(variants[0], "这个方案怎么保证隐私？")

    def test_expand_query_variants_is_bounded_and_deduplicated(self):
        variants = expand_query_variants("GraphRAG 是什么？")

        self.assertLessEqual(len(variants), 5)
        self.assertEqual(len(variants), len(set(variants)))

    def test_expand_query_variants_preserves_technical_terms(self):
        variants = expand_query_variants("GraphRAG local search 怎么工作？")

        joined = " ".join(variants)
        self.assertIn("GraphRAG", joined)
        self.assertIn("local search", joined)

    def test_expand_query_variants_adds_lightweight_enterprise_term_aliases(self):
        variants = expand_query_variants("异常检测论文主要解决什么问题？")

        joined = " ".join(variants).lower()
        self.assertLessEqual(len(variants), 3)
        self.assertIn("anomaly detection", joined)

    def test_expand_query_variants_splits_blind_cross_document_question(self):
        variants = expand_query_variants(
            "在不透露文档名的情况下，对比RAG 评测综述与企业知识管理大模型两类资料："
            "它们在evaluation、知识管理和落地证据上的关注点有什么差异？"
        )

        self.assertEqual(variants[0].startswith("在不透露文档名的情况下"), True)
        self.assertTrue(any("RAG 评测综述 核心证据" in item for item in variants))
        self.assertTrue(any("企业知识管理大模型 核心证据" in item for item in variants))
        self.assertLessEqual(len(variants), 5)

    def test_expand_query_variants_splits_two_explicit_document_titles(self):
        variants = expand_query_variants(
            "请对比《数据安全技术规范》和《项目实施方案》在验收要求上的差异"
        )

        self.assertTrue(any(item == "数据安全技术规范 核心证据" for item in variants))
        self.assertTrue(any(item == "项目实施方案 核心证据" for item in variants))

    def test_query_requires_source_diversity_detects_comparison_intent(self):
        self.assertTrue(query_requires_source_diversity("分别说明两个方案的风险与收益"))
        self.assertFalse(query_requires_source_diversity("说明项目方案的建设目标"))
        self.assertFalse(query_requires_source_diversity("验收指标值和完成期限分别是多少"))

    def test_query_requires_source_diversity_does_not_misclassify_single_document_rule(self):
        self.assertFalse(query_requires_source_diversity("实验指标在什么条件下可以比较？"))

    def test_comparison_without_document_count_suffix_still_produces_subqueries(self):
        query = "请对比项目A与项目B的安全风险"

        self.assertEqual(
            split_comparison_query(query),
            ["项目A 核心证据", "项目B 核心证据"],
        )
        plan = plan_retrieval(query)
        self.assertEqual(plan.action, "decompose")
        self.assertEqual(plan.subqueries, ("项目A 核心证据", "项目B 核心证据"))
        variants = expand_query_variants(query)
        self.assertIn("项目A 核心证据", variants)
        self.assertIn("项目B 核心证据", variants)

    def test_expand_query_variants_covers_strict_retrieval_eval_terms(self):
        cases = [
            (
                "生成式人工智能服务安全基本要求里的标准要求是什么？",
                ["生成式AI", "AIGC", "人工智能安全"],
            ),
            (
                "数据分类分级规则和敏感个人信息处理有什么关系？",
                ["数据安全技术", "data classification", "敏感个人信息"],
            ),
            (
                "MaxKB 知识库问答系统如何做 RAG？",
                ["基于大模型和RAG的知识库问答系统", "knowledge base QA"],
            ),
            (
                "LazyLLM 私有化部署权限安全怎么做？",
                ["企业级RAG方案", "access control"],
            ),
            (
                "GraphRAG local search 和 global search 区别是什么？",
                ["graph retrieval augmented generation", "local search", "global search"],
            ),
            (
                "城市+AI 场景清单有哪些交通治理案例？",
                ["城市AI应用场景", "industry scenario"],
            ),
            (
                "综合交通运输大模型智能体创新应用怎么做召回？",
                ["交通运输", "智能体", "agent"],
            ),
        ]

        for query, expected_terms in cases:
            with self.subTest(query=query):
                variants = expand_query_variants(query, limit=5)
                joined = " ".join(variants)

                self.assertEqual(variants[0], query)
                self.assertLessEqual(len(variants), 5)
                self.assertEqual(len(variants), len(set(variants)))
                self.assertTrue(any(item.startswith(query + " ") for item in variants[1:]))
                for term in expected_terms:
                    self.assertIn(term, joined)

    def test_expand_query_variants_maps_blind_document_descriptions_to_index_titles(self):
        cases = [
            (
                "在不透露文档名的情况下，对比AI 安全治理框架与生成式 AI 安全规范两类资料：它们在治理框架、生成式 AI 和落地证据上的关注点有什么差异？",
                ["人工智能安全治理框架", "生成式人工智能服务安全基本要求"],
            ),
            (
                "在不透露文档名的情况下，对比个人信息保护标准与生成式 AI 安全规范两类资料：它们在个人信息、生成式 AI 和落地证据上的关注点有什么差异？",
                ["个人信息保护国家标准体系（2025版）", "生成式人工智能服务安全基本要求"],
            ),
            (
                "在不透露文档名的情况下，对比数据安全标准体系与生成式 AI 安全规范两类资料：它们在数据安全、生成式 AI 和落地证据上的关注点有什么差异？",
                ["数据安全国家标准体系（2025版）", "生成式人工智能服务安全基本要求"],
            ),
        ]

        for query, expected_terms in cases:
            with self.subTest(query=query):
                joined = " ".join(expand_query_variants(query))
                for term in expected_terms:
                    self.assertIn(term, joined)


if __name__ == "__main__":
    unittest.main()
