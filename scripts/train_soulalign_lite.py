"""Train and export the lightweight SoulAlign intent head.

This tool deliberately trains on the frozen train split and selects its
confidence threshold on validation only. It does not read or score the sealed
test split, so a trained checkpoint cannot contaminate the existing benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.soulalign import MODEL_SCHEMA_VERSION, build_features


DEFAULT_DATASET = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_480_v0.1" / "episodes.jsonl"
DEFAULT_BASELINE = ROOT / "eval" / "results" / "souldrive_pc_mmi_480" / "candidate_soulalign_v0.1_20260818" / "metrics.json"
DEFAULT_OUTPUT = ROOT / "configs" / "soulalign_lite_v0.1.json"
DEFAULT_REPORT = ROOT / "eval" / "results" / "souldrive_pc_mmi_480" / "soulalign_lite_training_v0.1_20260819.json"
THRESHOLDS = (0.45, 0.55, 0.65, 0.75, 0.85)


def path_reference(path: Path) -> str:
    """Keep repository artifacts portable while preserving external inputs."""
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("dataset is empty")
    for row in rows:
        if row.get("schema_version") != "souldrive_episode_v0.1":
            raise ValueError(f"{row.get('episode_id')}: unsupported episode schema")
        if row.get("split") not in {"train", "validation", "test"}:
            raise ValueError(f"{row.get('episode_id')}: missing frozen split")
        if not isinstance(row.get("ground_truth", {}).get("intent"), str):
            raise ValueError(f"{row.get('episode_id')}: missing intent")
    return rows


def transcript(row: dict[str, Any]) -> str:
    event = next((item for item in row["events"] if item.get("modality") == "speech_text"), None)
    if not event:
        raise ValueError(f"{row.get('episode_id')}: missing speech_text")
    return str((event.get("payload") or {}).get("text") or "")


def features(row: dict[str, Any]) -> dict[str, float]:
    return build_features(transcript(row), list(row["events"]))


def select_threshold(labels: list[str], probabilities: list[list[float]], classes: list[str]) -> tuple[float, list[dict[str, float]]]:
    candidates = []
    for threshold in THRESHOLDS:
        predicted = [
            classes[max(range(len(values)), key=values.__getitem__)] if max(values) >= threshold else "clarify_target"
            for values in probabilities
        ]
        candidates.append({
            "threshold": threshold,
            "accuracy": float(accuracy_score(labels, predicted)),
            "macro_f1": float(f1_score(labels, predicted, average="macro", zero_division=0)),
        })
    # A tie must prefer abstention over an eager learned route. Rules still
    # cover known commands, while unrecognised wording needs strong evidence.
    best = max(candidates, key=lambda item: (item["macro_f1"], item["accuracy"], item["threshold"]))
    return float(best["threshold"]), candidates


def train(dataset: Path, baseline: Path, output: Path, report_path: Path) -> dict[str, Any]:
    if not baseline.is_file():
        raise FileNotFoundError(f"baseline must exist before training: {baseline}")
    rows = load_rows(dataset)
    train_rows = [row for row in rows if row["split"] == "train"]
    validation_rows = [row for row in rows if row["split"] == "validation"]
    if not train_rows or not validation_rows:
        raise ValueError("training requires non-empty train and validation splits")

    vectorizer = DictVectorizer(sparse=True)
    matrix = vectorizer.fit_transform([features(row) for row in train_rows])
    labels = [row["ground_truth"]["intent"] for row in train_rows]
    classifier = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=20260819)
    classifier.fit(matrix, labels)
    validation_matrix = vectorizer.transform([features(row) for row in validation_rows])
    classes = [str(value) for value in classifier.classes_]
    threshold, candidates = select_threshold(
        [row["ground_truth"]["intent"] for row in validation_rows],
        classifier.predict_proba(validation_matrix).tolist(),
        classes,
    )
    checkpoint = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "checkpoint_id": "SoulAlign-Lite-v0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_version": "char_1_to_4_plus_authorized_event_context_v0.1",
        "classes": classes,
        "feature_names": [str(name) for name in vectorizer.get_feature_names_out()],
        "weights": classifier.coef_.tolist(),
        "intercepts": classifier.intercept_.tolist(),
        "minimum_confidence": threshold,
        "training": {
            "dataset": path_reference(dataset),
            "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "split_counts": {
                "train": len(train_rows),
                "validation": len(validation_rows),
                "test_excluded": sum(row["split"] == "test" for row in rows),
            },
            "baseline": path_reference(baseline),
            "algorithm": "DictVectorizer + balanced LogisticRegression(seed=20260819)",
            "test_used": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {
        "schema_version": "souldrive_soulalign_lite_training_report_v0.1",
        "checkpoint": path_reference(output),
        "checkpoint_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "baseline": path_reference(baseline),
        "train_count": len(train_rows),
        "validation_count": len(validation_rows),
        "test_count_excluded": sum(row["split"] == "test" for row in rows),
        "validation_threshold_candidates": candidates,
        "selected_minimum_confidence": threshold,
        "limitations": [
            "The source is the synthetic SoulDrive-PC-MMI v0.1 fixture; this checkpoint is an engineering integration artifact, not a competition model claim.",
            "The sealed test split is excluded from training, threshold selection, and this report.",
            "Rules remain the final safety boundary for destructive requests, confirmation, cancellation, target ambiguity, and external side effects.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    result = train(args.dataset.resolve(), args.baseline.resolve(), args.output.resolve(), args.report.resolve())
    print(json.dumps({"checkpoint": result["checkpoint"], "selected_minimum_confidence": result["selected_minimum_confidence"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
