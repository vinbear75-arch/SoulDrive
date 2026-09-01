"""Small dependency-free parser for project text and Office assets."""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from langchain_core.documents import Document

from core.document_types import SUPPORTED_OFFICE_EXTENSIONS, SUPPORTED_TEXT_EXTENSIONS
from core.indexing_errors import classify_indexing_error


def _xml_local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _xlsx_cell_value(cell: Any, shared_strings: list[str]) -> str:
    cell_type = str(cell.attrib.get("t") or "")
    if cell_type == "inlineStr":
        return "".join(
            str(item.text or "")
            for item in cell.iter()
            if _xml_local_name(item.tag) == "t"
        ).strip()
    value = next(
        (
            str(item.text or "")
            for item in cell.iter()
            if _xml_local_name(item.tag) == "v"
        ),
        "",
    ).strip()
    if cell_type == "s":
        try:
            return shared_strings[int(value)].strip()
        except (TypeError, ValueError, IndexError):
            return ""
    return value


def _xlsx_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(
            str(item.text or "")
            for item in value.iter()
            if _xml_local_name(item.tag) == "t"
        )
        for value in root.iter()
        if _xml_local_name(value.tag) == "si"
    ]


def _xlsx_sheet_labels(
    archive: zipfile.ZipFile,
    names: set[str],
    worksheets: list[str],
) -> dict[str, str]:
    if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        return {}
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib.get("Id"): item.attrib.get("Target")
        for item in relationships
        if item.attrib.get("Id") and item.attrib.get("Target")
    }
    labels: dict[str, str] = {}
    for sheet in workbook.iter():
        if _xml_local_name(sheet.tag) != "sheet":
            continue
        relationship_id = next(
            (
                value
                for key, value in sheet.attrib.items()
                if key == "id" or key.endswith("}id")
            ),
            None,
        )
        target = targets.get(relationship_id)
        if not target:
            continue
        normalized_target = str(target).replace("\\", "/").lstrip("/")
        if not normalized_target.startswith("xl/"):
            normalized_target = posixpath.normpath(
                posixpath.join("xl", normalized_target)
            )
        if normalized_target in worksheets:
            labels[normalized_target] = str(
                sheet.attrib.get("name") or Path(normalized_target).stem
            )
    return labels


def _xlsx_column_number(cell: Any, fallback: int) -> int:
    match = re.match(r"([A-Za-z]+)", str(cell.attrib.get("r") or ""))
    if not match:
        return fallback
    number = 0
    for character in match.group(1).upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return max(1, number)


def _xlsx_row_number(row: Any, fallback: int) -> int:
    try:
        return max(1, int(row.attrib.get("r") or fallback))
    except (TypeError, ValueError):
        return max(1, fallback)


def _xlsx_sheet_rows(source: Path) -> list[tuple[str, list[tuple[int, list[str]]]]]:
    if not zipfile.is_zipfile(source):
        raise ValueError("xlsx source is not a valid zip archive")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        worksheets = sorted(
            name
            for name in names
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
        )
        if not worksheets:
            raise ValueError("xlsx source contains no worksheet XML")
        shared_strings = _xlsx_shared_strings(archive, names)
        labels = _xlsx_sheet_labels(archive, names, worksheets)
        result: list[tuple[str, list[tuple[int, list[str]]]]] = []
        for worksheet in worksheets:
            root = ElementTree.fromstring(archive.read(worksheet))
            rows: list[tuple[int, list[str]]] = []
            for position, row in enumerate(
                (item for item in root.iter() if _xml_local_name(item.tag) == "row"),
                start=1,
            ):
                row_number = _xlsx_row_number(row, position)
                values_by_column: dict[int, str] = {}
                for cell_position, cell in enumerate(row, start=1):
                    if _xml_local_name(cell.tag) != "c":
                        continue
                    values_by_column[_xlsx_column_number(cell, cell_position)] = _xlsx_cell_value(
                        cell, shared_strings
                    )
                if not values_by_column:
                    continue
                values = [
                    values_by_column.get(column, "")
                    for column in range(1, max(values_by_column) + 1)
                ]
                if any(value.strip() for value in values):
                    rows.append((row_number, values))
            if rows:
                result.append((labels.get(worksheet) or Path(worksheet).stem, rows))
        return result


def _xlsx_logical_table(
    physical_rows: list[tuple[int, list[str]]],
) -> tuple[list[list[str]], bool, list[int]]:
    if not physical_rows:
        return [], False, []
    header = physical_rows[0][1]
    if len(header) == 1 and ("\n" in header[0] or "\r" in header[0]):
        embedded = list(csv.reader(io.StringIO(header[0])))
        if len(embedded) > 1:
            return embedded, True, list(range(2, len(embedded) + 1))
    return (
        [values for _, values in physical_rows],
        False,
        [row_number for row_number, _ in physical_rows[1:]],
    )


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    column_count = max(len(row) for row in rows)

    def render(row: list[str]) -> str:
        values = [
            str(value or "").replace("|", "\\|").replace("\n", " ")
            for value in row
        ]
        values.extend("" for _ in range(column_count - len(values)))
        return "| " + " | ".join(values) + " |"

    rendered = [render(rows[0])]
    if len(rows) == 1:
        return rendered[0]
    rendered.append("| " + " | ".join("---" for _ in range(column_count)) + " |")
    rendered.extend(render(row) for row in rows[1:])
    return "\n".join(rendered)


class PlainDocumentParser:
    def __init__(self):
        self.last_error: dict[str, Any] | None = None
        self.last_parse_stats: dict[str, Any] = {}

    def parse_and_chunk(self, document_path: str) -> list[Document]:
        source = Path(document_path)
        self.last_error = None
        self.last_parse_stats = {
            "raw_items": 0, "kept_items": 0, "removed_page_artifacts": 0,
            "removed_non_content": 0, "removed_duplicates": 0, "table_items": 0,
            "image_items": 0, "pages": 1, "parent_chunks": 0,
            "conversion_status": "pending", "conversion_error_count": 0,
            "converted_pages": 1, "failed_pages": [], "modality": "document",
        }
        try:
            if source.suffix.lower() in SUPPORTED_TEXT_EXTENSIONS:
                text = self._read_text(source)
            elif source.suffix.lower() in SUPPORTED_OFFICE_EXTENSIONS:
                text = self._read_office(source)
            else:
                raise ValueError(f"unsupported plain document type: {source.suffix}")
            text = text.strip()
            if not text:
                raise RuntimeError("NO_PARSEABLE_CHUNKS: document contains no text")
            chunks = self._chunks(source, text)
            source_provenance = self._source_provenance(source)
            for chunk in chunks:
                chunk.metadata.update(source_provenance)
            self.last_parse_stats.update({
                "raw_items": 1, "kept_items": 1, "parent_chunks": len(chunks),
                "conversion_status": "success",
            })
            return chunks
        except Exception as exc:
            reason = str(exc)
            self.last_parse_stats.update({"conversion_status": "failed", "conversion_error_count": 1})
            self.last_error = {"source_filename": source.name, "reason": reason, **classify_indexing_error(reason)}
            return []

    @classmethod
    def _source_provenance(cls, source: Path) -> dict[str, Any]:
        suffix = source.suffix.lower()
        if suffix == ".csv":
            with source.open("r", encoding="utf-8-sig", newline="") as handle:
                row_count = len(list(csv.reader(handle)))
            return {
                "source_locator": cls._row_range_locator(row_count),
                "source_locator_kind": "csv_row_range",
            }
        if suffix == ".xlsx":
            return {
                "source_locator": cls._xlsx_locator(source)[:600],
                "source_locator_kind": "xlsx_sheet_row_range",
            }
        if suffix in {".json", ".yaml", ".yml", ".log", ".txt", ".md"}:
            line_count = len(source.read_text(encoding="utf-8", errors="replace").splitlines())
            return {
                "source_locator": cls._line_range_locator(line_count),
                "source_locator_kind": "source_line_range",
            }
        if suffix == ".docx":
            return {
                "source_locator": "word/document.xml",
                "source_locator_kind": "office_part",
            }
        if suffix == ".pptx":
            return {
                "source_locator": "ppt/slides/slide*.xml",
                "source_locator_kind": "office_part",
            }
        return {}

    @staticmethod
    def _row_range_locator(row_count: int) -> str:
        if row_count <= 0:
            return "rows:empty"
        if row_count == 1:
            return "header:row:1"
        return f"rows:2-{row_count}"

    @staticmethod
    def _line_range_locator(line_count: int) -> str:
        return "lines:empty" if line_count <= 0 else f"lines:1-{line_count}"

    @classmethod
    def _xlsx_locator(cls, source: Path) -> str:
        try:
            locators = []
            for label, physical_rows in _xlsx_sheet_rows(source):
                logical_rows, embedded, data_rows = _xlsx_logical_table(physical_rows)
                if not data_rows:
                    row_range = f"header:row:{physical_rows[0][0]}"
                elif embedded:
                    row_range = f"embedded_rows:{data_rows[0]}-{data_rows[-1]}"
                else:
                    row_range = f"rows:{data_rows[0]}-{data_rows[-1]}"
                if logical_rows:
                    locators.append(f"sheet:{label}/{row_range}")
            return "; ".join(locators) or "workbook:unknown"
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            return "workbook:unknown"

    def _read_text(self, source: Path) -> str:
        raw = source.read_text(encoding="utf-8", errors="replace")
        if source.suffix.lower() == ".csv":
            rows = list(csv.reader(io.StringIO(raw)))
            if not rows:
                return ""
            formatted_rows = [
                "| " + " | ".join(cell.strip() for cell in row) + " |"
                for row in rows
            ]
            if len(rows) == 1:
                return formatted_rows[0]
            column_count = max(len(row) for row in rows)
            separator = "| " + " | ".join("---" for _ in range(column_count)) + " |"
            return "\n".join([formatted_rows[0], separator, *formatted_rows[1:]])
        if source.suffix.lower() == ".json":
            try:
                return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                return raw
        return raw

    def _read_office(self, source: Path) -> str:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if source.suffix.lower() == ".docx":
                targets = [name for name in names if name == "word/document.xml"]
            elif source.suffix.lower() == ".pptx":
                targets = sorted(name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name))
            else:
                return self._read_xlsx_text(source)
            texts = []
            for name in targets:
                root = ElementTree.fromstring(archive.read(name))
                values = [item.text.strip() for item in root.iter() if item.text and item.text.strip()]
                if values:
                    texts.append(" ".join(values))
            return "\n\n".join(texts)

    @staticmethod
    def _read_xlsx_text(source: Path) -> str:
        sections = []
        for label, physical_rows in _xlsx_sheet_rows(source):
            rows, _, _ = _xlsx_logical_table(physical_rows)
            if rows:
                sections.append(f"## {label}\n{_markdown_table(rows)}")
        return "\n\n".join(sections)

    def _chunks(self, source: Path, text: str) -> list[Document]:
        fallback_section = "第 1 页正文（未识别标题）"
        chunks: list[Document] = []
        heading_stack: list[tuple[int, str]] = []
        block_lines: list[str] = []
        block_path: list[str] = []
        block_heading_level: int | None = None
        block_has_body = False

        def flush_block() -> None:
            nonlocal block_lines, block_path, block_heading_level, block_has_body
            if not block_lines:
                return
            section = "\n".join(block_lines).strip()
            if block_has_body and section:
                section_path = block_path or [fallback_section]
                chunks.append(self._make_chunk(
                    source,
                    section[:12000],
                    section_path,
                    block_heading_level,
                ))
            block_lines = []
            block_path = []
            block_heading_level = None
            block_has_body = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_block()
                continue

            heading = self._heading_info(line)
            if heading:
                if block_has_body:
                    flush_block()
                title, level = heading
                heading_stack = self._push_heading(heading_stack, title, level)
                if not block_lines:
                    block_path = [item[1] for item in heading_stack]
                    block_heading_level = level
                # Keep a heading that shares a paragraph with its body.  This
                # fixes the old path where the heading was detected but lost.
                block_lines.append(line)
                continue

            if not block_lines:
                block_path = [item[1] for item in heading_stack]
                block_heading_level = heading_stack[-1][0] if heading_stack else None
            block_lines.append(line)
            block_has_body = True

        trailing_heading = "\n".join(block_lines).strip() if not block_has_body else ""
        trailing_heading_path = list(block_path)
        trailing_heading_level = block_heading_level
        flush_block()

        # A title-only file is still useful for source tracing and should not
        # silently disappear during ingestion.
        if not chunks and trailing_heading:
            section_path = trailing_heading_path or [fallback_section]
            chunks.append(self._make_chunk(
                source,
                trailing_heading[:12000],
                section_path,
                trailing_heading_level,
            ))
        return chunks

    @staticmethod
    def _push_heading(
        stack: list[tuple[int, str]],
        title: str,
        level: int,
    ) -> list[tuple[int, str]]:
        bounded_level = max(1, min(int(level), 6))
        next_stack = [item for item in stack if item[0] < bounded_level]
        next_stack.append((bounded_level, title))
        return next_stack

    @staticmethod
    def _make_chunk(
        source: Path,
        section: str,
        section_path: list[str],
        heading_level: int | None,
    ) -> Document:
        clean_path = [str(item).strip() for item in section_path if str(item).strip()]
        return Document(
            page_content=section,
            metadata={
                "page": 1, "page_number": 1, "page_label": "1",
                "document_title": source.stem,
                "section": clean_path[-1] if clean_path else "第 1 页正文（未识别标题）",
                "section_path": clean_path or ["第 1 页正文（未识别标题）"],
                "heading_level": heading_level,
                "content_kind": "table" if _looks_like_table(section) else "prose",
                "modality": "document", "structure_aware": True,
                "source_format": source.suffix.lower(),
            },
        )

    @classmethod
    def _heading_info(cls, value: str) -> tuple[str, int] | None:
        line = value.strip()
        if not line or len(line) > 100 or any(mark in line for mark in ("。", "！", "？", "；", ";")):
            return None

        markdown = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if markdown:
            title = markdown.group(2).strip().rstrip("#").strip()
            return (title, len(markdown.group(1))) if title else None

        if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\b|T)", line):
            return None
        if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分](?:[：:、\s].+)?$", line):
            return line, 1

        numbered = re.match(
            r"^(\d+(?:\.\d+)*)(?:[、.)]|\s+)\s*\S+",
            line,
        )
        if numbered:
            return line, min(6, numbered.group(1).count(".") + 1)
        if re.match(r"^[一二三四五六七八九十百千万]+、\s*\S+", line):
            return line, 1
        if re.match(r"^（[一二三四五六七八九十百千万0-9]+）\s*\S+", line):
            return line, 2
        return None

    @classmethod
    def _heading(cls, value: str) -> str:
        lines = str(value or "").splitlines()
        first_line = lines[0] if lines else ""
        info = cls._heading_info(first_line)
        return info[0] if info else ""


def _looks_like_table(value: str) -> bool:
    return sum(1 for line in value.splitlines() if line.count("|") >= 2) >= 2
