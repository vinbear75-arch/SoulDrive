import json
import tempfile
import zipfile
from pathlib import Path

from core.parent_child_index import split_parent_document
from core.plain_document_parser import PlainDocumentParser


def test_plain_parser_preserves_structured_text_and_metadata():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "metrics.csv"
        source.write_text("metric,value\naccuracy,0.850\n", encoding="utf-8")
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert len(chunks) == 1
    assert "accuracy" in chunks[0].page_content
    assert "| --- | --- |" in chunks[0].page_content
    assert chunks[0].metadata["source_format"] == ".csv"
    assert chunks[0].metadata["page"] == 1
    assert chunks[0].metadata["source_locator"] == "rows:2-2"
    assert chunks[0].metadata["source_locator_kind"] == "csv_row_range"


def test_csv_parent_child_split_repeats_header_for_long_metric_tables():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "metrics.csv"
        source.write_text(
            "metric,value\n"
            + "\n".join(f"metric_{index},{index}" for index in range(1, 30))
            + "\n",
            encoding="utf-8",
        )
        chunks = PlainDocumentParser().parse_and_chunk(str(source))
        children = split_parent_document(
            "metrics-parent",
            chunks[0].page_content,
            child_size=80,
            child_overlap=0,
        )

    assert len(children) > 1
    assert all("| metric | value |" in child.content for child in children)
    assert any("| metric_29 | 29 |" in child.content for child in children)


def test_plain_parser_preserves_xlsx_sheet_and_row_locator():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "metrics.xlsx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "xl/workbook.xml",
                (
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="Metrics" sheetId="1" r:id="rId1"/></sheets></workbook>'
                ),
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                (
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
                    '</Relationships>'
                ),
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                (
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row r="1"><c><v>metric</v></c></row>'
                    '<row r="2"><c><v>accuracy</v></c></row>'
                    '<row r="3"><c><v>0.85</v></c></row></sheetData></worksheet>'
                ),
            )
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert chunks
    assert chunks[0].metadata["source_locator"] == "sheet:Metrics/rows:2-3"
    assert chunks[0].metadata["source_locator_kind"] == "xlsx_sheet_row_range"


def test_plain_parser_falls_back_to_xlsx_worksheet_locator_without_workbook_metadata():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "metrics.xlsx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                (
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row r="1"><c><v>metric</v></c></row>'
                    '<row r="2"><c><v>accuracy</v></c></row></sheetData></worksheet>'
                ),
            )
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert chunks[0].metadata["source_locator"] == "sheet:sheet1/rows:2-2"


def test_plain_parser_preserves_xlsx_table_rows_for_lightweight_export():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "metrics.xlsx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                (
                    "<worksheet><sheetData>"
                    '<row r="1"><c r="A1"><v>metric</v></c><c r="B1"><v>value</v></c></row>'
                    '<row r="2"><c r="A2"><v>accuracy</v></c><c r="B2"><v>0.921</v></c></row>'
                    '<row r="3"><c r="A3"><v>p95_latency_ms</v></c><c r="B3"><v>960</v></c></row>'
                    "</sheetData></worksheet>"
                ),
            )
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert chunks
    assert "## sheet1" in chunks[0].page_content
    assert "| metric | value |" in chunks[0].page_content
    assert "| accuracy | 0.921 |" in chunks[0].page_content
    assert "| p95_latency_ms | 960 |" in chunks[0].page_content
    assert chunks[0].metadata["source_locator"] == "sheet:sheet1/rows:2-3"


def test_plain_parser_extracts_docx_xml_without_optional_office_dependency():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "report.docx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", "<document><p><t>Experiment report</t></p></document>")
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert chunks[0].page_content == "Experiment report"


def test_plain_parser_formats_json_deterministically():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "config.json"
        source.write_text(json.dumps({"batch_size": 8}, ensure_ascii=False), encoding="utf-8")
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert '"batch_size": 8' in chunks[0].page_content


def test_plain_parser_preserves_heading_hierarchy_and_sibling_boundaries():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "rag.md"
        source.write_text(
            "# RAG 方案\n\n"
            "## 检索\n\n"
            "混合检索负责召回候选证据。\n\n"
            "## 生成\n\n"
            "生成阶段只使用通过证据门的内容。\n",
            encoding="utf-8",
        )
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert [chunk.metadata["section_path"] for chunk in chunks] == [
        ["RAG 方案", "检索"],
        ["RAG 方案", "生成"],
    ]
    assert chunks[0].metadata["section"] == "检索"
    assert chunks[1].metadata["section"] == "生成"
    assert "检索" not in chunks[1].metadata["section_path"]


def test_plain_parser_keeps_heading_when_heading_and_body_share_a_block():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "inline.md"
        source.write_text("## 证据门\n回答必须带可定位引用。\n", encoding="utf-8")
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert len(chunks) == 1
    assert chunks[0].page_content.startswith("## 证据门")
    assert chunks[0].metadata["section_path"] == ["证据门"]
    assert chunks[0].metadata["heading_level"] == 2


def test_plain_parser_does_not_drop_title_only_document():
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "title.md"
        source.write_text("# 只有标题\n", encoding="utf-8")
        chunks = PlainDocumentParser().parse_and_chunk(str(source))

    assert len(chunks) == 1
    assert chunks[0].page_content == "# 只有标题"
    assert chunks[0].metadata["section_path"] == ["只有标题"]
