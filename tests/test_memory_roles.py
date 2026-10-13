import unittest

from core.memory_roles import classify_memory_family


class MemoryRoleTests(unittest.TestCase):
    def test_existing_assets_project_into_explicit_memory_families(self):
        self.assertEqual(classify_memory_family("memory", "user_preference"), "policy")
        self.assertEqual(classify_memory_family("memory", "project_constraint"), "policy")
        self.assertEqual(classify_memory_family("memory", "project_fact"), "semantic")
        self.assertEqual(classify_memory_family("memory", "workflow_fact"), "semantic")
        self.assertEqual(classify_memory_family("skill", "sop"), "procedural")

    def test_unknown_memory_subtype_is_not_admissible_by_projection(self):
        self.assertEqual(classify_memory_family("memory", "legacy_note"), "unknown")
        self.assertEqual(classify_memory_family("memory", ""), "unknown")


if __name__ == "__main__":
    unittest.main()
