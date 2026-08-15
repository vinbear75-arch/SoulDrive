import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docling.datamodel.base_models import ConversionStatus, DoclingComponentType, ErrorItem

from core.document_parser import AdvancedDocumentParser


class FakeProv:
    def __init__(self, page_no):
        self.page_no = page_no


class FakeDoclingItem:
    def __init__(self, text, page_no, label="text", level=1):
        self.text = text
        self.orig = text
        self.prov = [FakeProv(page_no)]
        self.label = label
        self.level = level


class FakeTableItem:
    def __init__(self, markdown, page_no):
        self.prov = [FakeProv(page_no)]
        self.label = "table"
        self._markdown = markdown

    def export_to_markdown(self, doc=None):
        _ = doc
        return self._markdown


class FakeImageItem:
    def __init__(self, text, page_no):
        self.text = text
        self.orig = text
        self.prov = [FakeProv(page_no)]
        self.label = "picture"
        self.level = 1


class FakeDoclingDocument:
    def iterate_items(self):
        return iter(
            [
                (FakeDoclingItem("Transformer Paper", 1, label="section_header", level=1), 1),
                (FakeDoclingItem("Self-attention connects every token on the first page.", 1), 1),
                (FakeTableItem("| Layer | Value |\n|---|---|\n| heads | 8 |", 2), 1),
                (FakeImageItem("Decoder architecture diagram", 2), 1),
                (FakeDoclingItem("The decoder stack is described on the second page.", 2), 1),
            ]
        )

    def export_to_markdown(self):
        return "# Transformer Paper\nSelf-attention fallback text."


class FakeConvertResult:
    document = FakeDoclingDocument()


class FakeConverter:
    def convert(self, pdf_path):
        _ = pdf_path
        return FakeConvertResult()


class FakeFormalDocument:
    def iterate_items(self):
        return iter(
            [
                (FakeDoclingItem("一、总体要求", 1, label="text"), 1),
                (FakeDoclingItem("围绕企业知识库建设明确建设目标和实施范围。", 1, label="text"), 1),
                (FakeDoclingItem("（一）建设目标", 1, label="text"), 1),
                (FakeDoclingItem("支持政策文件、项目方案和技术文档的高质量检索。", 1, label="text"), 1),
            ]
        )

    def export_to_markdown(self):
        return "一、总体要求\n围绕企业知识库建设明确建设目标和实施范围。"


class FakeFormalConvertResult:
    document = FakeFormalDocument()


class FakeFormalConverter:
    def convert(self, pdf_path):
        _ = pdf_path
        return FakeFormalConvertResult()


class FakePolicyDocument:
    def iterate_items(self):
        duplicate = "本办法适用于企业数据处理和安全管理活动。"
        return iter(
            [
                (FakeDoclingItem("数据安全管理办法", 1, label="title", level=1), 1),
                (FakeDoclingItem("某府〔2026〕12号", 1), 1),
                (FakeDoclingItem("2026年4月1日", 1), 1),
                (FakeDoclingItem("第 1 页 共 3 页", 1, label="page_footer"), 1),
                (FakeDoclingItem("目录 第一章 总则 ........ 1", 1, label="document_index"), 1),
                (FakeDoclingItem("第一章 总则", 1), 1),
                (FakeDoclingItem("第一条 为了规范企业数据处理活动，制定本办法。", 1), 1),
                (FakeDoclingItem(duplicate, 1), 1),
                (FakeDoclingItem(duplicate, 1), 1),
            ]
        )

    def export_to_markdown(self):
        return "# 数据安全管理办法\n\n第一条 为了规范企业数据处理活动，制定本办法。"


class FakePolicyConvertResult:
    document = FakePolicyDocument()


class FakePolicyConverter:
    def convert(self, pdf_path):
        _ = pdf_path
        return FakePolicyConvertResult()


class DocumentParserTests(unittest.TestCase):
    def test_parser_uses_bounded_docling_batches_without_local_artifact_override(self):
        with patch.dict(
            "os.environ",
            {},
            clear=True,
        ), patch("core.document_parser.DocumentConverter") as converter:
            AdvancedDocumentParser()

        format_options = converter.call_args.kwargs["format_options"]
        pdf_option = next(iter(format_options.values()))
        pipeline_options = pdf_option.pipeline_options
        self.assertIsNone(pipeline_options.artifacts_path)
        self.assertEqual(pipeline_options.accelerator_options.device.value, "cpu")
        self.assertEqual(pipeline_options.layout_batch_size, 2)
        self.assertEqual(pipeline_options.queue_max_size, 8)

    def test_parser_uses_local_accurate_docling_artifacts_when_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_DOCLING_ARTIFACTS_PATH": temp_dir},
        ), patch("core.document_parser.DocumentConverter") as converter:
            AdvancedDocumentParser()

        format_options = converter.call_args.kwargs["format_options"]
        pdf_option = next(iter(format_options.values()))
        pipeline_options = pdf_option.pipeline_options
        self.assertEqual(Path(pipeline_options.artifacts_path), Path(temp_dir).resolve())
        self.assertEqual(pipeline_options.table_structure_options.mode.value, "accurate")
        self.assertTrue(pipeline_options.table_structure_options.do_cell_matching)
        self.assertEqual(pipeline_options.accelerator_options.device.value, "cpu")
        self.assertEqual(pipeline_options.layout_batch_size, 2)
        self.assertEqual(pipeline_options.ocr_batch_size, 2)
        self.assertEqual(pipeline_options.table_batch_size, 2)
        self.assertEqual(pipeline_options.queue_max_size, 8)

    def test_parse_and_chunk_attaches_page_metadata_from_docling_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=FakeConverter()):
                chunks = AdvancedDocumentParser().parse_and_chunk(str(pdf_path))

        self.assertTrue(chunks)
        self.assertTrue(
            any(chunk.metadata.get("page") == 1 and "Self-attention" in chunk.page_content for chunk in chunks)
        )
        self.assertTrue(
            any(chunk.metadata.get("page") == 2 and "decoder stack" in chunk.page_content for chunk in chunks)
        )
        second_page_chunks = [chunk for chunk in chunks if chunk.metadata.get("page") == 2]
        self.assertTrue(second_page_chunks)
        self.assertEqual(second_page_chunks[0].metadata.get("section"), "Transformer Paper")
        for chunk in chunks:
            self.assertIn(chunk.metadata.get("page_label"), {"1", "2"})

    def test_parse_and_chunk_records_table_and_image_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=FakeConverter()):
                chunks = AdvancedDocumentParser().parse_and_chunk(str(pdf_path))

        table_chunks = [chunk for chunk in chunks if chunk.metadata.get("table_count")]
        image_chunks = [chunk for chunk in chunks if chunk.metadata.get("image_count")]

        self.assertTrue(table_chunks)
        self.assertIn("Layer", table_chunks[0].metadata.get("tables_json", ""))
        self.assertTrue(image_chunks)
        self.assertIn("Decoder architecture diagram", image_chunks[0].metadata.get("image_metadata_json", ""))
        self.assertIn("[图像:", image_chunks[0].page_content)

    def test_parse_failure_exposes_structured_error(self):
        class FailingConverter:
            def convert(self, pdf_path):
                _ = pdf_path
                raise RuntimeError("encrypted document")

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "locked.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=FailingConverter()):
                parser = AdvancedDocumentParser()
                chunks = parser.parse_and_chunk(str(pdf_path))

        self.assertEqual(chunks, [])
        self.assertEqual(parser.last_error["source_filename"], "locked.pdf")
        self.assertEqual(parser.last_error["error_code"], "ENCRYPTED_DOCUMENT")

    def test_partial_conversion_is_rejected_and_failed_pages_are_observable(self):
        class PartialConverter:
            def convert(self, pdf_path):
                _ = pdf_path
                result = FakeConvertResult()
                result.status = ConversionStatus.PARTIAL_SUCCESS
                result.errors = [
                    ErrorItem(
                        component_type=DoclingComponentType.PIPELINE,
                        module_name="StandardPdfPipeline",
                        error_message="Page 40: std::bad_alloc",
                    ),
                    ErrorItem(
                        component_type=DoclingComponentType.PIPELINE,
                        module_name="StandardPdfPipeline",
                        error_message="Page 41: std::bad_alloc",
                    ),
                ]
                return result

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "partial.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=PartialConverter()):
                parser = AdvancedDocumentParser()
                chunks = parser.parse_and_chunk(str(pdf_path))

        self.assertEqual(chunks, [])
        self.assertEqual(parser.last_error["error_code"], "PARTIAL_DOCUMENT_PARSE")
        self.assertEqual(parser.last_parse_stats["conversion_status"], "partial_success")
        self.assertEqual(parser.last_parse_stats["conversion_error_count"], 2)
        self.assertEqual(parser.last_parse_stats["failed_pages"], [40, 41])

    def test_parse_and_chunk_promotes_formal_document_headings_to_section_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "policy.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=FakeFormalConverter()):
                chunks = AdvancedDocumentParser().parse_and_chunk(str(pdf_path))

        self.assertTrue(any("一、总体要求" in chunk.metadata.get("section_path", []) for chunk in chunks))
        self.assertTrue(any(chunk.metadata.get("section") == "（一）建设目标" for chunk in chunks))
        self.assertTrue(any(chunk.page_content.startswith("## 一、总体要求") for chunk in chunks))

    def test_policy_cleaning_and_metadata_are_preserved_for_indexing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "policy.pdf"
            pdf_path.write_bytes(b"%PDF fake")

            with patch("core.document_parser.DocumentConverter", return_value=FakePolicyConverter()):
                parser = AdvancedDocumentParser()
                chunks = parser.parse_and_chunk(str(pdf_path))

        self.assertTrue(chunks)
        self.assertTrue(all("第 1 页 共 3 页" not in chunk.page_content for chunk in chunks))
        self.assertEqual(parser.last_parse_stats["removed_page_artifacts"], 1)
        self.assertEqual(parser.last_parse_stats["removed_non_content"], 1)
        self.assertEqual(parser.last_parse_stats["removed_duplicates"], 1)
        policy_chunk = next(chunk for chunk in chunks if "第一条" in chunk.page_content)
        self.assertEqual(policy_chunk.metadata["document_title"], "数据安全管理办法")
        self.assertEqual(policy_chunk.metadata["document_number"], "某府〔2026〕12号")
        self.assertEqual(policy_chunk.metadata["document_date"], "2026年4月1日")
        self.assertEqual(policy_chunk.metadata["document_profile"], "government_policy")
        self.assertEqual(policy_chunk.metadata["clause_id"], "第一条")
        self.assertEqual(policy_chunk.metadata["content_kind"], "clause")
        self.assertTrue(policy_chunk.metadata["structure_aware"])

    def test_list_item_preserves_original_marker_and_cleans_zero_width_text(self):
        item = FakeDoclingItem("第一项\u200b要求", 1, label="list_item")
        item.marker = "1."
        parser = AdvancedDocumentParser.__new__(AdvancedDocumentParser)

        markdown = parser._item_markdown(None, item)

        self.assertEqual(markdown, "1. 第一项要求")

    def test_technical_heading_depth_is_preserved(self):
        parser = AdvancedDocumentParser.__new__(AdvancedDocumentParser)

        self.assertEqual(parser._formal_heading_markdown("1 总体架构"), "# 1 总体架构")
        self.assertEqual(parser._formal_heading_markdown("1.2 数据架构"), "## 1.2 数据架构")
        self.assertEqual(parser._formal_heading_markdown("1.2.3 接口规范"), "### 1.2.3 接口规范")
        self.assertEqual(
            parser._document_profile("信息安全技术要求", "GB/T 35273-2020"),
            "standard_document",
        )


if __name__ == "__main__":
    unittest.main()
