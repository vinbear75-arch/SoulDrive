"""Evaluate saved ScreenSpot predictions by platform and target type.

The evaluator consumes already-scored JSONL. It does not download data or
re-score images, and it fails closed when paired baseline/candidate rows do not
describe the same image-instruction examples.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


PLATFORM_ALIASES = {
    "ios": "mobile",
    "android": "mobile",
    "mobile": "mobile",
    "macos": "desktop",
    "windows": "desktop",
    "linux": "desktop",
    "desktop": "desktop",
    "web": "web",
    "browser": "web",
}
TYPE_ALIASES = {
    "text": "text",
    "icon": "icon",
    "widget": "icon",
    "icon/widget": "icon",
}


def load_scored_predictions(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"ScreenSpot result file not found: {source}")
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {source}:line:{line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"ScreenSpot row must be an object at line {line_number}")
            image = str(raw.get("img_filename") or "").strip()
            instruction = str(raw.get("instruction") or "").strip()
            platform = _normalise_label(raw.get("platform"), PLATFORM_ALIASES, "platform", line_number)
            data_type = _normalise_label(raw.get("data_type"), TYPE_ALIASES, "data_type", line_number)
            if not image or not instruction:
                raise ValueError(f"missing image/instruction at {source}:line:{line_number}")
            key = f"{image}\0{instruction}"
            if key in seen_keys:
                raise ValueError(f"duplicate ScreenSpot sample at {source}:line:{line_number}")
            seen_keys.add(key)
            correct = raw.get("correct")
            if not isinstance(correct, bool):
                raise ValueError(f"correct must be boolean at {source}:line:{line_number}")
            missing_prediction = bool(raw.get("missing_prediction", False))
            if missing_prediction and correct:
                raise ValueError(f"missing prediction cannot be correct at {source}:line:{line_number}")
            latency = _optional_non_negative_float(raw.get("latency_ms"), source, line_number)
            rows.append({
                "key": key,
                "img_filename": image,
                "instruction": instruction,
                "platform": platform,
                "data_type": data_type,
                "correct": correct,
                "missing_prediction": missing_prediction,
                "latency_ms": latency,
            })
    if not rows:
        raise ValueError(f"ScreenSpot result file is empty: {source}")
    return rows


def evaluate_screen_spot_subgroups(
    results_path: str | Path,
    *,
    baseline_path: str | Path | None = None,
) -> dict[str, Any]:
    candidate_rows = load_scored_predictions(results_path)
    report = {
        "schema_version": "screenspot_subgroup_eval_v0.1",
        "source_results": str(Path(results_path)),
        "input_kind": "saved_scored_predictions",
        "metrics": _group_metrics(candidate_rows),
        "evaluation_boundary": (
            "This report evaluates saved ScreenSpot predictions only; it is not a "
            "voice, project-RAG, or end-to-end business metric."
        ),
        "provenance": _load_provenance(Path(results_path)),
    }
    if baseline_path is not None:
        baseline_rows = load_scored_predictions(baseline_path)
        candidate_by_key = {row["key"]: row for row in candidate_rows}
        baseline_by_key = {row["key"]: row for row in baseline_rows}
        if set(candidate_by_key) != set(baseline_by_key):
            missing = sorted(set(baseline_by_key) - set(candidate_by_key))
            extra = sorted(set(candidate_by_key) - set(baseline_by_key))
            raise ValueError(
                "ScreenSpot baseline/candidate sample keys do not match: "
                f"missing={missing[:3]}, extra={extra[:3]}"
            )
        for key, row in candidate_by_key.items():
            baseline = baseline_by_key[key]
            if (row["platform"], row["data_type"]) != (baseline["platform"], baseline["data_type"]):
                raise ValueError(f"ScreenSpot labels differ for sample: {key}")
        report["comparison"] = {
            "baseline_results": str(Path(baseline_path)),
            "paired_count": len(candidate_rows),
            "accuracy_delta": round(
                _accuracy(candidate_rows) - _accuracy(baseline_rows),
                12,
            ),
            "by_platform": _paired_group_deltas(candidate_rows, baseline_rows, "platform"),
            "by_type": _paired_group_deltas(candidate_rows, baseline_rows, "data_type"),
        }
    return report


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "overall": _metrics(rows),
        "by_platform": _grouped_metrics(rows, "platform"),
        "by_type": _grouped_metrics(rows, "data_type"),
        "by_platform_and_type": {
            platform: _grouped_metrics(
                [row for row in rows if row["platform"] == platform],
                "data_type",
            )
            for platform in sorted({row["platform"] for row in rows})
        },
    }


def _grouped_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return {
        value: _metrics([row for row in rows if row[field] == value])
        for value in sorted({str(row[field]) for row in rows})
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = sorted(
        float(row["latency_ms"])
        for row in rows
        if row.get("latency_ms") is not None
    )
    return {
        "sample_count": len(rows),
        "correct_count": sum(bool(row["correct"]) for row in rows),
        "accuracy": round(_accuracy(rows), 12),
        "missing_prediction_count": sum(bool(row["missing_prediction"]) for row in rows),
        "latency_count": len(latencies),
        "latency_ms": {
            "p50": _nearest_rank(latencies, 0.50),
            "p95": _nearest_rank(latencies, 0.95),
        },
    }


def _paired_group_deltas(
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    field: str,
) -> dict[str, Any]:
    candidate_groups = {value: [] for value in sorted({row[field] for row in candidate_rows})}
    baseline_groups = {value: [] for value in sorted({row[field] for row in baseline_rows})}
    if set(candidate_groups) != set(baseline_groups):
        raise ValueError(f"ScreenSpot labels differ for {field}")
    for row in candidate_rows:
        candidate_groups[row[field]].append(row)
    for row in baseline_rows:
        baseline_groups[row[field]].append(row)
    return {
        value: {
            "sample_count": len(candidate_groups[value]),
            "baseline_accuracy": round(_accuracy(baseline_groups[value]), 12),
            "candidate_accuracy": round(_accuracy(candidate_groups[value]), 12),
            "accuracy_delta": round(
                _accuracy(candidate_groups[value]) - _accuracy(baseline_groups[value]),
                12,
            ),
        }
        for value in candidate_groups
    }


def _accuracy(rows: list[dict[str, Any]]) -> float:
    return sum(bool(row["correct"]) for row in rows) / max(len(rows), 1)


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(quantile * len(values)) - 1))
    return round(values[index], 6)


def _normalise_label(value: Any, aliases: dict[str, str], field: str, line_number: int) -> str:
    label = str(value or "").strip().lower()
    if label not in aliases:
        raise ValueError(f"unsupported {field} at line {line_number}: {value!r}")
    return aliases[label]


def _optional_non_negative_float(value: Any, source: Path, line_number: int) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid latency at {source}:line:{line_number}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid latency at {source}:line:{line_number}")
    return number


def _load_provenance(results_path: Path) -> dict[str, Any]:
    path = results_path.parent / "data_provenance.json"
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "path": str(path)}
    if not isinstance(payload, dict) or not str(payload.get("dataset") or "").strip():
        return {"status": "invalid", "path": str(path)}
    return {
        "status": "declared",
        "path": str(path),
        "dataset": payload.get("dataset"),
        "source_run": payload.get("source_run"),
        "candidate": payload.get("candidate"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate saved ScreenSpot predictions by subgroup.")
    parser.add_argument("--results", required=True, help="scored_predictions.jsonl")
    parser.add_argument("--baseline", help="paired baseline scored_predictions.jsonl")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()
    report = evaluate_screen_spot_subgroups(args.results, baseline_path=args.baseline)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
