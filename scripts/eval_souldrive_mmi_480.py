"""Train text B1 and evaluate the SoulAlign event-gated candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
import joblib


ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_480_v0.1" / "episodes.jsonl"
OUTPUT = ROOT / "eval" / "results" / "souldrive_pc_mmi_480" / "candidate_soulalign_v0.1_20260818"


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def speech(row: dict) -> dict:
    return next(event for event in row["events"] if event["modality"] == "speech_text")


def text(row: dict) -> str:
    return speech(row)["payload"]["text"]


def text_action(intent: str) -> str:
    return {"out_of_scope": "refuse", "confirm": "confirm", "cancel": "cancel"}.get(intent, "respond")


def event_decision(row: dict, intent: str, threshold: float) -> dict:
    spoken = speech(row)
    speech_time = spoken["start_ms"]
    aligned = [spoken["event_id"]]
    target_events, all_target_events = [], []
    for event in row["events"]:
        if event is spoken:
            continue
        has_target = event["modality"] in {"ui_selection", "ocr_region"} and event["payload"].get("target_id")
        if has_target:
            all_target_events.append(event)
        if abs(event["start_ms"] - speech_time) <= 2500:
            aligned.append(event["event_id"])
            if has_target:
                target_events.append(event)
    target_ids = {event["payload"]["target_id"] for event in target_events}
    projects = {event["payload"].get("project_id") for event in target_events if event["payload"].get("project_id")}
    conflict = "project_mismatch" if len(projects) > 1 else "none"
    low_confidence = any(event["confidence"] < threshold for event in target_events)
    clarification = intent == "clarify_target" or conflict != "none" or low_confidence or len(all_target_events) != len(target_events)
    action = "refuse" if intent == "out_of_scope" else text_action(intent)
    if action == "respond" and clarification:
        action = "clarify"
    return {
        "intent": intent,
        "target_id": target_events[-1]["payload"]["target_id"] if len(target_ids) == 1 and target_events else None,
        "aligned_event_ids": aligned,
        "conflict_type": conflict,
        "clarification_required": clarification,
        "action": action,
    }


def score(rows: list[dict], predicted: list[dict]) -> dict:
    truth = [row["ground_truth"] for row in rows]
    return {
        "sample_count": len(rows),
        "intent_accuracy": sum(item["intent"] == gold["intent"] for item, gold in zip(predicted, truth)) / len(rows),
        "intent_macro_f1": f1_score([gold["intent"] for gold in truth], [item["intent"] for item in predicted], average="macro"),
        "target_accuracy_on_targeted": sum(item["target_id"] == gold["target_id"] for item, gold in zip(predicted, truth) if gold["target_id"] is not None) / sum(gold["target_id"] is not None for gold in truth),
        "alignment_exact_match": sum(item["aligned_event_ids"] == gold["aligned_event_ids"] for item, gold in zip(predicted, truth)) / len(rows),
        "conflict_accuracy": sum(item["conflict_type"] == gold["conflict_type"] for item, gold in zip(predicted, truth)) / len(rows),
        "action_accuracy": sum(item["action"] == gold["expected_action"] for item, gold in zip(predicted, truth)) / len(rows),
        "clarification_accuracy": sum(item["clarification_required"] == (gold["expected_action"] == "clarify") for item, gold in zip(predicted, truth)) / len(rows),
    }


def predict_text_only(rows: list[dict], classifier, vectorizer) -> list[dict]:
    labels = classifier.predict(vectorizer.transform([text(row) for row in rows]))
    return [{"intent": label, "target_id": None, "aligned_event_ids": [speech(row)["event_id"]], "conflict_type": "none", "clarification_required": False, "action": text_action(label)} for row, label in zip(rows, labels)]


def predict_candidate(rows: list[dict], classifier, vectorizer, threshold: float) -> tuple[list[dict], list[float]]:
    labels = classifier.predict(vectorizer.transform([text(row) for row in rows]))
    predictions, elapsed = [], []
    for row, label in zip(rows, labels):
        started = time.perf_counter()
        predictions.append(event_decision(row, label, threshold))
        elapsed.append((time.perf_counter() - started) * 1000)
    return predictions, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    rows = load_rows(args.dataset.resolve())
    train = [row for row in rows if row["split"] == "train"]
    validation = [row for row in rows if row["split"] == "validation"]
    test = [row for row in rows if row["split"] == "test"]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True)
    matrix = vectorizer.fit_transform([text(row) for row in train])
    classifier = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42).fit(matrix, [row["ground_truth"]["intent"] for row in train])
    b1_validation = score(validation, predict_text_only(validation, classifier, vectorizer))
    candidates = []
    for threshold in (0.4, 0.5, 0.6, 0.7):
        prediction, _ = predict_candidate(validation, classifier, vectorizer, threshold)
        candidates.append((score(validation, prediction)["action_accuracy"], threshold))
    _, threshold = max(candidates)
    b1_test = score(test, predict_text_only(test, classifier, vectorizer))
    candidate_test, latencies = predict_candidate(test, classifier, vectorizer, threshold)
    candidate_metrics = score(test, candidate_test)
    candidate_metrics["latency_ms"] = {"p50": statistics.median(latencies), "p95": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]}
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump({"vectorizer": vectorizer, "classifier": classifier}, output / "text_intent_model.joblib")
    (output / "predictions_test.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidate_test), encoding="utf-8")
    report = {"dataset_id": "SoulDrive-PC-MMI-480-v0.1", "split": {"train": len(train), "validation": len(validation), "test": len(test)}, "b1_text_only_validation": b1_validation, "selected_confidence_threshold": threshold, "b1_text_only_test": b1_test, "candidate_soulalign_test": candidate_metrics, "limitations": ["Synthetic structured episodes; results validate harness behavior, not competition performance.", "No audio waveform, screenshot pixels, VLM, or QLoRA weights are used in this run."]}
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"run_id": output.name, "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(), "training": "TF-IDF char 2-5 + balanced LogisticRegression seed=42", "candidate": "trained intent + timestamp/project/confidence event gate", "validation_selection": {"parameter": "confidence_threshold", "candidates": [0.4, 0.5, 0.6, 0.7], "selected": threshold}, "test_used_after_selection": True}
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
