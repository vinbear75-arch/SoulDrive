import json
import os
import re
import unicodedata
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
        do_ocr=False,
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
            ("####", "Header 4"),
            ("#####", "Header 5"),
            ("######", "Header 6"),
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
            "repeated_page_artifacts": 0,
            "heading_only_merges": 0,
            "short_same_section_merges": 0,
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

        # Docling can return a usable document together with page-level
        # failures. Keep the successfully converted pages available for
        # retrieval and expose the failed pages through parse statistics. A
        # partial result without a document is still a hard parse failure.
        if status == ConversionStatus.PARTIAL_SUCCESS and getattr(result, "document", None) is not None:
            logger.warning(
                "[Parser] Docling returned a partial document; continuing with converted pages: %s",
                failed_pages,
            )
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
        entries = list(document.iterate_items())
        repeated_artifact_keys = self._repeated_page_artifact_keys(entries)

        for raw_entry in entries:
            item = raw_entry[0] if isinstance(raw_entry, tuple) else raw_entry
            self.last_parse_stats["raw_items"] += 1
            page = self._page_number(item) or last_page
            label = self._item_label(item)
            raw_text = self._item_text(item)
            raw_key = self._duplicate_key(raw_text)
            if self._is_page_artifact(label, raw_text) or raw_key in repeated_artifact_keys:
                self.last_parse_stats["removed_page_artifacts"] += 1
                if raw_key in repeated_artifact_keys:
                    self.last_parse_stats["repeated_page_artifacts"] += 1
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
                page_assets[page]["images"].append(
                    self._image_metadata(
                        page,
                        item,
                        markdown,
                        len(page_assets[page]["images"]) + 1,
                    )
                )
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
            page_assets_attached = False
            for chunk in page_chunks:
                if not chunk.page_content.strip():
                    continue
                self._inherit_page_headers(chunk.metadata, active_headers)
                chunk.metadata.update(document_metadata)
                chunk.metadata["page"] = page
                chunk.metadata["page_number"] = page
                chunk.metadata["page_label"] = str(page)
                chunk.metadata.update(
                    self._structured_metadata_for_chunk(
                        page,
                        chunk.page_content,
                        attach_page_images=not page_assets_attached,
                    )
                )
                page_assets_attached = page_assets_attached or bool(chunk.metadata.get("image_count"))
                section_path = self._section_path(chunk.metadata)
                if section_path:
                    chunk.metadata["section_path"] = section_path
                    chunk.metadata["section"] = section_path[-1]
                else:
                    # Page provenance is more useful than a blank section in the review UI.
                    chunk.metadata["section"] = f"第 {page} 页正文（未识别标题）"
                    chunk.metadata["section_path"] = [chunk.metadata["section"]]
                chunk.metadata["structure_aware"] = bool(
                    section_path or document_metadata.get("document_profile") != "general"
                )
                chunk.metadata.update(self._clause_metadata(chunk.page_content))
                if chunk.metadata.get("table_count"):
                    chunk.metadata["content_kind"] = "table"
                elif chunk.metadata.get("clause_id"):
                    chunk.metadata["content_kind"] = "clause"
                else:
                    chunk.metadata["content_kind"] = "prose"
                chunks.append(chunk)
        return self._merge_fragmented_chunks(chunks)

    def _merge_fragmented_chunks(self, chunks):
        """Join structural leftovers while retaining the smallest useful retrieval unit."""
        if not chunks:
            return []
        merged = []
        pending_headings = []
        for chunk in chunks:
            if self._is_heading_only_chunk(chunk):
                pending_headings.append(chunk)
                continue
            if pending_headings:
                for heading in pending_headings:
                    chunk = self._merge_chunk_pair(heading, chunk)
                pending_headings = []
                self._increment_parse_stat("heading_only_merges")
            if merged and self._can_merge_short_section(merged[-1], chunk):
                merged[-1] = self._merge_chunk_pair(merged[-1], chunk)
                self._increment_parse_stat("short_same_section_merges")
            else:
                merged.append(chunk)
        if pending_headings:
            if merged and self._can_merge_short_section(merged[-1], pending_headings[0]):
                for heading in pending_headings:
                    merged[-1] = self._merge_chunk_pair(merged[-1], heading)
                self._increment_parse_stat("heading_only_merges")
            else:
                merged.extend(pending_headings)
        return merged

    def _is_heading_only_chunk(self, chunk) -> bool:
        lines = [line.strip() for line in str(chunk.page_content or "").splitlines() if line.strip()]
        return bool(lines) and all(re.match(r"^#{1,6}\s+\S", line) for line in lines)

    def _can_merge_short_section(self, left, right) -> bool:
        left_text = str(left.page_content or "").strip()
        right_text = str(right.page_content or "").strip()
        if not left_text or not right_text:
            return False
        if len(left_text) >= 160 and len(right_text) >= 160:
            return False
        if len(left_text) + len(right_text) + 2 > 2400:
            return False
        if left.metadata.get("table_count") or right.metadata.get("table_count"):
            return False
        if left.metadata.get("image_count") or right.metadata.get("image_count"):
            return False
        return self._section_path(left.metadata) == self._section_path(right.metadata)

    def _merge_chunk_pair(self, left, right):
        from langchain_core.documents import Document

        metadata = {**left.metadata, **right.metadata}
        start = min(int(left.metadata.get("page_start", left.metadata.get("page", 1))),
                    int(right.metadata.get("page_start", right.metadata.get("page", 1))))
        end = max(int(left.metadata.get("page_end", left.metadata.get("page", start))),
                  int(right.metadata.get("page_end", right.metadata.get("page", start))))
        metadata["page_start"] = start
        metadata["page_end"] = end
        metadata["page"] = start
        metadata["page_number"] = start
        metadata["page_label"] = str(start) if start == end else f"{start}-{end}"
        return Document(
            page_content=f"{left.page_content.rstrip()}\n\n{right.page_content.lstrip()}",
            metadata=metadata,
        )

    def _increment_parse_stat(self, key: str) -> None:
        stats = getattr(self, "last_parse_stats", None)
        if isinstance(stats, dict):
            stats[key] = int(stats.get(key, 0)) + 1

    def _repeated_page_artifact_keys(self, entries) -> set[str]:
        pages_by_key: dict[str, set[int]] = defaultdict(set)
        for raw_entry in entries:
            item = raw_entry[0] if isinstance(raw_entry, tuple) else raw_entry
            text = self._item_text(item)
            key = self._duplicate_key(text)
            page = self._page_number(item)
            label = self._item_label(item)
            if key and page is not None and len(text) <= 100 and not self._is_image_label(label):
                pages_by_key[key].add(page)
        return {key for key, pages in pages_by_key.items() if len(pages) >= 3}

    def _inherit_page_headers(self, metadata: dict[str, Any], active_headers: dict[str, str]) -> None:
        header_keys = tuple(f"Header {level}" for level in range(1, 7))
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

        if self._is_image_label(label):
            # 图像本身由多模态链路处理，不能用占位符污染文本检索。
            return ""
        text = self._item_text(item)
        if not text:
            return ""

        if label == "title":
            if not self._is_valid_heading(text):
                return text
            return f"# {text}"
        if "list_item" in label:
            marker = self._clean_text(str(getattr(item, "marker", "") or ""))
            if marker and not text.startswith(marker):
                return f"{marker} {text}"
            return f"- {text}"
        formal_heading = self._formal_heading_markdown(text)
        if formal_heading:
            return formal_heading
        if "section_header" in label or item.__class__.__name__.lower().endswith("sectionheaderitem"):
            if not self._is_valid_heading(text):
                return text
            level = self._bounded_heading_level(getattr(item, "level", 1))
            return f"{'#' * level} {text}"
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

    def _structured_metadata_for_chunk(
        self,
        page: int,
        content: str,
        *,
        attach_page_images: bool = False,
    ) -> dict[str, Any]:
        assets = self._page_assets.get(page, {"tables": [], "images": []})
        tables = [
            table
            for table in assets.get("tables", [])
            if table["markdown"] and table["markdown"] in content
        ]
        images = [
            image
            for image in assets.get("images", [])
            if attach_page_images
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

    def _image_metadata(
        self,
        page: int,
        item: Any,
        markdown: str,
        asset_index: int,
    ) -> dict[str, Any]:
        caption = str(getattr(item, "text", None) or getattr(item, "orig", "") or "").strip()
        return {
            "asset_id": f"figure-p{page}-{asset_index}",
            "page": page,
            "caption": caption,
            "placeholder": markdown or "[图像]",
            "source_locator": f"page:{page}/figure:{asset_index}",
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

    def _is_valid_heading(self, text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", normalized):
            return False
        # Docling can label a short OCR-extracted author name as a section.
        # Keep the text, but do not let that low-information line become a
        # retrieval hierarchy node.
        if re.fullmatch(r"[\u4e00-\u9fff]{1,2}\s+[\u4e00-\u9fff]{1,2}", normalized):
            return False
        return not re.fullmatch(
            r"(?:第[一二三四五六七八九十百千万0-9]+[章节篇部分]|[一二三四五六七八九十百千万]+、|\d+(?:\.\d+)*[、.)]?)",
            normalized,
        )

    def _section_path(self, metadata: dict[str, Any]) -> list[str]:
        path = []
        for key in ("Header 1", "Header 2", "Header 3", "Header 4", "Header 5", "Header 6"):
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
        text = "".join(
            unicodedata.normalize("NFKC", character)
            if "\u2f00" <= character <= "\u2fd5" or "\uf900" <= character <= "\ufaff"
            else character
            for character in value
        ).replace("戶", "户").replace("\u00ad", "")
        text = (text
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
            r"^\d+\s+copyright$",
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
