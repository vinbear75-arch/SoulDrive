import unittest

from core.rag_engine import (
    build_agent_status_bar,
    build_evidence_mindmap,
    compact_context_text,
    normalize_technical_terms,
)


class RagContextTests(unittest.TestCase):
    def test_compact_context_text_preserves_head_and_tail(self):
        text = " ".join([f"token{i}" for i in range(500)])

        compacted = compact_context_text(text, max_chars=240)

        self.assertLessEqual(len(compacted), 260)
        self.assertIn("token0", compacted)
        self.assertIn("token499", compacted)
        self.assertIn("context trimmed", compacted)

    def test_legacy_evidence_mindmap_uses_evidence_text_without_meta_branches(self):
        answer = build_evidence_mindmap(
            "transformer机制是什么",
            [
                {
                    "id": "E1",
                    "section": "Transformer 机制",
                    "snippet": "Transformer 通过 Attention / Self-Attention 建模序列关系。",
                }
            ],
        )

        self.assertIsNotNone(answer)
        self.assertIn("# transformer机制是什么", answer)
        self.assertIn("## Transformer 机制", answer)
        self.assertIn("Attention / Self-Attention", answer)
        self.assertNotIn("## 论文依据", answer)
        self.assertNotIn("问题焦点", answer)
        self.assertNotIn("[E1]", answer)

    def test_normalize_technical_terms_keeps_transformer_name(self):
        self.assertEqual(normalize_technical_terms("变压器架构"), "Transformer 架构")

    def test_agent_status_bar_compacts_runtime_metadata(self):
        status = build_agent_status_bar(
            {
                "trace_id": "trace-123",
                "retrieval_mode": "parent_child_hybrid_dense_fts_graph_rrf",
                "top_k": 6,
                "evidence_count": 3,
                "matched_entities": ["数据安全", "隐私保护"],
                "evidence_gate": {"decision": "accept", "reason": "evidence accepted"},
            }
        )

        self.assertIn("【Agent 状态栏】", status)
        self.assertNotIn("trace-123", status)
        self.assertIn("evidence_count: 3", status)
        self.assertIn("数据安全", status)

    def test_agent_status_bar_is_stable_when_audit_trace_changes(self):
        first = build_agent_status_bar({"trace_id": "trace-a", "evidence_count": 1})
        second = build_agent_status_bar({"trace_id": "trace-b", "evidence_count": 1})

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
