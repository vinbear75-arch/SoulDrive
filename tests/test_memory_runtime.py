import unittest

from core.memory_runtime import build_memory_capsule


class MemoryRuntimeTests(unittest.TestCase):
    def test_build_memory_capsule_prioritizes_preferences_and_bounds_text(self):
        capsule = build_memory_capsule(
            [
                {
                    "memory_id": "mem_workflow",
                    "subtype": "workflow_fact",
                    "title": "Workflow",
                    "content": "Use the project branch for implementation.",
                    "updated_at": 1.0,
                },
                {
                    "memory_id": "mem_pref",
                    "subtype": "user_preference",
                    "title": "Review preference",
                    "content": "User wants human review before long-term memory writes.",
                    "updated_at": 2.0,
                },
            ],
            max_chars=120,
        )

        self.assertEqual(capsule["items"][0]["memory_id"], "mem_pref")
        self.assertLessEqual(len(capsule["text"]), 120)
        self.assertIn("Review preference", capsule["text"])


if __name__ == "__main__":
    unittest.main()
