import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.image_ocr import LocalImageOcrParser


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict) or not payload.get("id") or not payload.get("image_path"):
                raise ValueError(f"invalid image OCR record at line {line_number}")
            records.append(payload)
    return records


def evaluate_records(
    records: list[dict[str, Any]],
    dataset_dir: Path,
    parser: LocalImageOcrParser | None = None,
) -> dict[str, Any]:
    parser = parser or LocalImageOcrParser()
    results = []
    latencies = []
    expected_term_count = 0
    matched_term_count = 0
    all_terms_samples = 0
    localization_count = 0
    localization_matches = 0
    confidence_weighted_sum = 0.0
    confidence_region_count = 0

    for record in records:
        image_path = (dataset_dir / str(record["image_path"])).resolve()
        started_at = time.perf_counter()
        chunks = parser.parse_and_chunk(str(image_path))
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        latencies.append(latency_ms)
        text = "\n".join(chunk.page_content for chunk in chunks)
        regions = _regions_from_chunks(chunks)
        expected_terms = [str(term) for term in record.get("expected_terms") or [] if str(term).strip()]
        term_matches = sum(1 for term in expected_terms if _contains(text, term))
        expected_term_count += len(expected_terms)
        matched_term_count += term_matches
        if expected_terms and term_matches == len(expected_terms):
            all_terms_samples += 1

        sample_localization_count = 0
        sample_localization_matches = 0
        for expected in record.get("expected_regions") or []:
            if not isinstance(expected, dict) or not isinstance(expected.get("bbox"), dict):
                continue
            term = str(expected.get("text") or "").strip()
            if not term:
                continue
            sample_localization_count += 1
            candidates = [region for region in regions if _contains(region.get("text", ""), term)]
            best_iou = max((_bbox_iou(region.get("bbox"), expected["bbox"]) for region in candidates), default=0.0)
            if best_iou >= float(expected.get("min_iou") or 0.5):
                sample_localization_matches += 1
        localization_count += sample_localization_count
        localization_matches += sample_localization_matches

        region_count = int(parser.last_parse_stats.get("ocr_region_count") or 0)
        mean_confidence = float(parser.last_parse_stats.get("ocr_mean_confidence") or 0.0)
        confidence_weighted_sum += mean_confidence * region_count
        confidence_region_count += region_count
        results.append(
            {
                "id": str(record["id"]),
                "parsed": bool(chunks),
                "latency_ms": latency_ms,
                "expected_terms": len(expected_terms),
                "matched_terms": term_matches,
                "expected_regions": sample_localization_count,
                "localized_regions": sample_localization_matches,
                "ocr_region_count": region_count,
                "ocr_mean_confidence": round(mean_confidence, 6),
            }
        )

    parsed_count = sum(1 for result in results if result["parsed"])
    sample_count = len(results)
    return {
        "schema_version": "souldrive_image_ocr_eval_v1",
        "sample_count": sample_count,
        "parsed_count": parsed_count,
        "parse_success_rate": _rate(parsed_count, sample_count),
        "expected_term_count": expected_term_count,
        "matched_term_count": matched_term_count,
        "term_recall": _rate(matched_term_count, expected_term_count),
        "all_terms_sample_rate": _rate(all_terms_samples, sum(1 for record in records if record.get("expected_terms"))),
        "expected_region_count": localization_count,
        "localized_region_count": localization_matches,
        "localization_accuracy_at_iou": _rate(localization_matches, localization_count),
        "ocr_mean_confidence": round(
            confidence_weighted_sum / confidence_region_count if confidence_region_count else 0.0,
            6,
        ),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies, default=0),
        },
        "under_5s_rate": _rate(sum(1 for latency in latencies if latency < 5000), len(latencies)),
        "samples": results,
    }


def _regions_from_chunks(chunks: list[Any]) -> list[dict[str, Any]]:
    regions = []
    for chunk in chunks:
        raw = chunk.metadata.get("ocr_regions_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            regions.extend(item for item in payload if isinstance(item, dict))
    return regions


def _contains(text: str, term: str) -> bool:
    return "".join(str(term).lower().split()) in "".join(str(text).lower().split())


def _bbox_iou(left: Any, right: Any) -> float:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return 0.0
    try:
        left_x1 = float(left["x"])
        left_y1 = float(left["y"])
        left_x2 = left_x1 + float(left["width"])
        left_y2 = left_y1 + float(left["height"])
        right_x1 = float(right["x"])
        right_y1 = float(right["y"])
        right_x2 = right_x1 + float(right["width"])
        right_y2 = right_y1 + float(right["height"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    intersection_width = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    intersection_height = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    intersection = intersection_width * intersection_height
    union = (
        max(0.0, left_x2 - left_x1) * max(0.0, left_y2 - left_y1)
        + max(0.0, right_x2 - right_x1) * max(0.0, right_y2 - right_y1)
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def main() -> int:
    argument_parser = argparse.ArgumentParser(description="Evaluate local image OCR accuracy and latency")
    argument_parser.add_argument("--dataset", required=True, type=Path, help="JSONL dataset path")
    argument_parser.add_argument("--output-json", type=Path, help="Optional report output path")
    args = argument_parser.parse_args()

    dataset_path = args.dataset.resolve()
    report = evaluate_records(load_jsonl(dataset_path), dataset_path.parent)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(serialized + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
