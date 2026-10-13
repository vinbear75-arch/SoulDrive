import unittest

from scripts import eval_graphrag_route


class GraphRAGRouteEvalTests(unittest.TestCase):
    def test_route_eval_covers_hybrid_local_and_global_contracts(self):
        report = eval_graphrag_route.evaluate(
            [
                {
                    "schema_version": "souldrive_graphrag_route_quality_v0.1",
                    "id": "ROUTE-HYBRID-001",
                    "query": "RAG 的核心思想是什么？",
                    "expected_mode": "hybrid",
                    "expected_use_graph": False,
                },
                {
                    "schema_version": "souldrive_graphrag_route_quality_v0.1",
                    "id": "ROUTE-LOCAL-001",
                    "query": "GraphRAG 中实体之间的影响链和上下游关系是什么？",
                    "expected_mode": "local",
                    "expected_use_graph": True,
                    "required_signals": ["影响链", "上下游"],
                },
                {
                    "schema_version": "souldrive_graphrag_route_quality_v0.1",
                    "id": "ROUTE-GLOBAL-001",
                    "query": "请总结这些论文关于检索纠错的共同思路。",
                    "expected_mode": "global",
                    "expected_use_graph": True,
                    "required_signals": ["共同思路"],
                },
                {
                    "schema_version": "souldrive_graphrag_route_quality_v0.1",
                    "id": "ROUTE-PRECEDENCE-001",
                    "query": "请分析本项目整体方案中实体A和实体B的影响链。",
                    "expected_mode": "local",
                    "expected_use_graph": True,
                    "required_signals": ["影响链"],
                },
            ]
        )

        self.assertEqual(report["summary"]["total"], 4)
        self.assertEqual(report["summary"]["passed"], 4)
        self.assertEqual(report["summary"]["route_accuracy"], 1.0)
        self.assertEqual(report["summary"]["graph_gate_accuracy"], 1.0)

    def test_route_eval_rejects_unknown_expected_mode(self):
        with self.assertRaisesRegex(ValueError, "unsupported expected_mode"):
            eval_graphrag_route.evaluate(
                [
                    {
                        "schema_version": "souldrive_graphrag_route_quality_v0.1",
                        "id": "ROUTE-BAD-001",
                        "query": "问题",
                        "expected_mode": "drift",
                        "expected_use_graph": True,
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
