import unittest

from core.rag_engine import build_evidence_mindmap, compact_context_text, normalize_technical_terms


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


if __name__ == "__main__":
    unittest.main()
