"""Build independent table/image ground truth for the ingestion corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import fitz


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "eval" / "corpus_manifest_v0.2.json"
OUTPUT_PATH = PROJECT_ROOT / "eval" / "corpus_modality_ground_truth_v0.1.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    documents = []
    for document in manifest["documents"]:
        filename = str(document["local_filename"])
        documents.append(
            audit_document(
                doc_id=str(document["doc_id"]),
                source_filename=filename,
                pdf_path=args.corpus_dir / filename,
            )
        )

    payload = {
        "schema_version": "corpus_modality_ground_truth_v1",
        "method": {
            "tables": "PyMuPDF find_tables; page-level evidence retained",
            "images": (
                "PyMuPDF image bounding boxes; tiny assets and near-full-page "
                "background scans excluded"
            ),
        },
        "document_count": len(documents),
        "documents": documents,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def audit_document(
    *,
    doc_id: str,
    source_filename: str,
    pdf_path: Path,
) -> dict[str, Any]:
    if not pdf_path.is_file():
        raise FileNotFoundError(f"corpus PDF not found: {pdf_path}")

    table_pages: list[int] = []
    image_pages: list[int] = []
    table_count = 0
    image_count = 0
    with fitz.open(pdf_path) as document:
        for page_index, page in enumerate(document, start=1):
            tables = page.find_tables().tables
            if tables:
                table_pages.append(page_index)
                table_count += len(tables)

            meaningful_images = _meaningful_images(page)
            if meaningful_images:
                image_pages.append(page_index)
                image_count += len(meaningful_images)

        page_count = len(document)

    required_modalities = []
    if table_pages:
        required_modalities.append("table")
    if image_pages:
        required_modalities.append("image")
    return {
        "doc_id": doc_id,
        "source_filename": source_filename,
        "page_count": page_count,
        "table_count": table_count,
        "table_pages": table_pages,
        "image_count": image_count,
        "image_pages": image_pages,
        "required_modalities": required_modalities,
    }


def _meaningful_images(page) -> list[dict[str, Any]]:
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    meaningful = []
    seen = set()
    for image in page.get_image_info(xrefs=True):
        bbox = tuple(round(float(value), 3) for value in image.get("bbox", ()))
        key = (image.get("xref"), bbox)
        if key in seen:
            continue
        seen.add(key)
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        if width < 64 or height < 64 or len(bbox) != 4:
            continue
        area_ratio = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / page_area)
        if area_ratio < 0.002 or area_ratio > 0.9:
            continue
        meaningful.append({"xref": int(image.get("xref") or 0), "area_ratio": area_ratio})
    return meaningful


if __name__ == "__main__":
    main()
