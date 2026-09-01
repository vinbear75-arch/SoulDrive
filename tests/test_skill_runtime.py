import hashlib
import tempfile
import unittest
from pathlib import Path

from core.skill_runtime import (
    create_skill_execution_plan,
    execute_skill_script,
    match_skills,
    normalize_skill_payload,
)


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

    def test_match_skills_crosses_chinese_query_to_english_tdd_skill(self):
        skill = normalize_skill_payload(
            {
                "skill_id": "skill_en_tdd",
                "title": "test-driven-development",
                "content": "Use when implementing any feature or bugfix, before writing implementation code.",
            }
        )

        [match] = match_skills("实现一个功能并先编写测试", [skill])

        self.assertEqual(match["skill_id"], "skill_en_tdd")

    def test_match_skills_keeps_different_skill_ids_with_same_title(self):
        skills = [
            normalize_skill_payload(
                {
                    "skill_id": "skill_tdd_old",
                    "title": "TDD workflow",
                    "content": "Implement a feature with tests first.",
                }
            ),
            normalize_skill_payload(
                {
                    "skill_id": "skill_tdd_new",
                    "title": "TDD workflow",
                    "content": "Implement a feature with tests first.",
                }
            ),
        ]

        matches = match_skills("Implement a feature with tests first", skills, top_k=3)

        self.assertEqual([match["skill_id"] for match in matches], ["skill_tdd_new", "skill_tdd_old"])

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

    def test_execute_skill_script_runs_only_declared_python_inside_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            script = skill_dir / "scripts" / "check.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "import json, pathlib, sys\n"
                "payload = json.load(sys.stdin)\n"
                "pathlib.Path(sys.argv[1]).write_text(payload['value'], encoding='utf-8')\n"
                "print('validated')\n",
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text("---\nname: Demo\ndescription: Demo\n---\n", encoding="utf-8")
            skill = {
                "skill_id": "skill_demo",
                "title": "Demo",
                "source_path": "demo/SKILL.md",
                "resource_manifest": [{
                    "path": "scripts/check.py",
                    "kind": "script",
                    "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                }],
            }
            output_dir = Path(temp_dir) / "outputs"
            target = output_dir / "result.txt"

            result = execute_skill_script(
                str(skills_root),
                skill,
                "scripts/check.py",
                args=[str(target)],
                input_payload={"value": "ok"},
                output_directory=str(output_dir),
                allowed_output_root=str(output_dir),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["returncode"], 0)
            self.assertIn("validated", result["stdout"])
            self.assertEqual(target.read_text(encoding="utf-8"), "ok")
            self.assertGreaterEqual(result["duration_ms"], 0)

    def test_execute_skill_script_rejects_escape_install_and_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            script_dir = skill_dir / "scripts"
            script_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: Demo\ndescription: Demo\n---\n", encoding="utf-8")
            timeout_script = script_dir / "slow.py"
            timeout_script.write_text("import time; time.sleep(1)\n", encoding="utf-8")
            install_script = script_dir / "install.py"
            install_script.write_text("print('must not run')\n", encoding="utf-8")
            manifest = [
                {"path": "scripts/slow.py", "kind": "script", "sha256": hashlib.sha256(timeout_script.read_bytes()).hexdigest()},
                {"path": "scripts/install.py", "kind": "script", "sha256": hashlib.sha256(install_script.read_bytes()).hexdigest()},
            ]
            skill = {"skill_id": "skill_demo", "source_path": "demo/SKILL.md", "resource_manifest": manifest}

            timeout_result = execute_skill_script(
                str(skills_root), skill, "scripts/slow.py", timeout_seconds=0.05,
                output_directory=str(skill_dir / "out"), allowed_output_root=str(skill_dir),
            )
            self.assertEqual(timeout_result["status"], "timed_out")
            self.assertIn("超时", timeout_result["error"])

            install_result = execute_skill_script(
                str(skills_root), skill, "scripts/install.py",
                output_directory=str(skill_dir / "out"), allowed_output_root=str(skill_dir),
            )
            self.assertEqual(install_result["status"], "rejected")
            self.assertIn("安装", install_result["error"])

            escape_result = execute_skill_script(
                str(skills_root), skill, "../outside.py",
                output_directory=str(skill_dir / "out"), allowed_output_root=str(skill_dir),
            )
            self.assertEqual(escape_result["status"], "rejected")
            self.assertIn("Skill", escape_result["error"])

    def test_execute_skill_script_caps_captured_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            script = skill_dir / "scripts" / "verbose.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('x' * 10000)\n", encoding="utf-8")
            (skill_dir / "SKILL.md").write_text("---\nname: Demo\ndescription: Demo\n---\n", encoding="utf-8")
            skill = {
                "skill_id": "skill_demo",
                "source_path": "demo/SKILL.md",
                "resource_manifest": [{
                    "path": "scripts/verbose.py",
                    "kind": "script",
                    "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                }],
            }

            result = execute_skill_script(
                str(skills_root), skill, "scripts/verbose.py",
                output_directory=str(skill_dir / "out"), allowed_output_root=str(skill_dir),
                max_output_bytes=32,
            )

            self.assertEqual(result["status"], "completed")
            self.assertLessEqual(len(result["stdout"].encode("utf-8")), 32)
            self.assertTrue(result["output_truncated"])


if __name__ == "__main__":
    unittest.main()
