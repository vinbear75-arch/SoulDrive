import io
import json
import time
from pathlib import Path
from typing import Any, Callable

from langchain_core.documents import Document
from PIL import Image

from core.document_types import is_image_document
from core.indexing_errors import classify_indexing_error
from core.logging_config import get_logger


logger = get_logger(__name__)

MAX_IMAGE_PIXELS = 40_000_000
MAX_OCR_REGIONS = 500
REGIONS_PER_CHUNK = 24


def _rapid_ocr_factory():
    from rapidocr import RapidOCR

    return RapidOCR()


class LocalImageOcrParser:
    def __init__(self, engine_factory: Callable[[], Any] | None = None):
        self.engine_factory = engine_factory or _rapid_ocr_factory
        self.engine = None
        self.last_error: dict[str, Any] | None = None
        self.last_parse_stats: dict[str, Any] = {}

    def parse_and_chunk(self, image_path: str):
        self.last_error = None
        self.last_parse_stats = self._empty_stats()
        source = Path(image_path)
        if not source.exists():
            raise FileNotFoundError(f"找不到指定的图像文件: {image_path}")
        if not is_image_document(source.name):
            raise ValueError(f"不支持的图像类型: {source.suffix or 'unknown'}")

        try:
            payload = source.read_bytes()
            width, height = self._verified_image_size(payload)
            started_at = time.perf_counter()
            result = self._ocr_engine()(payload)
            ocr_ms = int((time.perf_counter() - started_at) * 1000)
            self.last_parse_stats["ocr_ms"] = ocr_ms
            regions = self._regions(result, width, height)
            if not regions:
                raise RuntimeError("OCR_NO_TEXT: image contains no recognized text")

            chunks = self._chunks(source, width, height, regions)
            scores = [region["confidence"] for region in regions]
            self.last_parse_stats.update(
                {
                    "raw_items": len(regions),
                    "kept_items": len(regions),
                    "image_items": 1,
                    "pages": 1,
                    "parent_chunks": len(chunks),
                    "conversion_status": "success",
                    "converted_pages": 1,
                    "ocr_ms": ocr_ms,
                    "ocr_region_count": len(regions),
                    "ocr_mean_confidence": round(sum(scores) / len(scores), 6),
                    "modality": "image",
                }
            )
            return chunks
        except Exception as exc:
            reason = str(exc)
            self.last_parse_stats["conversion_status"] = "failed"
            self.last_parse_stats["conversion_error_count"] = 1
            self.last_error = {
                "source_filename": source.name,
                "reason": reason,
                **classify_indexing_error(reason),
            }
            logger.error("[Image OCR] 图像识别失败: %s", exc)
            return []

    def _ocr_engine(self):
        if self.engine is None:
            self.engine = self.engine_factory()
        return self.engine

    def _verified_image_size(self, payload: bytes) -> tuple[int, int]:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"image exceeds {MAX_IMAGE_PIXELS} pixel safety limit")
        return width, height

    def _regions(self, result: Any, width: int, height: int) -> list[dict[str, Any]]:
        raw_texts = getattr(result, "txts", None)
        raw_boxes = getattr(result, "boxes", None)
        raw_scores = getattr(result, "scores", None)
        texts = list(raw_texts) if raw_texts is not None else []
        boxes = list(raw_boxes) if raw_boxes is not None else []
        scores = list(raw_scores) if raw_scores is not None else []
        regions = []
        for index, raw_text in enumerate(texts[:MAX_OCR_REGIONS]):
            text = " ".join(str(raw_text or "").replace("\x00", " ").split()).strip()
            if not text:
                continue
            confidence = self._confidence(scores[index] if index < len(scores) else None)
            bbox = self._normalized_bbox(boxes[index] if index < len(boxes) else None, width, height)
            region = {
                "text": text[:1200],
                "confidence": confidence,
            }
            if bbox is not None:
                region["bbox"] = bbox
            regions.append(region)
        regions.sort(
            key=lambda item: (
                (item.get("bbox") or {}).get("y", 1.0),
                (item.get("bbox") or {}).get("x", 1.0),
            )
        )
        return regions

    def _chunks(
        self,
        source: Path,
        width: int,
        height: int,
        regions: list[dict[str, Any]],
    ) -> list[Document]:
        chunks = []
        for start in range(0, len(regions), REGIONS_PER_CHUNK):
            group = regions[start : start + REGIONS_PER_CHUNK]
            confidence = round(sum(item["confidence"] for item in group) / len(group), 6)
            chunks.append(
                Document(
                    page_content="\n".join(item["text"] for item in group),
                    metadata={
                        "page": 1,
                        "page_number": 1,
                        "page_label": "1",
                        "document_title": source.stem,
                        "section": f"图像文字区域 {len(chunks) + 1}",
                        "content_kind": "image_ocr",
                        "modality": "image",
                        "structure_aware": True,
                        "image_count": 1,
                        "image_width": width,
                        "image_height": height,
                        "ocr_region_count": len(group),
                        "ocr_mean_confidence": confidence,
                        "ocr_regions_json": json.dumps(group, ensure_ascii=False),
                        "vision_status": "ocr_complete",
                    },
                )
            )
        return chunks

    def _normalized_bbox(self, box: Any, width: int, height: int) -> dict[str, float] | None:
        try:
            points = list(box)
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
        except (TypeError, ValueError, IndexError):
            return None
        if not xs or not ys:
            return None
        left = max(0.0, min(xs))
        top = max(0.0, min(ys))
        right = min(float(width), max(xs))
        bottom = min(float(height), max(ys))
        if right <= left or bottom <= top:
            return None
        return {
            "x": round(left / width, 6),
            "y": round(top / height, 6),
            "width": round((right - left) / width, 6),
            "height": round((bottom - top) / height, 6),
        }

    def _confidence(self, value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return round(max(0.0, min(confidence, 1.0)), 6)

    def _empty_stats(self) -> dict[str, Any]:
        return {
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
            "ocr_ms": 0,
            "ocr_region_count": 0,
            "ocr_mean_confidence": 0.0,
            "modality": "image",
        }
