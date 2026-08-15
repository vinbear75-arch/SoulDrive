import json
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
from core.workspace import SoulDriveWorkspace


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
        with sqlite3.connect(db_path) as conn:
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


if __name__ == "__main__":
    unittest.main()
