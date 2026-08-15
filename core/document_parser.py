import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.base_models import ConversionStatus
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from langchain_text_splitters import MarkdownHeaderTextSplitter

from core.indexing_errors import classify_indexing_error
from core.logging_config import get_logger

logger = get_logger(__name__)


def _document_converter() -> DocumentConverter:
    artifacts_value = str(os.environ.get("SOULDRIVE_DOCLING_ARTIFACTS_PATH") or "").strip()
    artifacts_path = None
    if artifacts_value:
        artifacts_path = Path(artifacts_value).expanduser().resolve()
        if not artifacts_path.is_dir():
            raise FileNotFoundError(f"Docling artifacts directory not found: {artifacts_path}")

    pipeline_options = PdfPipelineOptions(
        artifacts_path=artifacts_path,
        accelerator_options=AcceleratorOptions(
            num_threads=2,
            device=AcceleratorDevice.CPU,
        ),
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
        ocr_batch_size=2,
        layout_batch_size=2,
        table_batch_size=2,
        queue_max_size=8,
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )


class AdvancedDocumentParser:
    def __init__(self):
        logger.info("[System] 正在挂载工业级版面分析引擎 (Docling) ...")
        # Docling 默认调用轻量级 ONNX 模型运行在 CPU 上
        # 彻底解放显存，同时具备极强的双栏、表格、公式结构化能力
        self.converter = _document_converter()

        # 保留基于语义的 Markdown 切片器，防止段落被打碎
        self.headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
        ]
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.headers_to_split_on,
            strip_headers=False
        )
        self.last_error: dict[str, str] | None = None
        self.last_parse_stats: dict[str, Any] = {}
        self._page_assets: dict[int, dict[str, list[dict[str, Any]]]] = {}

    def parse_and_chunk(self, pdf_path: str):
        """主入口：基于版面感知 (Layout-Aware) 的极速解析管道"""
        self.last_error = None
        self.last_parse_stats = {
            "raw_items": 0,
            "kept_items": 0,
            "removed_page_artifacts": 0,
            "removed_non_content": 0,
            "removed_duplicates": 0,
            "table_items": 0,
            "image_items": 0,
            "pages": 0,
            "parent_chunks": 0,
            "conversion_status": "pending",
            "conversion_error_count": 0,
            "converted_pages": 0,
            "failed_pages": [],
        }
        self._page_assets = {}
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"找不到指定的 PDF 文件: {pdf_path}")

        logger.info("[Parser] 开始执行版面感知解析: %s", os.path.basename(pdf_path))
        logger.info("[Parser] 引擎正在识别双栏、表格、公式并重构 Markdown (CPU 运算)...")

        try:
            # 这一行代码完成了所有工作：读取PDF -> 目标检测(分栏/表格) -> OCR -> Markdown对齐
            result = self.converter.convert(pdf_path)
            self._validate_conversion_result(result)
            page_markdown = self._export_markdown_by_page(result.document)
            document_metadata = self._document_metadata(page_markdown, pdf_path)

            logger.info("[Parser] Markdown 重构完毕，正在执行带页码的语义切片...")
            chunks = self._split_page_markdown(page_markdown, document_metadata)
            if not chunks:
                full_markdown = result.document.export_to_markdown()
                chunks = self._split_page_markdown({1: full_markdown}, document_metadata)
            self.last_parse_stats["pages"] = len(page_markdown)
            self.last_parse_stats["parent_chunks"] = len(chunks)
            logger.info("[Parser] 解析完成！共生成 %s 个高质量语义块。", len(chunks))

            return chunks

        except Exception as e:
            reason = str(e)
            self.last_error = {
                "source_filename": os.path.basename(pdf_path),
                "reason": reason,
                **classify_indexing_error(reason),
            }
            logger.error("[Parser 致命错误] Docling 解析失败: %s", e)
            return []

    def _validate_conversion_result(self, result: Any) -> None:
        status = getattr(result, "status", None)
        status_value = str(getattr(status, "value", status) or "success")
        errors = list(getattr(result, "errors", None) or [])
        error_messages = [
            str(getattr(error, "error_message", None) or error)
            for error in errors
        ]
        failed_pages = sorted({
            int(match.group(1))
            for message in error_messages
            for match in [re.search(r"\bPage\s+(\d+)\b", message, flags=re.IGNORECASE)]
            if match
        })
        self.last_parse_stats["conversion_status"] = status_value
        self.last_parse_stats["conversion_error_count"] = len(error_messages)
        self.last_parse_stats["converted_pages"] = len(
            list(getattr(result, "pages", None) or [])
        )
        self.last_parse_stats["failed_pages"] = failed_pages

        if status is None or status == ConversionStatus.SUCCESS:
            return

        detail = "; ".join(error_messages[:3]) or "unspecified conversion error"
        page_detail = f"; failed pages: {failed_pages}" if failed_pages else ""
        raise RuntimeError(
            f"Docling {status_value} conversion{page_detail}; {detail}"
        )

    def _export_markdown_by_page(self, document) -> dict[int, str]:
        page_parts: dict[int, list[str]] = defaultdict(list)
        page_assets: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"tables": [], "images": []})
        last_page: int | None = None
        last_markdown_by_page: dict[int, str] = {}

        for raw_entry in document.iterate_items():
            item = raw_entry[0] if isinstance(raw_entry, tuple) else raw_entry
            self.last_parse_stats["raw_items"] += 1
            page = self._page_number(item) or last_page
            label = self._item_label(item)
            raw_text = self._item_text(item)
            if self._is_page_artifact(label, raw_text):
                self.last_parse_stats["removed_page_artifacts"] += 1
                continue
            if "document_index" in label:
                self.last_parse_stats["removed_non_content"] += 1
                continue
            markdown = self._item_markdown(document, item)
            normalized_markdown = self._duplicate_key(markdown)
            if (
                page is not None
                and normalized_markdown
                and last_markdown_by_page.get(page) == normalized_markdown
            ):
                self.last_parse_stats["removed_duplicates"] += 1
                continue
            if page is not None and "table" in label and markdown:
                page_assets[page]["tables"].append(self._table_metadata(page, markdown))
                self.last_parse_stats["table_items"] += 1
            if page is not None and self._is_image_label(label):
                page_assets[page]["images"].append(self._image_metadata(page, item, markdown))
                self.last_parse_stats["image_items"] += 1
            if page is None or not markdown:
                continue
            page_parts[page].append(markdown)
            last_markdown_by_page[page] = normalized_markdown
            self.last_parse_stats["kept_items"] += 1
            last_page = page

        self._page_assets = dict(page_assets)
        return {
            page: "\n\n".join(part for part in parts if part.strip()).strip()
            for page, parts in sorted(page_parts.items())
            if any(part.strip() for part in parts)
        }

    def _split_page_markdown(self, page_markdown: dict[int, str], document_metadata: dict[str, Any] | None = None):
        chunks = []
        active_headers: dict[str, str] = {}
        document_metadata = document_metadata or {}
        for page, markdown in page_markdown.items():
            page_chunks = self.markdown_splitter.split_text(markdown)
            for chunk in page_chunks:
                if not chunk.page_content.strip():
                    continue
                self._inherit_page_headers(chunk.metadata, active_headers)
                chunk.metadata.update(document_metadata)
                chunk.metadata["page"] = page
                chunk.metadata["page_number"] = page
                chunk.metadata["page_label"] = str(page)
                chunk.metadata.update(self._structured_metadata_for_chunk(page, chunk.page_content))
                section_path = self._section_path(chunk.metadata)
                if section_path:
                    chunk.metadata["section_path"] = section_path
                    chunk.metadata["section"] = section_path[-1]
                chunk.metadata["structure_aware"] = bool(
                    section_path or document_metadata.get("document_profile") != "general"
                )
                chunk.metadata.update(self._clause_metadata(chunk.page_content))
                if chunk.metadata.get("table_count"):
                    chunk.metadata["content_kind"] = "table"
                elif chunk.metadata.get("clause_id"):
                    chunk.metadata["content_kind"] = "clause"
                elif chunk.metadata.get("image_count"):
                    chunk.metadata["content_kind"] = "image"
                else:
                    chunk.metadata["content_kind"] = "prose"
                chunks.append(chunk)
        return chunks

    def _inherit_page_headers(self, metadata: dict[str, Any], active_headers: dict[str, str]) -> None:
        header_keys = ("Header 1", "Header 2", "Header 3")
        explicit_headers = {
            key: str(metadata.get(key) or "").strip()
            for key in header_keys
            if str(metadata.get(key) or "").strip()
        }

        for index, key in enumerate(header_keys):
            value = explicit_headers.get(key)
            if not value:
                continue
            for lower_key in header_keys[index + 1 :]:
                active_headers.pop(lower_key, None)
            active_headers[key] = value

        for key in header_keys:
            if key in active_headers and not metadata.get(key):
                metadata[key] = active_headers[key]

    def _item_markdown(self, document, item) -> str:
        label = self._item_label(item)
        if "table" in label and hasattr(item, "export_to_markdown"):
            try:
                return str(item.export_to_markdown(document)).strip()
            except TypeError:
                return str(item.export_to_markdown()).strip()
            except Exception:
                return ""

        text = self._item_text(item)
        if self._is_image_label(label):
            return f"[图像: {text}]" if text else "[图像]"
        if not text:
            return ""

        if label == "title":
            return f"# {text}"
        formal_heading = self._formal_heading_markdown(text)
        if formal_heading:
            return formal_heading
        if "section_header" in label or item.__class__.__name__.lower().endswith("sectionheaderitem"):
            level = self._bounded_heading_level(getattr(item, "level", 1))
            return f"{'#' * level} {text}"
        if "list_item" in label:
            marker = self._clean_text(str(getattr(item, "marker", "") or ""))
            if marker and not text.startswith(marker):
                return f"{marker} {text}"
            return f"- {text}"
        return text

    def _page_number(self, item) -> int | None:
        for provenance in getattr(item, "prov", []) or []:
            page_no = getattr(provenance, "page_no", None)
            page = self._positive_int(page_no)
            if page is not None:
                return page
        return None

    def _item_label(self, item) -> str:
        label = getattr(item, "label", "")
        return str(getattr(label, "value", label)).lower()

    def _structured_metadata_for_chunk(self, page: int, content: str) -> dict[str, Any]:
        assets = self._page_assets.get(page, {"tables": [], "images": []})
        tables = [
            table
            for table in assets.get("tables", [])
            if table["markdown"] and table["markdown"] in content
        ]
        images = [
            image
            for image in assets.get("images", [])
            if not image.get("caption") or image.get("caption") in content or "[图像" in content
        ]
        metadata: dict[str, Any] = {}
        if tables:
            metadata["table_count"] = len(tables)
            metadata["tables_json"] = json.dumps(tables, ensure_ascii=False)
        if images:
            metadata["image_count"] = len(images)
            metadata["image_metadata_json"] = json.dumps(images, ensure_ascii=False)
        return metadata

    def _table_metadata(self, page: int, markdown: str) -> dict[str, Any]:
        rows = [line for line in markdown.splitlines() if line.strip().startswith("|")]
        data_rows = [line for line in rows if "---" not in line]
        return {
            "page": page,
            "row_count": max(0, len(data_rows) - 1),
            "markdown": markdown,
        }

    def _image_metadata(self, page: int, item: Any, markdown: str) -> dict[str, Any]:
        caption = str(getattr(item, "text", None) or getattr(item, "orig", "") or "").strip()
        return {
            "page": page,
            "caption": caption,
            "placeholder": markdown or "[图像]",
            "vision_status": "pending_local_model",
        }

    def _is_image_label(self, label: str) -> bool:
        return any(token in label for token in ("picture", "image", "figure"))

    def _formal_heading_markdown(self, text: str) -> str:
        if len(text) > 80 or any(mark in text for mark in ("。", "！", "？", "；", ";")):
            return ""
        technical_heading = re.match(
            r"^(\d+(?:\.\d+){0,5})(?:[、.)]|\s+)\s*[\u4e00-\u9fffA-Za-z].+",
            text,
        )
        if technical_heading:
            level = min(3, technical_heading.group(1).count(".") + 1)
            return f"{'#' * level} {text}"
        heading_patterns = (
            (r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分](?:[：:、\s].+)?$", 1),
            (r"^[一二三四五六七八九十]+、.+", 2),
            (r"^（[一二三四五六七八九十]+）.+", 3),
            (r"^\([一二三四五六七八九十]+\).+", 3),
            (r"^\d+[.、]\s*[\u4e00-\u9fffA-Za-z].+", 3),
            (r"^附件\s*[一二三四五六七八九十0-9]*[：:].+", 2),
        )
        for pattern, level in heading_patterns:
            if re.match(pattern, text):
                return f"{'#' * level} {text}"
        return ""

    def _section_path(self, metadata: dict[str, Any]) -> list[str]:
        path = []
        for key in ("Header 1", "Header 2", "Header 3"):
            value = str(metadata.get(key) or "").strip()
            if value:
                path.append(value)
        return path

    def _bounded_heading_level(self, value: Any) -> int:
        level = self._positive_int(value) or 1
        return max(1, min(level, 6))

    def _positive_int(self, value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _item_text(self, item: Any) -> str:
        value = str(getattr(item, "text", None) or getattr(item, "orig", "") or "")
        return self._clean_text(value)

    def _clean_text(self, value: str) -> str:
        text = (
            value.replace("\u00ad", "")
            .replace("\u200b", "")
            .replace("\u200c", "")
            .replace("\u200d", "")
            .replace("\ufeff", "")
            .replace("\u00a0", " ")
        )
        lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _is_page_artifact(self, label: str, text: str) -> bool:
        if any(token in label for token in ("page_header", "page_footer", "page_number")):
            return True
        normalized = text.strip()
        if not normalized:
            return False
        page_number_patterns = (
            r"^第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?$",
            r"^[Pp]age\s+\d+(?:\s+of\s+\d+)?$",
            r"^\d+\s*/\s*\d+$",
            r"^[-—–]\s*\d{1,4}\s*[-—–]$",
        )
        return any(re.fullmatch(pattern, normalized) for pattern in page_number_patterns)

    def _duplicate_key(self, markdown: str) -> str:
        return re.sub(r"\s+", " ", markdown or "").strip().lower()

    def _document_metadata(self, page_markdown: dict[int, str], pdf_path: str) -> dict[str, Any]:
        leading_pages = [
            markdown
            for _, markdown in list(sorted(page_markdown.items()))[:3]
            if markdown
        ]
        leading_text = "\n".join(leading_pages)
        full_text = "\n".join(page_markdown.values())
        title = self._document_title(leading_text) or _path_stem(pdf_path)
        document_number = self._first_match(
            leading_text,
            (
                r"[\u4e00-\u9fffA-Za-z]{1,20}[〔\[]\d{4}[〕\]]\s*\d+\s*号",
                r"(?:GB|GB/T|DB\d*/T|T/[\w-]+|Q/[\w-]+)\s*[\w.-]+(?:-\d{4})?",
            ),
        )
        document_date = self._first_match(
            leading_text,
            (r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日",),
        )
        document_profile = self._document_profile(full_text, document_number)
        metadata: dict[str, Any] = {
            "document_title": title,
            "document_profile": document_profile,
        }
        if document_number:
            metadata["document_number"] = document_number
        if document_date:
            metadata["document_date"] = document_date
        return metadata

    def _document_title(self, text: str) -> str:
        candidates = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = re.match(r"^#{1,6}\s+(.+)$", line)
            if heading:
                candidate = heading.group(1).strip()
                if not self._formal_heading_markdown(candidate):
                    candidates.append(candidate)
        if candidates:
            return candidates[0][:160]

        for raw_line in text.splitlines():
            line = re.sub(r"^(?:[-*+]\s+|#{1,6}\s+)", "", raw_line.strip())
            if 4 <= len(line) <= 120 and not self._is_page_artifact("", line):
                if not self._first_match(line, (r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日",)):
                    return line
        return ""

    def _document_profile(self, text: str, document_number: str) -> str:
        if re.match(
            r"^(?:GB|GB/T|DB\d*/T|T/[\w-]+|Q/[\w-]+)",
            document_number,
            flags=re.IGNORECASE,
        ):
            return "standard_document"
        if re.search(
            r"(?:第[一二三四五六七八九十百千万0-9]+条|本办法|本条例|本通知|人民政府|国务院)",
            text,
        ):
            return "government_policy"
        if re.search(r"(?:项目方案|建设方案|实施方案|投标文件|验收方案|项目目标)", text):
            return "project_plan"
        if re.search(r"(?:技术方案|系统架构|接口规范|部署架构|数据架构|安全架构)", text):
            return "technical_document"
        if document_number:
            return "formal_document"
        if re.search(r"(?m)^#{1,6}\s+", text):
            return "formal_document"
        return "general"

    def _clause_metadata(self, content: str) -> dict[str, str]:
        clause_ids = []
        patterns = (
            r"(?m)^\s*(第[一二三四五六七八九十百千万0-9]+条)\b",
            r"(?m)^\s*((?:\d+\.)+\d+)(?:[、.)\s]|$)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                value = match.group(1)
                if value not in clause_ids:
                    clause_ids.append(value)
        if not clause_ids:
            return {}
        return {
            "clause_id": clause_ids[0],
            "clause_ids": ",".join(clause_ids),
        }

    def _first_match(self, text: str, patterns: tuple[str, ...]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return ""


def _path_stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]
