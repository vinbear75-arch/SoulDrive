import unittest
from unittest.mock import patch

from core.memory_gate import evaluate_memory_write_gate
from core.memory_runtime import gate_memory_context


class MemoryGateTests(unittest.TestCase):
    def test_explicit_durable_memory_requires_human_review(self):
        candidate = {
            "asset_type": "memory",
            "subtype": "user_preference",
            "title": "Review preference",
            "content": "I prefer human review before every long-term memory write.",
            "evidence": ["Please remember: I prefer human review before every long-term memory write."],
            "rationale": "Durable preference",
            "confidence": 0.94,
            "source_message_indexes": [0],
        }

        gate = evaluate_memory_write_gate(
            candidate,
            [{"role": "user", "content": candidate["evidence"][0]}],
        )

        self.assertEqual(gate.decision, "review")
        self.assertTrue(gate.requires_human_review)
        self.assertFalse(gate.hard_block)
        self.assertGreaterEqual(gate.score, 0.72)

    def test_secret_is_hard_blocked(self):
        gate = evaluate_memory_write_gate(
            {
                "asset_type": "memory",
                "title": "API key",
                "content": "Remember API key abc123",
                "evidence": ["Remember API key abc123"],
                "confidence": 1.0,
                "source_message_indexes": [0],
            },
            [{"role": "user", "content": "Remember API key abc123"}],
        )

        self.assertEqual(gate.decision, "reject")
        self.assertTrue(gate.hard_block)

    def test_read_gate_only_injects_query_relevant_memory(self):
        result = gate_memory_context(
            "本项目使用什么数据库？",
            [
                {
                    "memory_id": "relevant",
                    "subtype": "project_fact",
                    "title": "Database",
                    "content": "本项目使用 SQLite 数据库。",
                    "score": 0.8,
                },
                {
                    "memory_id": "irrelevant",
                    "subtype": "workflow_fact",
                    "title": "Meeting",
                    "content": "每周一召开销售例会。",
                    "score": 0.0,
                },
            ],
        )

        self.assertEqual([item["memory_id"] for item in result["memories"]], ["relevant"])
        self.assertEqual(result["gate"]["rejected"][0]["memory_id"], "irrelevant")

    def test_read_gate_decays_stale_unconfirmed_memory_without_erasing_recent_one(self):
        with patch("core.memory_runtime.time.time", return_value=200 * 24 * 60 * 60):
            result = gate_memory_context(
                "本项目使用什么数据库？",
                [
                    {
                        "memory_id": "stale",
                        "subtype": "user_preference",
                        "title": "Database",
                        "content": "本项目使用 SQLite 数据库。",
                        "score": 0.8,
                        "created_at": 1.0,
                        "confirmed_at": None,
                        "last_used_at": None,
                    },
                    {
                        "memory_id": "recent",
                        "subtype": "user_preference",
                        "title": "Database",
                        "content": "本项目使用 SQLite 数据库。",
                        "score": 0.8,
                        "created_at": 199 * 24 * 60 * 60,
                        "confirmed_at": 199 * 24 * 60 * 60,
                        "last_used_at": 199 * 24 * 60 * 60,
                    },
                ],
            )

        self.assertEqual([item["memory_id"] for item in result["memories"]], ["recent", "stale"])
        self.assertEqual(result["memories"][0]["freshness_weight"], 1.0)
        self.assertEqual(result["memories"][1]["freshness_weight"], 0.45)

    def test_read_gate_keeps_project_preference_when_global_preference_conflicts(self):
        result = gate_memory_context(
            "应该怎样保存记忆？",
            [
                {
                    "memory_id": "global-memory",
                    "subtype": "user_preference",
                    "scope": "global",
                    "title": "自动保存",
                    "content": "以后每次回答自动保存记忆。",
                    "score": 0.95,
                    "confirmation_status": "human_reviewed",
                },
                {
                    "memory_id": "project-memory",
                    "subtype": "user_preference",
                    "scope": "project",
                    "project_id": "project-a",
                    "title": "关闭前审核",
                    "content": "只在关闭前集中审核记忆。",
                    "score": 0.4,
                    "confirmation_status": "explicit_signal",
                },
            ],
        )

        assert [item["memory_id"] for item in result["memories"]] == ["project-memory"]
        assert result["gate"]["rejected"][-1]["reason"] == "conflicting_preference"
        assert result["gate"]["rejected"][-1]["memory_id"] == "global-memory"


if __name__ == "__main__":
    unittest.main()
