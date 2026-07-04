import unittest

from core.skill_runtime import create_skill_execution_plan, match_skills, normalize_skill_payload


class SkillRuntimeTests(unittest.TestCase):
    def test_normalize_skill_payload_adds_executable_contract(self):
        skill = normalize_skill_payload(
            {
                "title": "TDD workflow",
                "content": "1. Write a failing test\n2. Implement minimal code\n3. Run verification",
                "tags": ["tdd", "testing"],
            }
        )

        self.assertEqual(skill["intent"], "TDD workflow")
        self.assertGreaterEqual(len(skill["steps"]), 3)
        self.assertEqual(skill["tool_permissions"], [])
        self.assertTrue(skill["output_contract"])
        self.assertTrue(skill["success_criteria"])

    def test_match_skills_selects_relevant_skill_and_ignores_unrelated_query(self):
        skill = normalize_skill_payload(
            {
                "skill_id": "skill_tdd",
                "title": "TDD workflow",
                "content": "Use this when implementing a feature with tests first.",
                "triggers": ["implement feature", "tests first", "tdd"],
                "tags": ["engineering"],
            }
        )

        [match] = match_skills("Please implement this feature with tests first", [skill])
        self.assertEqual(match["skill_id"], "skill_tdd")
        self.assertGreater(match["score"], 0.3)

        self.assertEqual(match_skills("What does this document say about privacy?", [skill]), [])

    def test_match_skills_supports_chinese_workflow_terms(self):
        skill = normalize_skill_payload(
            {
                "skill_id": "skill_cn_tdd",
                "title": "实现前先写失败测试",
                "content": "处理代码功能时，先写失败测试，再实现最小代码，再运行测试确认通过。",
            }
        )

        [match] = match_skills("接下来实现一个后端功能时应该按什么流程做？", [skill])

        self.assertEqual(match["skill_id"], "skill_cn_tdd")

    def test_create_skill_execution_plan_is_structured(self):
        skill = normalize_skill_payload(
            {
                "skill_id": "skill_tdd",
                "title": "TDD workflow",
                "content": "Write a failing test. Implement minimal code. Run verification.",
                "required_inputs": ["task description"],
            }
        )

        plan = create_skill_execution_plan(skill, "Implement learning validator", matched_score=0.82)

        self.assertEqual(plan["skill_id"], "skill_tdd")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["matched_score"], 0.82)
        self.assertTrue(plan["steps"])
        self.assertEqual(plan["required_inputs"], ["task description"])
        self.assertTrue(plan["output_contract"])


if __name__ == "__main__":
    unittest.main()
