import json
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from core.presentation_service import (
    PresentationService,
    build_extractable_presentation_plan,
    collect_indexed_evidence,
    validate_presentation_request,
    verify_pptx,
)
from core.openxml_presentation import render_presentation
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class PresentationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()

    def test_request_requires_selected_indexed_document_names(self):
        request = validate_presentation_request(
            {
                "source_filenames": ["A方案.pdf", "A方案.pdf", "B政策.pdf"],
                "audience": "government",
                "max_slides": 20,
                "open_in_wps": False,
            }
        )

        self.assertEqual(request["source_filenames"], ["A方案.pdf", "B政策.pdf"])
        self.assertEqual(request["max_slides"], 16)

    def test_collects_only_selected_parent_documents_and_builds_source_bound_plan(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    (
                        "p1",
                        "本项目建设统一数据平台，采用分级授权和本地加密机制。项目分三个阶段实施。",
                        json.dumps({"source_filename": "A方案.pdf", "section": "总体方案", "page_number": 3}),
                    ),
                    (
                        "p2",
                        "政策要求建立数据分类分级制度，并明确安全责任和监督机制。",
                        json.dumps({"source_filename": "B政策.pdf", "section": "安全要求", "page_number": 8}),
                    ),
                    (
                        "p3",
                        "不应进入结果的其他材料。",
                        json.dumps({"source_filename": "C噪声.pdf", "page_number": 1}),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        records = collect_indexed_evidence(self.workspace, None, ["A方案.pdf", "B政策.pdf"])
        plan = build_extractable_presentation_plan(
            records,
            {
                "source_filenames": ["A方案.pdf", "B政策.pdf"],
                "title": "数据平台项目汇报",
                "audience": "government",
                "max_slides": 8,
                "open_in_wps": False,
            },
        )

        self.assertEqual({item["source_filename"] for item in records}, {"A方案.pdf", "B政策.pdf"})
        self.assertLessEqual(len(plan["slides"]), 8)
        self.assertEqual(plan["slides"][0]["kind"], "cover")
        self.assertEqual(plan["slides"][-1]["kind"], "sources")
        self.assertNotIn("C噪声.pdf", json.dumps(plan, ensure_ascii=False))

    def test_parent_index_fills_sources_missing_from_vector_index(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                ("p1", "父文档索引仍可用于生成汇报。", json.dumps({"source_filename": "仅父索引.md", "page_number": 1})),
            )
            conn.commit()
        finally:
            conn.close()
        secure_path = Path(self.workspace.secure_vector_store_path)
        secure_path.parent.mkdir(parents=True, exist_ok=True)
        secure_path.touch()

        class EmptyVectorStore:
            def __init__(self, *_args, **_kwargs):
                self.collection = self

            def get(self, **_kwargs):
                return {"metadatas": []}

            def close(self):
                pass

        with patch("core.presentation_service.SecureVectorStore", EmptyVectorStore):
            records = collect_indexed_evidence(self.workspace, object(), ["仅父索引.md"])

        self.assertEqual([record["source_filename"] for record in records], ["仅父索引.md"])

    def test_verify_pptx_checks_openxml_slide_count(self):
        path = Path(self.temp_dir.name) / "test.pptx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            archive.writestr("ppt/slides/slide1.xml", "x" * 9000)

        report = verify_pptx(path, 1)

        self.assertTrue(report["valid"])
        self.assertEqual(report["slide_count"], 1)

    def test_openxml_renderer_writes_editable_utf8_presentation(self):
        path = Path(self.temp_dir.name) / "中文汇报.pptx"
        plan = {
            "title": "数据平台项目汇报",
            "generated_at_label": "2026-08-14",
            "slides": [
                {
                    "kind": "cover",
                    "eyebrow": "项目方案",
                    "title": "数据平台项目汇报",
                    "subtitle": "建设目标与实施路径",
                },
                {
                    "kind": "content",
                    "title": "总体方案",
                    "points": ["建立统一数据平台", "实施分类分级保护"],
                    "sources": ["A方案.pdf", "B政策.pdf"],
                    "footer": "基于已入库文档",
                },
            ],
        }

        result = render_presentation(plan, path)

        self.assertEqual(result["renderer"], "SoulDrive OpenXML")
        self.assertTrue(verify_pptx(path, 2)["valid"])
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            slide_text = archive.read("ppt/slides/slide2.xml").decode("utf-8")
            relationships = archive.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        self.assertIn("总体方案", slide_text)
        self.assertIn("A方案.pdf", slide_text)
        self.assertIn("ppt/slideMasters/slideMaster1.xml", names)
        self.assertIn('Target="slides/slide2.xml"', relationships)

    def test_service_uses_openxml_renderer_by_default(self):
        service = PresentationService(self.workspace, None)
        inputs = {
            "task_id": "task_12345678",
            "title": "测试汇报",
            "source_filenames": ["A方案.pdf"],
        }
        outputs = {
            "build_outline": {
                "title": "测试汇报",
                "generated_at_label": "2026-08-14",
                "slides": [
                    {"kind": "cover", "title": "测试汇报"},
                    {"kind": "sources", "title": "参考文档", "sources": ["A方案.pdf"]},
                ],
            }
        }

        with patch("core.presentation_service.subprocess.run") as com_renderer:
            result = service._render_pptx_step(inputs, outputs)

        self.assertEqual(result["renderer"], "SoulDrive OpenXML")
        com_renderer.assert_not_called()
        self.assertTrue(verify_pptx(Path(result["output_path"]), 2)["valid"])

    def test_run_result_reports_extractable_outline_evidence_and_timing(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                ("p1", "系统通过本地证据链生成科研汇报，并保留文档来源。", json.dumps({"source_filename": "A方案.pdf"})),
            )
            conn.commit()
        finally:
            conn.close()
        service = PresentationService(self.workspace, keys)
        task = service.create_task({"source_filenames": ["A方案.pdf"], "open_in_wps": False})
        result = service.run_task(task["task_id"])["result"]

        self.assertEqual(result["outline_source"], "extractive")
        self.assertEqual(result["evidence_metrics"]["evidence_slide_coverage"], 1.0)
        self.assertEqual(result["evidence_metrics"]["selected_source_coverage"], 1.0)
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_project_presentation_records_versions_plan_and_completion_provenance(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("论文项目", project_id="paper")
            asset_id = catalog.create_asset("paper", "A 方案", "document", asset_id="asset-a")
            version_id = catalog.create_asset_version(asset_id, "hash-a", "A方案.pdf", version_id="version-a")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "p1",
                    "系统保留来源版本并生成可追溯汇报。",
                    json.dumps({
                        "source_filename": "A方案.pdf",
                        "asset_version_id": version_id,
                        "asset_id": asset_id,
                        "section": "方案",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        task = service.create_task({
            "project_id": "paper",
            "source_filenames": ["A方案.pdf"],
            "open_in_wps": False,
            "title": "方案汇报",
        })
        artifact_id = task["inputs"]["artifact_id"]
        result = service.run_task(task["task_id"])
        artifact = ProjectCatalog(self.workspace)
        try:
            stored = artifact.get_artifact(artifact_id)
        finally:
            artifact.close()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["artifact_id"], artifact_id)
        self.assertEqual(result["result"]["source_version_ids"], [version_id])
        self.assertEqual(stored["source_version_ids"], [version_id])
        self.assertEqual(stored["plan"]["task_id"], task["task_id"])
        self.assertEqual(stored["confirmation_record"]["generation_status"], "completed")
        self.assertIn("生成结果", stored["change_summary"])

    def test_custom_outline_generator_is_identified_without_claiming_ollama(self):
        service = PresentationService(
            self.workspace,
            None,
            outline_generator=lambda _records, _inputs: {
                "title": "自定义",
                "slides": [{"kind": "cover", "title": "自定义"}, {"kind": "sources", "title": "来源", "sources": ["A方案.pdf"]}],
            },
        )
        result = service._build_outline_step({"source_filenames": ["A方案.pdf"], "max_slides": 4}, {"collect_evidence": {"records": []}})

        self.assertEqual(result["outline_source"], "custom_generator")

    def test_template_skill_is_bound_to_task_and_changes_safe_palette(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        service = PresentationService(
            self.workspace,
            keys,
            style_skill_lookup=lambda skill_id: {
                "skill_id": skill_id,
                "title": "学院蓝组会模板",
                "presentation_style": {"primary": "123456", "accent": "FEDCBA"},
            } if skill_id == "skill_template" else None,
        )
        task = service.create_task({
            "source_filenames": ["A方案.pdf"],
            "style_skill_id": "skill_template",
        })
        self.assertEqual(task["inputs"]["style_skill"]["skill_id"], "skill_template")
        plan = build_extractable_presentation_plan([], task["inputs"])
        self.assertEqual(plan["style"]["primary"], "123456")
        self.assertEqual(plan["style"]["accent"], "FEDCBA")

    def test_unknown_template_skill_is_rejected(self):
        service = PresentationService(self.workspace, None, style_skill_lookup=lambda _skill_id: None)
        with self.assertRaisesRegex(ValueError, "风格 Skill"):
            service.create_task({"source_filenames": ["A方案.pdf"], "style_skill_id": "missing"})

    def test_skill_without_declarative_style_uses_safe_default(self):
        skill = {
            "skill_id": "skill_frontend",
            "title": "frontend-slides",
            "content": "Create animation-rich HTML presentations from scratch.",
        }
        plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
                "style_skill": skill,
            },
        )

        self.assertEqual(plan["style"]["primary"], "173F3B")
        self.assertEqual(plan["style"]["content_variant"], "evidence_split")
        self.assertFalse(plan["skill_application"]["style_applied"])
        self.assertTrue(plan["skill_application"]["instructions_applied"])
        self.assertEqual(plan["skill_application"]["skill_id"], "skill_frontend")
        self.assertEqual(plan["presentation_contract"]["adaptation"], "native_pptx")

    def test_selected_skill_changes_deterministic_business_contract(self):
        records = [
            {"source_filename": "A.pdf", "content": "第一条结论。", "section": "结论", "page_number": 1},
        ]
        default_plan = build_extractable_presentation_plan(records, {
            "source_filenames": ["A.pdf"], "title": "汇报", "audience": "research", "max_slides": 6,
        })
        skill_plan = build_extractable_presentation_plan(records, {
            "source_filenames": ["A.pdf"], "title": "汇报", "audience": "research", "max_slides": 6,
            "style_skill": {"skill_id": "pptx", "title": "pptx", "content": "Create editable PPTX slides."},
        })
        self.assertEqual(default_plan["presentation_contract"]["adaptation"], "system_default")
        self.assertEqual(skill_plan["presentation_contract"]["adaptation"], "native_pptx")
        self.assertNotEqual(default_plan["slides"][0]["subtitle"], skill_plan["slides"][0]["subtitle"])
        self.assertLessEqual(len(skill_plan["slides"][1]["points"]), 4)

    def test_selected_skill_validation_script_is_ignored(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        skill_dir = Path(self.workspace.skills_path) / "ppt-check"
        script = skill_dir / "scripts" / "validate.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "import json, pathlib, sys\n"
            "pptx = pathlib.Path(sys.argv[1])\n"
            "assert pptx.is_file()\n"
            "print(json.dumps({'checked': pptx.name}))\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: PPT Check\ndescription: Validate PPTX\n---\n",
            encoding="utf-8",
        )
        skill = {
            "skill_id": "skill_ppt_check",
            "title": "PPT Check",
            "source_path": "ppt-check/SKILL.md",
            "resource_manifest": [{
                "path": "scripts/validate.py",
                "kind": "script",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }],
        }
        service = PresentationService(self.workspace, keys)
        output_path = Path(self.workspace.presentations_path) / "input.pptx"
        render_presentation(
            {"title": "输入", "slides": [{"kind": "cover", "title": "输入"}, {"kind": "sources", "title": "来源", "sources": ["A.pdf"]}]},
            output_path,
        )

        result = service._verify_pptx_step(
            {"style_skill": skill},
            {"render_pptx": {"output_path": str(output_path), "planned_slide_count": 2}},
        )

        self.assertTrue(result["valid"])
        self.assertIsNone(result["skill_execution"])
        self.assertTrue(result["skill_gate_passed"])

    def test_failed_selected_skill_validation_script_is_not_executed(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        skill_dir = Path(self.workspace.skills_path) / "ppt-check-fail"
        script = skill_dir / "scripts" / "validate.py"
        script.parent.mkdir(parents=True)
        script.write_text("raise SystemExit('style contract rejected')\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            "---\nname: PPT Check Fail\ndescription: Reject PPTX\n---\n",
            encoding="utf-8",
        )
        skill = {
            "skill_id": "skill_ppt_check_fail",
            "title": "PPT Check Fail",
            "source_path": "ppt-check-fail/SKILL.md",
            "resource_manifest": [{
                "path": "scripts/validate.py",
                "kind": "script",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }],
        }
        service = PresentationService(self.workspace, keys)
        output_path = Path(self.workspace.presentations_path) / "input-fail.pptx"
        render_presentation(
            {"title": "输入", "slides": [{"kind": "cover", "title": "输入"}, {"kind": "sources", "title": "来源", "sources": ["A.pdf"]}]},
            output_path,
        )

        result = service._verify_pptx_step(
            {"style_skill": skill},
            {"render_pptx": {"output_path": str(output_path), "planned_slide_count": 2}},
        )
        self.assertTrue(result["valid"])
        self.assertIsNone(result["skill_execution"])

    def test_declarative_skill_style_changes_layout_and_font(self):
        skill = {
            "skill_id": "skill_frontend",
            "title": "frontend-slides",
            "content": "Create animation-rich HTML presentations from scratch.",
            "presentation_style_source": "presentation_style.json",
            "presentation_style": {
                "primary": "102A43",
                "accent": "F97316",
                "background": "F7F3EA",
                "text": "243B53",
                "surface": "FFFDF7",
                "cover_variant": "editorial_light",
                "content_variant": "split_cards",
                "font_family": "Arial",
            },
            "resource_manifest": [
                {"path": "scripts/export-presentation.ps1", "kind": "script"},
                {"path": "STYLE_PRESETS.md", "kind": "instruction"},
            ],
        }
        plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
                "style_skill": skill,
            },
        )
        default_plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
            },
        )
        styled_path = Path(self.temp_dir.name) / "styled.pptx"
        default_path = Path(self.temp_dir.name) / "default.pptx"
        render_presentation(plan, styled_path)
        render_presentation(default_plan, default_path)

        with zipfile.ZipFile(styled_path) as archive:
            styled_cover = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            styled_content = archive.read("ppt/slides/slide2.xml").decode("utf-8")
        with zipfile.ZipFile(default_path) as archive:
            default_cover = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            default_content = archive.read("ppt/slides/slide2.xml").decode("utf-8")

        self.assertEqual(plan["style"]["content_variant"], "split_cards")
        self.assertTrue(plan["skill_application"]["style_applied"])
        self.assertEqual(plan["skill_application"]["script_count"], 0)
        self.assertIn('typeface="Arial"', styled_cover)
        self.assertIn('val="102A43"', styled_cover)
        self.assertNotEqual(styled_cover, default_cover)
        self.assertNotEqual(styled_content, default_content)


if __name__ == "__main__":
    unittest.main()
