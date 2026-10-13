import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.learning_store import LearningStore
from core.openxml_presentation import render_presentation
from core.presentation_service import PresentationService
from core.project_skill_installer import install_skill_to_inbox
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _RecordingRag:
    def __init__(self):
        self.learning_context = None
        self.interaction_context = None

    def generate_response_stream(
        self,
        query,
        top_k=3,
        trace_id=None,
        learning_context=None,
        interaction_context=None,
    ):
        _ = (query, top_k, trace_id)
        self.learning_context = learning_context
        self.interaction_context = interaction_context
        yield "已按审核通过的 Skill 处理。"


class SkillFullE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()
        initialize_keystore(self.workspace, "correct horse battery staple")
        self.keys = unlock_keystore(self.workspace, "correct horse battery staple")
        set_workspace_keys(self.workspace.root_path, self.keys)
        self.addCleanup(clear_workspace_keys)
        self.addCleanup(mcp_server._close_session_store)
        self.original_kb = mcp_server.kb
        self.original_rag = mcp_server.rag
        self.original_loaded_workspace_path = mcp_server.loaded_workspace_path
        self.addCleanup(self._restore_runtime)
        self.client = TestClient(mcp_server.app)
        self.env_patch = patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _restore_runtime(self):
        mcp_server.kb = self.original_kb
        mcp_server.rag = self.original_rag
        mcp_server.loaded_workspace_path = self.original_loaded_workspace_path

    def _state(self):
        return {
            "locked": False,
            "software_unlocked": True,
            "workspace_path": self.workspace.root_path,
            "auth_level": "HARDWARE_PLUS_PASSWORD",
        }

    def _headers(self):
        return {"X-SoulDrive-Token": "test-token"}

    def _remote_skill_responses(self):
        base = "https://api.github.com/repos/acme/research/contents/skills/review-deck?ref=main"
        api_urls = {
            "SKILL.md": f"{base}/SKILL.md",
            "presentation_style.json": f"{base}/presentation_style.json",
            "scripts/validate.py": f"{base}/scripts/validate.py",
        }
        raw_urls = {
            name: "https://raw.githubusercontent.com/acme/research/main/skills/review-deck/" + name
            for name in api_urls
        }
        skill = (
            "---\n"
            "name: Reviewed Slide Deck\n"
            "description: Build editable evidence-first PPTX and validate the result.\n"
            "---\n"
            "Use evidence-first slide structure, preserve citations, and run the validation script.\n"
        ).encode("utf-8")
        style = json.dumps(
            {
                "primary": "123456",
                "accent": "ABCDEF",
                "background": "F0F0F0",
                "text": "101010",
                "surface": "FFFFFF",
                "cover_variant": "editorial_light",
                "content_variant": "split_cards",
                "font_family": "Arial",
            }
        ).encode("utf-8")
        validator = (
            "import json, pathlib, sys, zipfile\n"
            "pptx = pathlib.Path(sys.argv[1])\n"
            "assert pptx.is_file()\n"
            "with zipfile.ZipFile(pptx) as archive:\n"
            "    assert 'ppt/slides/slide1.xml' in archive.namelist()\n"
            "print(json.dumps({'checked': pptx.name}))\n"
        ).encode("utf-8")
        payloads = {
            base: json.dumps(
                [
                    {"type": "file", "path": "skills/review-deck/SKILL.md", "url": api_urls["SKILL.md"]},
                    {
                        "type": "file",
                        "path": "skills/review-deck/presentation_style.json",
                        "url": api_urls["presentation_style.json"],
                    },
                    {
                        "type": "file",
                        "path": "skills/review-deck/scripts/validate.py",
                        "url": api_urls["scripts/validate.py"],
                    },
                ]
            ).encode("utf-8"),
            api_urls["SKILL.md"]: json.dumps(
                {"type": "file", "path": "skills/review-deck/SKILL.md", "download_url": raw_urls["SKILL.md"]}
            ).encode("utf-8"),
            api_urls["presentation_style.json"]: json.dumps(
                {
                    "type": "file",
                    "path": "skills/review-deck/presentation_style.json",
                    "download_url": raw_urls["presentation_style.json"],
                }
            ).encode("utf-8"),
            api_urls["scripts/validate.py"]: json.dumps(
                {
                    "type": "file",
                    "path": "skills/review-deck/scripts/validate.py",
                    "download_url": raw_urls["scripts/validate.py"],
                }
            ).encode("utf-8"),
            raw_urls["SKILL.md"]: skill,
            raw_urls["presentation_style.json"]: style,
            raw_urls["scripts/validate.py"]: validator,
        }
        return payloads

    def test_download_approve_and_ppt_use_the_compiled_presentation_skill(self):
        payloads = self._remote_skill_responses()

        def opener(request, timeout=0):
            _ = timeout
            try:
                return _Response(payloads[request.full_url])
            except KeyError as exc:
                raise AssertionError(f"unexpected Skill URL: {request.full_url}") from exc

        def download_for_api(skills_path, source, skill_name=None, target="conversation"):
            return install_skill_to_inbox(skills_path, source, skill_name, target, opener=opener)

        state = self._state()
        project_id = "skill-e2e-project"
        session_id = "skill-e2e-session"
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "install_skill_to_inbox",
            side_effect=download_for_api,
        ):
            project = self.client.post(
                "/projects",
                json={"name": "Skill 闭环项目", "project_id": project_id},
                headers=self._headers(),
            )
            downloaded = self.client.post(
                "/learning/skills/install",
                json={"source": "https://github.com/acme/research/tree/main/skills/review-deck", "target": "presentation"},
                headers=self._headers(),
            )
            skill_name = downloaded.json()["skill_name"]
            approved = self.client.post(
                f"/learning/skills/inbox/{skill_name}/approve",
                headers=self._headers(),
            )
            self.assertEqual(approved.json()["status"], "pending_review")
            activated = self.client.post(
                f"/learning/skills/inbox/{skill_name}/activate",
                headers=self._headers(),
            )
            self.assertEqual(activated.status_code, 200)
            listed = self.client.get("/learning/skills", headers=self._headers())

        self.assertEqual(project.status_code, 200)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.json()["status"], "pending_review")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        installed_root = Path(self.workspace.skills_path) / skill_name
        self.assertTrue((installed_root / "SKILL.md").is_file())
        self.assertTrue((installed_root / "scripts" / "validate.py").is_file())
        skills = listed.json()["skills"]
        [skill] = [item for item in skills if item.get("source_path") == f"{skill_name}/SKILL.md"]
        skill_id = skill["skill_id"]
        self.assertEqual(skill["target"], "presentation")
        self.assertEqual(skill["compatibility_status"], "verified")
        self.assertTrue((installed_root / "souldrive_skill.json").is_file())
        self.assertEqual(skill["presentation_style"]["primary"], "123456")
        self.assertFalse(any(item["path"] == "scripts/validate.py" for item in skill["resource_manifest"]))

        seed_pptx = Path(self.workspace.presentations_path) / "seed.pptx"
        render_presentation(
            {
                "title": "seed",
                "slides": [
                    {"kind": "cover", "title": "seed"},
                    {"kind": "sources", "title": "来源", "sources": ["RAG.pdf"]},
                ],
            },
            seed_pptx,
        )
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            script_run = self.client.post(
                f"/learning/skills/{skill_id}/runs",
                json={
                    "query": "校验刚生成的 PPTX",
                    "mode": "execute",
                    "script_path": "scripts/validate.py",
                    "args": [str(seed_pptx)],
                },
                headers=self._headers(),
            )

        self.assertEqual(script_run.status_code, 410)
        self.assertIn("通用 Skill 脚本执行已停用", script_run.json()["error"])

        evidence = [
            {
                "parent_id": "parent-rag",
                "source_filename": "RAG.pdf",
                "document_title": "RAG",
                "asset_id": "",
                "asset_version_id": "",
                "catalog_file_ref": "",
                "section": "核心思想",
                "page_number": 1,
                "parent_index": 0,
                "content": "RAG 先检索外部知识，再让生成模型基于证据回答问题。",
            }
        ]
        with patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.dict(
            os.environ,
            {"SOULDRIVE_PRESENTATION_RENDERER": "openxml"},
            clear=False,
        ), patch(
            "core.presentation_service.collect_indexed_evidence",
            return_value=evidence,
        ):
            service = PresentationService(
                self.workspace,
                self.keys,
                style_skill_lookup=mcp_server._presentation_style_skill,
            )
            plan_task = service.create_plan(
                {
                    "source_filenames": ["RAG.pdf"],
                    "title": "Skill 证据汇报",
                    "open_in_wps": False,
                    "style_skill_id": skill_id,
                }
            )
            planned = service.run_task(plan_task["task_id"])
            generation_task = service.confirm_plan(
                plan_task["task_id"],
                planned["result"]["plan_digest"],
            )
            completed = service.run_task(generation_task["task_id"])
            public_task = service.get_task(generation_task["task_id"])

        self.assertEqual(completed["status"], "completed")
        result = completed["result"]
        self.assertTrue(result["skill_application"]["instructions_applied"])
        self.assertTrue(result["skill_application"]["style_applied"])
        self.assertIn("structural_qa", result["skill_application"]["applied_rules"])
        self.assertIsNone(result["skill_execution"])
        self.assertTrue(result["skill_gate_passed"])
        self.assertIsNone(public_task["result"]["skill_execution"])
        output_path = Path(result["output_path"])
        self.assertTrue(output_path.is_file())
        with zipfile.ZipFile(output_path) as archive:
            slide_xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            content_xml = archive.read("ppt/slides/slide2.xml").decode("utf-8")
        self.assertIn("123456", slide_xml)
        self.assertIn("ABCDEF", slide_xml)
        self.assertIn("Arial", slide_xml)
        self.assertIn("RAG.pdf", content_xml)

        store = LearningStore(self.workspace, self.keys)
        try:
            runs = [item for item in store.list_skill_runs() if item["skill_id"] == skill_id]
        finally:
            store.close()
        self.assertEqual(len(runs), 0)


if __name__ == "__main__":
    unittest.main()
