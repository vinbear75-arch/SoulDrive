import unittest

from scripts import eval_memory_quality


class MemoryQualityEvalTests(unittest.TestCase):
    def test_memory_quality_runner_covers_recall_scope_workflow_and_updates(self):
        report = eval_memory_quality.evaluate(
            [
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-01",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "mail_preference",
                            "subtype": "user_preference",
                            "title": "邮件发送时段",
                            "content": "用户偏好早上发送邮件。",
                        },
                        {
                            "op": "query",
                            "query": "邮件发送时段",
                            "expect": {
                                "required_aliases": ["mail_preference"],
                                "expected_content": ["早上"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-02",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "project_a_fact",
                            "subtype": "project_fact",
                            "project_id": "project-a",
                            "title": "项目 A 数据集",
                            "content": "项目 A 使用公开数据集。",
                        },
                        {
                            "op": "remember",
                            "alias": "project_b_fact",
                            "subtype": "project_fact",
                            "project_id": "project-b",
                            "title": "项目 B 数据集",
                            "content": "项目 B 使用内部数据集。",
                        },
                        {
                            "op": "query",
                            "query": "数据集",
                            "project_id": "project-a",
                            "expect": {
                                "required_aliases": ["project_a_fact"],
                                "forbidden_aliases": ["project_b_fact"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-03",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "paper_window_fact",
                            "subtype": "workflow_fact",
                            "workflow_id": "paper_review",
                            "title": "当前研究窗口",
                            "content": "论文窗口关注方法和证据。",
                        },
                        {
                            "op": "remember",
                            "alias": "experiment_window_fact",
                            "subtype": "workflow_fact",
                            "workflow_id": "experiment_compare",
                            "title": "当前研究窗口",
                            "content": "实验窗口关注指标和对照。",
                        },
                        {
                            "op": "query",
                            "query": "当前研究窗口",
                            "workflow_id": "paper_review",
                            "context_only": True,
                            "expect": {
                                "required_aliases": ["paper_window_fact"],
                                "forbidden_aliases": ["experiment_window_fact"],
                                "expected_content": ["方法和证据"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-04",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "model_version",
                            "subtype": "project_fact",
                            "project_id": "project-a",
                            "title": "模型版本",
                            "content": "当前版本是 v1。",
                        },
                        {
                            "op": "update",
                            "alias": "model_version",
                            "project_id": "project-a",
                            "title": "模型版本",
                            "content": "当前版本是 v2。",
                        },
                        {
                            "op": "query",
                            "query": "模型版本",
                            "project_id": "project-a",
                            "expect": {
                                "required_aliases": ["model_version"],
                                "expected_content": ["v2"],
                                "forbidden_content": ["v1"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-05",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "historical_version",
                            "subtype": "project_fact",
                            "project_id": "project-a",
                            "title": "历史模型版本",
                            "content": "历史版本是 v1。",
                        },
                        {
                            "op": "update",
                            "alias": "historical_version",
                            "project_id": "project-a",
                            "title": "历史模型版本",
                            "content": "当前版本是 v2。",
                            "valid_from": 200.0,
                        },
                        {
                            "op": "query",
                            "query": "历史模型版本",
                            "project_id": "project-a",
                            "context_only": True,
                            "as_of": 100.0,
                            "expect": {
                                "required_aliases": ["historical_version"],
                                "expected_content": ["历史版本是 v1"],
                                "forbidden_content": ["当前版本是 v2"],
                            },
                        },
                        {
                            "op": "query",
                            "query": "历史模型版本",
                            "project_id": "project-a",
                            "context_only": True,
                            "as_of": 300.0,
                            "expect": {
                                "required_aliases": ["historical_version"],
                                "expected_content": ["当前版本是 v2"],
                                "forbidden_content": ["历史版本是 v1"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-06",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "old_notification_preference",
                            "subtype": "user_preference",
                            "title": "通知频率",
                            "content": "用户希望每天收到通知。",
                        },
                        {
                            "op": "remember",
                            "alias": "new_notification_preference",
                            "subtype": "user_preference",
                            "title": "通知频率",
                            "content": "用户现在希望每周收到通知。",
                            "conflicts_with": ["old_notification_preference"],
                        },
                        {
                            "op": "query",
                            "query": "通知频率",
                            "context_only": True,
                            "expect": {
                                "required_aliases": ["new_notification_preference"],
                                "forbidden_aliases": ["old_notification_preference"],
                                "expected_content": ["每周"],
                                "forbidden_content": ["每天"],
                            },
                        },
                    ],
                },
                {
                    "schema_version": "souldrive_memory_quality_v0.1",
                    "id": "MEM-EVAL-07",
                    "events": [
                        {
                            "op": "remember",
                            "alias": "project_delivery_constraint",
                            "subtype": "project_constraint",
                            "project_id": "project-a",
                            "title": "项目交付约束",
                            "content": "本项目必须先核对证据，再生成交付物。",
                        },
                        {
                            "op": "context",
                            "query": "请概览本轮材料。",
                            "project_id": "project-a",
                            "expect": {
                                "required_aliases": ["project_delivery_constraint"],
                                "expected_content": ["先核对证据"],
                                "expected_gate_reasons": ["project_constraint"],
                            },
                        },
                    ],
                },
            ]
        )

        self.assertEqual(report["summary"]["total_queries"], 8)
        self.assertEqual(report["summary"]["passed_queries"], 8)
        self.assertEqual(report["summary"]["memory_recall_at_k"], 1.0)
        self.assertEqual(report["summary"]["stale_or_forbidden_leakage_rate"], 0.0)

    def test_memory_quality_validation_rejects_unknown_operation(self):
        with self.assertRaisesRegex(ValueError, "unsupported memory event"):
            eval_memory_quality.evaluate(
                [
                    {
                        "schema_version": "souldrive_memory_quality_v0.1",
                        "id": "MEM-EVAL-BAD",
                        "events": [{"op": "guess"}],
                    }
                ]
            )

    def test_memory_quality_validation_requires_project_scope_for_constraint(self):
        with self.assertRaisesRegex(ValueError, "project_id is required"):
            eval_memory_quality.evaluate(
                [
                    {
                        "schema_version": "souldrive_memory_quality_v0.1",
                        "id": "MEM-EVAL-BAD-CONSTRAINT",
                        "events": [
                            {
                                "op": "remember",
                                "alias": "unbound_constraint",
                                "subtype": "project_constraint",
                                "title": "项目约束",
                                "content": "本项目必须保留证据。",
                            }
                        ],
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
