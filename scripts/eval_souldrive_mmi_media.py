"""Evaluate the synthetic media-backed SoulDrive MMI harness.

This runner intentionally separates transcript-only B1 from a structured
media-aware gate.  It verifies that every episode has readable local media,
but it does not claim ASR, OCR, VLM, or Huawei Topic 5 performance.  Real
model runtime measurements belong in a separate run with model provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
from jsonschema import Draft202012Validator
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media
from scripts.eval_souldrive_mmi_480 import event_decision, score, speech, text, text_action

DEFAULT_DATASET = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_media_480_v0.2" / "episodes.jsonl"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "souldrive_pc_mmi_media_480" / "b1_and_structured_candidate_v0.2_20260819"
SCHEMA = ROOT / "eval" / "schemas" / "episode_v0.1.schema.json"


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    return sorted(values)[max(0, min(len(values) - 1, int(len(values) * ratio + 0.999999) - 1))]


def load_rows(path: Path) -> list[dict[str, Any]]:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        errors = list(validator.iter_errors(row))
        if errors:
            raise ValueError(f"{row.get('episode_id')}: {errors[0].message}")
        rows.append(row)
    return rows


def media_headers(row: dict[str, Any], media_root: Path) -> dict[str, Any]:
    """Read only local WAV/PNG headers; this is not ASR or visual inference."""
    audio_ref = next((event.get("payload_ref") for event in row["events"] if event.get("modality") == "audio"), None)
    image_ref = next((event.get("payload_ref") for event in row["events"] if event.get("modality") == "screen"), None)
    if not audio_ref or not image_ref:
        raise ValueError(f"{row['episode_id']}: expected one audio and one screen media reference")
    audio_path = media_root / str(audio_ref)
    image_path = media_root / str(image_ref)
    with wave.open(str(audio_path), "rb") as handle:
        audio = {
            "sample_rate": handle.getframerate(),
            "channels": handle.getnchannels(),
            "sample_width": handle.getsampwidth(),
            "duration_ms": round(handle.getnframes() * 1000 / handle.getframerate()),
        }
    from PIL import Image

    with Image.open(image_path) as image:
        screen = {"width": image.width, "height": image.height, "format": image.format}
    return {"audio_ref": str(audio_ref), "image_ref": str(image_ref), "audio": audio, "screen": screen}


def predict_text_only_timed(rows: list[dict[str, Any]], classifier, vectorizer) -> tuple[list[dict[str, Any]], list[float]]:
    labels = classifier.predict(vectorizer.transform([text(row) for row in rows]))
    predictions, timings = [], []
    for row, label in zip(rows, labels):
        started = time.perf_counter()
        predictions.append({
            "intent": str(label),
            "target_id": None,
            "aligned_event_ids": [speech(row)["event_id"]],
            "conflict_type": "none",
            "clarification_required": False,
            "action": text_action(str(label)),
        })
        timings.append((time.perf_counter() - started) * 1000)
    return predictions, timings


def predict_structured_candidate(
    rows: list[dict[str, Any]], classifier, vectorizer, threshold: float, media_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, float]]]:
    labels = classifier.predict(vectorizer.transform([text(row) for row in rows]))
    predictions, media, timings = [], [], []
    for row, label in zip(rows, labels):
        started = time.perf_counter()
        header_started = time.perf_counter()
        item_media = media_headers(row, media_root)
        header_ms = (time.perf_counter() - header_started) * 1000
        gate_started = time.perf_counter()
        prediction = event_decision(row, str(label), threshold)
        gate_ms = (time.perf_counter() - gate_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000
        predictions.append(prediction)
        media.append(item_media)
        timings.append({"media_header_io": header_ms, "structured_gate": gate_ms, "total": total_ms})
    return predictions, media, timings


def timing_summary(rows: list[dict[str, float]], key: str) -> dict[str, float]:
    values = [item[key] for item in rows]
    return {"p50": statistics.median(values), "p95": percentile(values, 0.95), "max": max(values)}


def repository_snapshot() -> dict[str, str]:
    def run(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=ROOT, check=False, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    tracked = subprocess.run(["git", "diff", "--binary", "--no-ext-diff"], cwd=ROOT, check=False, capture_output=True).stdout
    return {"head": run("rev-parse", "HEAD"), "branch": run("branch", "--show-current"), "tracked_diff_sha256": hashlib.sha256(tracked).hexdigest()}


def checks(row: dict[str, Any], prediction: dict[str, Any]) -> dict[str, bool]:
    truth = row["ground_truth"]
    return {
        "intent": prediction["intent"] == truth["intent"],
        "target": truth.get("target_id") is None or prediction.get("target_id") == truth.get("target_id"),
        "alignment": prediction.get("aligned_event_ids") == truth.get("aligned_event_ids"),
        "conflict": prediction.get("conflict_type") == truth.get("conflict_type"),
        "action": prediction.get("action") == truth.get("expected_action"),
        "clarification": prediction.get("clarification_required") == (truth.get("expected_action") == "clarify"),
    }


def evaluate(dataset: Path, output: Path) -> dict[str, Any]:
    dataset = dataset.resolve()
    media_root = dataset.parent
    rows = load_rows(dataset)
    manifest = json.loads((dataset.parent / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("episode_count") != len(rows):
        raise ValueError("manifest episode_count does not match episodes.jsonl")
    media_errors = [error for row in rows for error in validate_episode_media(row, media_root, require_media=True)]
    if media_errors:
        raise ValueError("media validation failed: " + "; ".join(media_errors[:3]))
    train = [row for row in rows if row["split"] == "train"]
    validation = [row for row in rows if row["split"] == "validation"]
    test = [row for row in rows if row["split"] == "test"]
    if not train or not validation or not test:
        raise ValueError("media evaluation requires train, validation, and test splits")
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True)
    classifier = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42).fit(
        vectorizer.fit_transform([text(row) for row in train]), [row["ground_truth"]["intent"] for row in train]
    )
    validation_candidates = []
    for threshold in (0.4, 0.5, 0.6, 0.7):
        candidate, _, _ = predict_structured_candidate(validation, classifier, vectorizer, threshold, media_root)
        validation_candidates.append({"threshold": threshold, "action_accuracy": score(validation, candidate)["action_accuracy"]})
    threshold = max(validation_candidates, key=lambda item: (item["action_accuracy"], item["threshold"]))["threshold"]
    b1_validation, _ = predict_text_only_timed(validation, classifier, vectorizer)
    b1_test, b1_timings = predict_text_only_timed(test, classifier, vectorizer)
    candidate_test, media, candidate_timings = predict_structured_candidate(test, classifier, vectorizer, threshold, media_root)
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump({"vectorizer": vectorizer, "classifier": classifier}, output / "text_intent_model.joblib")
    predictions = []
    latencies = []
    failures = []
    for row, b1, candidate, item_media, candidate_timing, b1_ms in zip(test, b1_test, candidate_test, media, candidate_timings, b1_timings):
        prediction_row = {
            "episode_id": row["episode_id"],
            "split": row["split"],
            "template_id": row["template_id"],
            "gold": row["ground_truth"],
            "b1_text_only": b1,
            "structured_media_candidate": candidate,
            "media": item_media,
        }
        predictions.append(prediction_row)
        latency = {"episode_id": row["episode_id"], "b1_text_only": {"total": b1_ms}, "structured_media_candidate": candidate_timing}
        latencies.append(latency)
        item_checks = checks(row, candidate)
        if not all(item_checks.values()):
            failures.append({"episode_id": row["episode_id"], "checks": item_checks, "gold": row["ground_truth"], "prediction": candidate})
    report = {
        "schema_version": "souldrive_pc_mmi_media_metrics_v0.2",
        "dataset_id": manifest["dataset_id"],
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "split": {"train": len(train), "validation": len(validation), "test": len(test)},
        "media_gate": {"require_media": True, "valid": True, "audio_episode_count": len(rows), "screenshot_episode_count": len(rows)},
        "b1_text_only_validation": score(validation, b1_validation),
        "b1_text_only_test": score(test, b1_test),
        "candidate_validation_selection": {"parameter": "target_confidence_threshold", "candidates": validation_candidates, "selected": threshold},
        "structured_media_candidate_test": score(test, candidate_test),
        "latency_ms": {
            "b1_text_only": {"p50": statistics.median(b1_timings), "p95": percentile(b1_timings, 0.95), "max": max(b1_timings)},
            "structured_media_candidate": {
                "media_header_io": timing_summary(candidate_timings, "media_header_io"),
                "structured_gate": timing_summary(candidate_timings, "structured_gate"),
                "total": timing_summary(candidate_timings, "total"),
            },
        },
        "failure_count": len(failures),
        "limitations": [
            "This is a synthetic media-backed harness result, not a public benchmark or Huawei Topic 5 score.",
            "B1 uses the provided synthetic transcript only; the candidate reads WAV/PNG headers and structured events, but does not run ASR, OCR, VLM, QLoRA, or a learned multimodal fusion model.",
            "The result proves media-file integrity and target/timestamp gate behavior only. It must not be presented as real speech or visual recognition accuracy.",
        ],
    }
    (output / "predictions.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in predictions), encoding="utf-8")
    (output / "latency.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in latencies), encoding="utf-8")
    (output / "failures.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": output.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "repository": repository_snapshot(),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "conda_environment": "souldrive"},
        "inputs": {"dataset": dataset.relative_to(ROOT).as_posix(), "dataset_sha256": report["dataset_sha256"], "media_index": "media/index.jsonl"},
        "baseline": "B1: TF-IDF char 2-5 + LogisticRegression over provided synthetic transcript; no media access",
        "candidate": "C0: same transcript classifier + validated local WAV/PNG headers + 2500 ms timestamp/project/confidence event gate",
        "validation_selection": report["candidate_validation_selection"],
        "test_used_after_selection": True,
        "outputs": ["predictions.jsonl", "latency.jsonl", "failures.jsonl", "metrics.json", "summary.md"],
        "limitations": report["limitations"],
    }
    (output / "run_manifest.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = "# SoulDrive-PC-MMI 合成媒体 Harness\n\n"
    summary += f"- 数据集：`{report['dataset_id']}`\n- 划分：`{report['split']}`\n"
    summary += f"- B1 test intent accuracy：`{report['b1_text_only_test']['intent_accuracy']:.4f}`\n"
    summary += f"- C0 test action accuracy：`{report['structured_media_candidate_test']['action_accuracy']:.4f}`\n"
    summary += f"- C0 media-header P95：`{report['latency_ms']['structured_media_candidate']['media_header_io']['p95']:.3f} ms`\n\n"
    summary += "## 边界\n\n" + "\n".join(f"- {item}" for item in report["limitations"]) + "\n"
    (output / "summary.md").write_text(summary, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = evaluate(args.dataset, args.output_dir.resolve())
    print(json.dumps({"dataset_id": report["dataset_id"], "output": str(args.output_dir.resolve()), "failure_count": report["failure_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
