import unittest

from core.answer_quality import (
    citation_coverage,
    evaluate_evidence_gate,
    refusal_answer,
    validate_answer_citations,
)


class AnswerQualityTests(unittest.TestCase):
    def test_evidence_gate_rejects_empty_or_low_confidence_evidence(self):
        empty = evaluate_evidence_gate([])
        low = evaluate_evidence_gate([{"id": "E1", "score": 0.001}])

        self.assertFalse(empty.allowed)
        self.assertFalse(low.allowed)
        self.assertEqual(empty.decision, "reject")
        self.assertEqual(low.decision, "reject")
        self.assertEqual(low.reason, "evidence confidence below threshold")

    def test_evidence_gate_requests_retry_for_mid_confidence_evidence(self):
        gate = evaluate_evidence_gate([{"id": "E1", "score": 0.055}])

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence confidence requires retry")

    def test_evidence_gate_accepts_low_scored_corroborating_sources(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.070,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
                },
                {
                    "id": "E2",
                    "score": 0.066,
                    "source_filename": "乙方案.pdf",
                    "snippet": "模型风险评估采用动态授权，安全审计按季度验收。",
                },
            ],
            query="比较两个方案在模型风险评估和安全审计上的做法。",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.decision, "accept")
        self.assertEqual(gate.reason, "evidence accepted by corroboration")

    def test_evidence_gate_does_not_treat_one_source_as_corroboration(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.070,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
                },
                {
                    "id": "E2",
                    "score": 0.066,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按季度检查。",
                },
            ],
            query="比较两个方案在模型风险评估和安全审计上的做法。",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")

    def test_evidence_gate_requests_retry_when_query_is_not_answered_by_evidence(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "snippet": "传统企业知识管理系统存在构建成本高、知识利用率低的问题。",
                }
            ],
            query="SoulDrive 的创始人是谁？",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_does_not_accept_entity_only_overlap_for_attribute_query(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "snippet": "SoulDrive 是一个面向私有知识库的本地知识引擎。",
                }
            ],
            query="SoulDrive 的创始人是谁？",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_accepts_scored_evidence(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.2,
                    "snippet": "传统企业知识管理系统存在构建成本高、知识利用率低的问题。",
                },
                {"id": "E2", "score": 0.09},
            ],
            query="传统企业知识管理系统主要存在哪些问题？",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.decision, "accept")
        self.assertEqual(gate.reason, "evidence accepted")

    def test_refusal_answer_is_explicit_about_local_evidence(self):
        gate = evaluate_evidence_gate([])
        answer = refusal_answer("What is GraphRAG?", gate)

        self.assertIn("未找到足够可靠的相关证据", answer)

    def test_citation_coverage_reports_used_and_unsupported_citations(self):
        report = citation_coverage("结论来自 [E1]，但 [E9] 不存在。", [{"id": "E1"}, {"id": "E2"}])

        self.assertEqual(report["coverage"], 0.5)
        self.assertEqual(report["used"], ["E1"])
        self.assertEqual(report["unsupported"], ["E9"])

    def test_validate_answer_citations_rejects_unsupported_citation(self):
        report = validate_answer_citations(
            "结论来自 [E1]，但 [E9] 也被引用。",
            [{"id": "E1"}, {"id": "E2"}],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["decision"], "retry")
        self.assertEqual(report["unsupported"], ["E9"])

    def test_validate_answer_citations_rejects_missing_citation_when_evidence_exists(self):
        report = validate_answer_citations(
            "这里有结论，但是没有给出任何引用。",
            [{"id": "E1"}],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["decision"], "retry")
        self.assertEqual(report["reason"], "answer missing evidence citation")


if __name__ == "__main__":
    unittest.main()
