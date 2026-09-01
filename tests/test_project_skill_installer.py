import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.project_skill_installer import (
    SkillInstallError,
    activate_skill_from_inbox,
    approve_skill_from_inbox,
    delete_project_skill_source_path,
    ensure_project_skill_presentation_adapter,
    install_skill_to_inbox,
    list_skill_inbox,
)
from core.project_skills import discover_project_skills


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ProjectSkillInstallerTests(unittest.TestCase):
    def test_source_cleanup_accepts_legacy_skills_root_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_root = skills_root / "legacy-skill"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\nname: Legacy\ndescription: test\n---\n",
                encoding="utf-8",
            )

            result = delete_project_skill_source_path(
                str(skills_root),
                "skills/legacy-skill/SKILL.md",
            )

            self.assertEqual(result["status"], "deleted")
            self.assertFalse(skill_root.exists())

    def test_source_cleanup_never_treats_root_prefix_as_delete_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skills_root.mkdir()
            with self.assertRaisesRegex(SkillInstallError, "must name a Skill"):
                delete_project_skill_source_path(str(skills_root), "skills")

    def test_npx_skills_command_selects_project_skill_path(self):
        api_root = "https://api.github.com/repos/affaan-m/ECC/contents/skills/frontend-slides?ref=main"
        api_file = "https://api.github.com/repos/affaan-m/ECC/contents/skills/frontend-slides/SKILL.md?ref=main"
        raw_file = "https://raw.githubusercontent.com/affaan-m/ECC/main/skills/frontend-slides/SKILL.md"
        responses = {
            api_root: json.dumps([{"type": "file", "path": "skills/frontend-slides/SKILL.md", "url": api_file}]).encode(),
            api_file: json.dumps({"type": "file", "path": "skills/frontend-slides/SKILL.md", "download_url": raw_file}).encode(),
            raw_file: b"---\nname: Frontend Slides\ndescription: Build browser presentation slides\n---\n",
        }

        def opener(request, timeout=0):
            return _Response(responses[request.full_url])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = install_skill_to_inbox(
                str(Path(temp_dir) / "skills"),
                "npx skills add https://github.com/affaan-m/ECC --skill frontend-slides",
                opener=opener,
            )
            self.assertEqual(result["status"], "pending_review")
            self.assertEqual(result["skill_title"], "Frontend Slides")

    def test_npx_add_skill_alias_is_supported(self):
        api_root = "https://api.github.com/repos/affaan-m/ECC/contents/skills/frontend-slides?ref=main"
        api_file = "https://api.github.com/repos/affaan-m/ECC/contents/skills/frontend-slides/SKILL.md?ref=main"
        raw_file = "https://raw.githubusercontent.com/affaan-m/ECC/main/skills/frontend-slides/SKILL.md"
        responses = {
            api_root: json.dumps([{"type": "file", "path": "skills/frontend-slides/SKILL.md", "url": api_file}]).encode(),
            api_file: json.dumps({"type": "file", "path": "skills/frontend-slides/SKILL.md", "download_url": raw_file}).encode(),
            raw_file: b"---\nname: Frontend Slides\ndescription: Build browser presentation slides\n---\n",
        }

        def opener(request, timeout=0):
            return _Response(responses[request.full_url])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = install_skill_to_inbox(
                str(Path(temp_dir) / "skills"),
                "npx add-skill https://github.com/affaan-m/ECC --skill frontend-slides",
                opener=opener,
            )
            self.assertEqual(result["status"], "pending_review")
            self.assertEqual(result["skill_name"], "Frontend-Slides")

    def test_unicode_custom_name_is_normalized_to_installable_ascii_slug(self):
        api_root = "https://api.github.com/repos/acme/research/contents/skills/demo?ref=main"
        api_file = "https://api.github.com/repos/acme/research/contents/skills/demo/SKILL.md?ref=main"
        raw_file = "https://raw.githubusercontent.com/acme/research/main/skills/demo/SKILL.md"
        responses = {
            api_root: json.dumps([{"type": "file", "path": "skills/demo/SKILL.md", "url": api_file}]).encode(),
            api_file: json.dumps({"type": "file", "path": "skills/demo/SKILL.md", "download_url": raw_file}).encode(),
            raw_file: b"---\nname: Demo\ndescription: Demo\n---\n",
        }

        def opener(request, timeout=0):
            return _Response(responses[request.full_url])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = install_skill_to_inbox(
                str(Path(temp_dir) / "skills"),
                "acme/research/skills/demo",
                skill_name="证据不足时的安全回答模板",
                opener=opener,
            )
            self.assertRegex(result["skill_name"], r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
            self.assertTrue((Path(result["path"]) / "SKILL.md").is_file())

    def test_github_directory_is_downloaded_to_inbox_until_approved(self):
        api_root = "https://api.github.com/repos/acme/research/contents/skills/meeting?ref=main"
        api_file = "https://api.github.com/repos/acme/research/contents/skills/meeting/SKILL.md?ref=main"
        raw_file = "https://raw.githubusercontent.com/acme/research/main/skills/meeting/SKILL.md"
        responses = {
            api_root: json.dumps([{"type": "file", "path": "skills/meeting/SKILL.md", "url": api_file}]).encode(),
            api_file: json.dumps({"type": "file", "path": "skills/meeting/SKILL.md", "download_url": raw_file}).encode(),
            raw_file: b"---\nname: Meeting Notes\ndescription: Extract decisions and action items\n---\n1. Extract decisions\n2. Verify sources\n",
        }

        def opener(request, timeout=0):
            self.assertEqual(timeout, 20)
            return _Response(responses[request.full_url])

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = str(Path(temp_dir) / "skills")
            result = install_skill_to_inbox(skills_path, "acme/research/skills/meeting", opener=opener)
            self.assertEqual(result["status"], "pending_review")
            self.assertEqual(result["skill_name"], "Meeting-Notes")
            self.assertEqual(len(list_skill_inbox(skills_path)), 1)
            approved = approve_skill_from_inbox(skills_path, result["skill_name"])
            self.assertEqual(approved["status"], "pending_review")
            activate_skill_from_inbox(skills_path, result["skill_name"])
            [skill] = discover_project_skills(skills_path)
            self.assertEqual(skill["title"], "Meeting Notes")

            inbox_path = Path(skills_path) / ".inbox"
            inbox_path.mkdir(exist_ok=True)
            shutil.copytree(Path(skills_path) / result["skill_name"], inbox_path / result["skill_name"])
            self.assertEqual(list_skill_inbox(skills_path), [])

    def test_approved_slide_preset_gets_compiled_adapter_without_executable_resources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir) / "skills"
            inbox_skill = skills_path / ".inbox" / "slide-skill"
            (inbox_skill / "scripts").mkdir(parents=True)
            (inbox_skill / "SKILL.md").write_text(
                "---\nname: Slide Skill\ndescription: Build a presentation\n---\nUse the style presets.\n",
                encoding="utf-8",
            )
            (inbox_skill / "STYLE_PRESETS.md").write_text("# Presets\n", encoding="utf-8")
            (inbox_skill / "scripts" / "export.ps1").write_text("Write-Output test\n", encoding="utf-8")

            approved = approve_skill_from_inbox(str(skills_path), "slide-skill")
            self.assertFalse(approved["presentation_adapter_created"])
            self.assertEqual(approved["compatibility_status"], "pending_review")
            activate_skill_from_inbox(str(skills_path), "slide-skill")
            adapter = skills_path / "slide-skill" / "presentation_style.json"
            self.assertFalse(adapter.is_file())

            [skill] = discover_project_skills(str(skills_path))
            self.assertEqual(skill["presentation_style"], {})
            resource_kinds = {item["kind"] for item in skill["resource_manifest"]}
            self.assertIn("instruction", resource_kinds)
            self.assertNotIn("script", resource_kinds)
            self.assertNotIn("presentation_style_source", skill)
            self.assertEqual(skill["target"], "presentation")
            self.assertEqual(skill["compatibility_status"], "verified")
            self.assertTrue((skills_path / "slide-skill" / "souldrive_skill.json").is_file())

    def test_explicit_style_adapter_creation_is_idempotent_and_preserves_edits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir) / "skills"
            skill_dir = skills_path / "custom-slides"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Custom Slides\ndescription: Build a local deck\n---\n",
                encoding="utf-8",
            )

            first = ensure_project_skill_presentation_adapter(
                str(skills_path), "custom-slides/SKILL.md"
            )
            adapter = skill_dir / "presentation_style.json"
            adapter.write_text('{"primary":"ABCDEF"}\n', encoding="utf-8")
            second = ensure_project_skill_presentation_adapter(
                str(skills_path), "skills/custom-slides/SKILL.md"
            )

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(json.loads(adapter.read_text(encoding="utf-8"))["primary"], "ABCDEF")

    def test_explicit_style_adapter_has_a_real_starter_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir) / "skills"
            skill_dir = skills_path / "custom-slides"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Custom Slides\ndescription: Build a local deck\n---\n",
                encoding="utf-8",
            )

            ensure_project_skill_presentation_adapter(str(skills_path), "custom-slides/SKILL.md")
            payload = json.loads((skill_dir / "presentation_style.json").read_text(encoding="utf-8"))

            self.assertEqual(payload["content_variant"], "split_cards")
            self.assertEqual(payload["cover_variant"], "editorial_light")
            self.assertNotEqual(payload["primary"], "173F3B")

    def test_remote_source_must_use_github_and_skill_front_matter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SkillInstallError, "HTTPS GitHub"):
                install_skill_to_inbox(str(Path(temp_dir) / "skills"), "https://example.com/SKILL.md")

            def opener(request, timeout=0):
                return _Response(b"---\nname: Missing description\n---\n")

            with self.assertRaisesRegex(SkillInstallError, "name and description"):
                install_skill_to_inbox(
                    str(Path(temp_dir) / "skills"),
                    "https://raw.githubusercontent.com/acme/research/main/SKILL.md",
                    opener=opener,
                )

    def test_delete_project_skill_source_path_supports_unicode_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir) / "skills"
            target_dir = skills_path / "证据不足时的安全回答模板"
            target_dir.mkdir(parents=True)
            (target_dir / "SKILL.md").write_text("---\nname: Demo\ndescription: Demo\n---\n", encoding="utf-8")

            result = delete_project_skill_source_path(str(skills_path), "证据不足时的安全回答模板/SKILL.md")
            self.assertEqual(result["status"], "deleted")
            self.assertFalse(target_dir.exists())

    def test_delete_root_skill_file_does_not_delete_skills_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_path = Path(temp_dir) / "skills"
            skills_path.mkdir(parents=True)
            root_skill = skills_path / "SKILL.md"
            root_skill.write_text("---\nname: Demo\ndescription: Demo\n---\n", encoding="utf-8")

            delete_project_skill_source_path(str(skills_path), "SKILL.md")
            self.assertTrue(skills_path.exists())
            self.assertFalse(root_skill.exists())


if __name__ == "__main__":
    unittest.main()
