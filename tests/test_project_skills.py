import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.learning_store import LearningStore
from core.mcp_server import sync_project_skills
from core.project_catalog import ProjectCatalog
from core.project_skills import compile_local_skill_adapter, discover_project_skills
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class ProjectSkillTests(unittest.TestCase):
    def test_compiled_adapter_replace_failure_preserves_previous_adapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "replace-failure"
            skill_dir.mkdir()
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "---\nname: Replace Failure\ndescription: Keep the old adapter\n---\n1. Verify\n",
                encoding="utf-8",
            )
            adapter = skill_dir / "souldrive_skill.json"
            original = '{"schema_version":"old"}\n'
            adapter.write_text(original, encoding="utf-8")

            with patch("core.atomic_file.os.replace", side_effect=OSError("replace denied")):
                with self.assertRaises(OSError):
                    compile_local_skill_adapter(skill_file)

            self.assertEqual(adapter.read_text(encoding="utf-8"), original)
            self.assertEqual(list(skill_dir.glob(".souldrive_skill.json.*.tmp")), [])

    def test_identical_skill_copies_are_discovered_once_from_canonical_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir)
            content = "---\nname: test-driven-development\ndescription: Test first\n---\n1. Write a test\n"
            for directory in ("tdd_acceptance", "test-driven-development"):
                skill_file = skills_path / directory / "SKILL.md"
                skill_file.parent.mkdir()
                skill_file.write_text(content, encoding="utf-8")

            [skill] = discover_project_skills(str(skills_path))

            self.assertEqual(skill["source_path"], "test-driven-development/SKILL.md")
            self.assertEqual(skill["duplicate_source_paths"], ["tdd_acceptance/SKILL.md"])

    def test_project_skill_is_discovered_imported_and_updated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace(temp_dir).ensure()
            initialize_keystore(workspace, "test-passphrase")
            skill_file = Path(workspace.skills_path) / "paper-summary" / "SKILL.md"
            skill_file.parent.mkdir()
            skill_file.write_text("---\nname: 论文组会汇报\ndescription: 汇总论文并生成可引用的组会大纲\n---\n1. 提取论文结论\n2. 核对引用\n3. 生成组会大纲\n", encoding="utf-8")
            [candidate] = discover_project_skills(workspace.skills_path)
            store = LearningStore(workspace, unlock_keystore(workspace, "test-passphrase"))
            try:
                self.assertEqual(store.sync_project_skill(candidate), "imported")
                [skill] = store.list_skills()
                self.assertEqual(skill["scope"], "global")
                self.assertIsNone(skill["project_id"])
                self.assertEqual(skill["title"], "论文组会汇报")
                self.assertEqual(skill["steps"], ["提取论文结论", "核对引用", "生成组会大纲"])
                self.assertEqual(store.sync_project_skill(candidate), "unchanged")
                skill_file.write_text(skill_file.read_text(encoding="utf-8") + "4. 输出待确认项\n", encoding="utf-8")
                self.assertEqual(store.sync_project_skill(discover_project_skills(workspace.skills_path)[0]), "updated")
                self.assertEqual(store.list_skills()[0]["version"], 2)
            finally:
                store.close()

    def test_project_skill_front_matter_can_scope_procedural_memory_to_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir)
            skill_file = skills_path / "meeting-actions" / "SKILL.md"
            skill_file.parent.mkdir()
            skill_file.write_text(
                "---\n"
                "name: Meeting actions\n"
                "description: Extract decisions and action items\n"
                "workflow_ids: meeting_actions, paper_review\n"
                "---\n"
                "1. Extract decisions\n",
                encoding="utf-8",
            )

            [candidate] = discover_project_skills(str(skills_path))

        self.assertEqual(candidate["workflow_ids"], ["meeting_actions", "paper_review"])

    def test_project_skill_unknown_workflow_scope_is_not_normalized_to_global(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir)
            skill_file = skills_path / "unknown-window" / "SKILL.md"
            skill_file.parent.mkdir()
            skill_file.write_text(
                "---\n"
                "name: Unknown window\n"
                "description: Must never be globally visible\n"
                "workflow_ids: unknown_window\n"
                "---\n"
                "1. Do not expose this process.\n",
                encoding="utf-8",
            )

            [candidate] = discover_project_skills(str(skills_path))

        self.assertEqual(candidate["workflow_ids"], [])
        self.assertTrue(candidate["workflow_scope_invalid"])

    def test_project_root_skills_are_synced_into_the_project_namespace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace(temp_dir).ensure()
            initialize_keystore(workspace, "test-passphrase")
            project_root = Path(temp_dir) / "project-a"
            skill_file = project_root / "skills" / "local-review" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(
                "---\nname: Local review\ndescription: Project-only review\n---\nUse project evidence.\n",
                encoding="utf-8",
            )
            catalog = ProjectCatalog(workspace)
            catalog.create_project("Project A", project_id="project-a", root_path=str(project_root))
            catalog.close()
            store = LearningStore(workspace, unlock_keystore(workspace, "test-passphrase"))
            try:
                summary = sync_project_skills(store, workspace, project_id="project-a")
                self.assertEqual(summary["imported"], 1)
                project_skills = store.list_skills(project_id="project-a", include_global=False)
                self.assertEqual(len(project_skills), 1)
                self.assertEqual(project_skills[0]["scope"], "project")
                self.assertEqual(project_skills[0]["project_id"], "project-a")
                self.assertEqual(store.list_skills(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
