import tempfile
import unittest
from pathlib import Path

from core.learning_store import LearningStore
from core.project_skills import discover_project_skills
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class ProjectSkillTests(unittest.TestCase):
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
                self.assertEqual(skill["title"], "论文组会汇报")
                self.assertEqual(skill["steps"], ["提取论文结论", "核对引用", "生成组会大纲"])
                self.assertEqual(store.sync_project_skill(candidate), "unchanged")
                skill_file.write_text(skill_file.read_text(encoding="utf-8") + "4. 输出待确认项\n", encoding="utf-8")
                self.assertEqual(store.sync_project_skill(discover_project_skills(workspace.skills_path)[0]), "updated")
                self.assertEqual(store.list_skills()[0]["version"], 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
