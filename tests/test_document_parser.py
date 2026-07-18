import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class DocumentParserTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
