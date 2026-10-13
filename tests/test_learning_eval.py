import tempfile
import unittest
from pathlib import Path

from scripts import eval_learning_loop


class LearningEvalTests(unittest.TestCase):
    def test_keyword_metric_scores_expected_and_forbidden_terms(self):
        score = eval_learning_loop.score_answer(
            answer="应该在关闭程序前集中审核，不希望每轮问答都弹窗。",
            expected_keywords=["关闭", "集中审核"],
            forbidden_keywords=["每次提问后"],
        )

        self.assertEqual(score["expected_hits"], 2)
        self.assertEqual(score["forbidden_hits"], 0)
        self.assertTrue(score["passed"])

    def test_load_records_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "learning.jsonl"
            path.write_text(
                '{"id":"X","setup_assets":[],"query":"q","expected_keywords":[],"forbidden_keywords":[],"metric":"m"}\n',
                encoding="utf-8",
            )

            records = eval_learning_loop.load_records(path)

        self.assertEqual(records[0]["id"], "X")

    def test_summarize_reports_pass_rate_and_latency(self):
        summary = eval_learning_loop.summarize(
            [
                {"score": {"passed": True}, "latency_ms": 5},
                {"score": {"passed": False}, "latency_ms": 15},
            ]
        )

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["average_latency_ms"], 10.0)

    def test_seeded_runtime_uses_memory_and_skill_assets(self):
        records = [
            {
                "id": "RUNTIME-01",
                "metric": "learning_runtime",
                "setup_assets": [
                    {
                        "asset_type": "memory",
                        "subtype": "user_preference",
                        "title": "Review before saving",
                        "content": "User wants human review before long-term memory writes.",
                    },
                    {
                        "asset_type": "skill",
                        "subtype": "sop",
                        "title": "TDD workflow",
                        "content": "Use tests first when implementing a feature.",
                        "triggers": ["implement feature", "tests first"],
                        "steps": ["Write a failing test", "Implement minimal code", "Run verification"],
                    },
                ],
                "query": "Please implement this feature with tests first",
                "expected_keywords": ["Review before saving", "TDD workflow", "Write a failing test"],
                "forbidden_keywords": ["unavailable"],
            }
        ]

        [result] = eval_learning_loop.run_seeded_runtime(records)

        self.assertTrue(result["score"]["passed"])
        self.assertEqual(result["learning_recall_at_k"], 2)
        self.assertEqual(result["skill_execution_plan_rate"], 1.0)

    def test_compare_baseline_and_runtime_reports_improvement(self):
        records = [
            {
                "id": "RUNTIME-02",
                "metric": "learning_runtime",
                "setup_assets": [
                    {
                        "asset_type": "skill",
                        "subtype": "sop",
                        "title": "TDD workflow",
                        "content": "Use tests first when implementing a feature.",
                        "triggers": ["implement feature", "tests first"],
                        "steps": ["Write a failing test"],
                    }
                ],
                "query": "Please implement this feature with tests first",
                "expected_keywords": ["TDD workflow", "Write a failing test"],
                "forbidden_keywords": ["unavailable"],
            }
        ]

        comparison = eval_learning_loop.compare_baseline_and_runtime(records)

        self.assertLess(comparison["baseline"]["summary"]["pass_rate"], comparison["runtime"]["summary"]["pass_rate"])

    def test_seeded_runtime_scopes_procedural_skills_to_workflow(self):
        records = [
            {
                "id": "SCOPE-01",
                "metric": "procedural_memory_scope",
                "workflow_id": "paper_review",
                "setup_assets": [
                    {
                        "asset_type": "skill",
                        "subtype": "sop",
                        "title": "Meeting actions",
                        "content": "Extract meeting decisions and action items.",
                        "workflow_ids": ["meeting_actions"],
                    },
                    {
                        "asset_type": "skill",
                        "subtype": "sop",
                        "title": "Paper review",
                        "content": "Extract paper methods and citations.",
                        "workflow_ids": ["paper_review"],
                    },
                ],
                "query": "Extract paper methods and action items",
                "expected_keywords": ["Paper review", "Extract paper methods"],
                "forbidden_keywords": ["Meeting actions", "meeting decisions"],
            }
        ]

        [result] = eval_learning_loop.run_seeded_runtime(records)

        self.assertTrue(result["score"]["passed"])
        self.assertEqual(result["matched_skill_ids"], ["Paper review"])


if __name__ == "__main__":
    unittest.main()
