import unittest

from scripts import eval_workflow_roles


class WorkflowRoleEvalTests(unittest.TestCase):
    def test_role_eval_proves_windows_have_distinct_contracts_and_gates(self):
        records = eval_workflow_roles.load_records(eval_workflow_roles.DEFAULT_DATASET)
        report = eval_workflow_roles.evaluate(records)

        self.assertEqual(report["summary"]["total"], 6)
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(report["summary"]["role_accuracy"], 1.0)
        self.assertEqual(report["summary"]["readiness_accuracy"], 1.0)
        self.assertEqual(report["summary"]["retrieval_profile_accuracy"], 1.0)
        self.assertEqual(report["summary"]["retrieval_focus_accuracy"], 1.0)
        self.assertEqual(report["summary"]["output_contract_accuracy"], 1.0)
        self.assertEqual(report["summary"]["citation_namespace_accuracy"], 1.0)
        self.assertEqual(report["summary"]["rag_gate_accuracy"], 1.0)

        profiles = {
            workflow_id: workflow_report["retrieval_profile"]
            for workflow_id, workflow_report in report["summary"]["workflow_breakdown"].items()
        }
        self.assertEqual(
            profiles,
            {
                "paper_review": "paper_review",
                "experiment_compare": "experiment_compare",
                "meeting_actions": "meeting_record_first",
            },
        )

    def test_role_eval_rejects_unknown_workflow_and_malformed_contract(self):
        with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
            eval_workflow_roles.evaluate(
                [
                    {
                        "schema_version": "souldrive_workflow_role_quality_v0.1",
                        "id": "ROLE-BAD-001",
                        "workflow_id": "unbounded_agent",
                        "query": "问题",
                        "project_id": "p1",
                        "expected_profile": "default",
                        "expected_required_sections": ["回答"],
                        "expected_allowed_prefixes": ["E"],
                        "expected_ready": False,
                    }
                ]
            )

        with self.assertRaisesRegex(ValueError, "expected_required_sections"):
            eval_workflow_roles.evaluate(
                [
                    {
                        "schema_version": "souldrive_workflow_role_quality_v0.1",
                        "id": "ROLE-BAD-002",
                        "workflow_id": "paper_review",
                        "query": "问题",
                        "project_id": "p1",
                        "document_names": ["论文A.pdf"],
                        "expected_profile": "paper_review",
                        "expected_required_sections": [],
                        "expected_allowed_prefixes": ["E", "V"],
                        "expected_ready": True,
                        "expected_needs_rag": True,
                        "expected_source_diversity": False,
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
