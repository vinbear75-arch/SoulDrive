"""Run a real local ASR + OCR + SoulAlign evaluation on the frozen media test set.

This is intentionally a runtime experiment, not a competition benchmark.
It uses only the frozen train split to fit B1 and a confidence threshold fixed
from validation (`0.7` by default).  The test labels are read only after
inference to compute metrics.  VLM is disabled on the fast path and can be
enabled for a small, fixed prefix with ``--vlm-smoke-count``.

Run this script inside the CUDA-enabled ``pytorch`` Conda environment.
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media
from core.topic5_runtime import Topic5Runtime

DATASET = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_media_480_v0.2" / "episodes.jsonl"
OUTPUT = ROOT / "eval" / "results" / "souldrive_pc_mmi_media_480" / "runtime_asr_ocr_soulalign_v0.2_20260819"
DEFAULT_THRESHOLD = 0.7


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        required = {"schema_version", "episode_id", "split", "events", "ground_truth"}
        missing = required.difference(row)
        if row.get("schema_version") != "souldrive_episode_v0.1" or missing:
            raise ValueError(f"{row.get('episode_id')}: invalid frozen episode shape; missing={sorted(missing)}")
        rows.append(row)
    return rows


def speech_event(row: dict[str, Any]) -> dict[str, Any]:
    return next(event for event in row["events"] if event.get("modality") == "speech_text")


def transcript(row: dict[str, Any]) -> str:
    return str((speech_event(row).get("payload") or {}).get("text") or "")


def audio_ref(row: dict[str, Any]) -> str:
    value = next((event.get("payload_ref") for event in row["events"] if event.get("modality") == "audio"), None)
    if not value:
        raise ValueError(f"{row['episode_id']}: missing audio event")
    return str(value)


def image_ref(row: dict[str, Any]) -> str:
    value = next((event.get("payload_ref") for event in row["events"] if event.get("modality") == "screen"), None)
    if not value:
        raise ValueError(f"{row['episode_id']}: missing screen event")
    return str(value)


def b1_action(intent: str) -> str:
    return {"out_of_scope": "refuse", "confirm": "confirm", "cancel": "cancel"}.get(intent, "respond")


def simple_b1(rows: list[dict[str, Any]], classifier, vectorizer) -> list[dict[str, Any]]:
    labels = classifier.predict(vectorizer.transform([transcript(row) for row in rows]))
    return [
        {
            "intent": str(intent),
            "target_id": None,
            "conflict_type": "none",
            "clarification_required": False,
            "action": b1_action(str(intent)),
        }
        for intent in labels
    ]


def explicit_target_id(row: dict[str, Any], *, window_ms: int = 2500) -> str | None:
    """Use only an unambiguous, explicitly selected local UI target."""
    started = int(speech_event(row).get("start_ms") or 0)
    targets = {
        str((event.get("payload") or {}).get("target_id"))
        for event in row["events"]
        if event.get("modality") in {"ui_selection", "ocr_region"}
        and (event.get("payload") or {}).get("target_id")
        and abs(int(event.get("start_ms") or 0) - started) <= window_ms
    }
    return next(iter(targets)) if len(targets) == 1 else None


def runtime_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Exclude transcript/audio/screenshot files; keep local UI/profile context."""
    return [
        {
            "event_id": event.get("event_id"),
            "modality": event.get("modality"),
            "start_ms": event.get("start_ms"),
            "end_ms": event.get("end_ms"),
            "confidence": event.get("confidence"),
            "payload": event.get("payload") or {},
        }
        for event in row["events"]
        if event.get("modality") not in {"speech_text", "audio", "screen"}
    ]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return sorted(values)[max(0, min(len(values) - 1, int(len(values) * q + 0.999999) - 1))]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {"count": len(values), "p50": statistics.median(values) if values else 0.0, "p95": _percentile(values, 0.95), "max": max(values) if values else 0.0}


def _gpu_memory() -> dict[str, float | None]:
    try:
        import torch

        if not torch.cuda.is_available():
            return {"allocated_mib": None, "reserved_mib": None, "peak_allocated_mib": None}
        return {
            "allocated_mib": round(torch.cuda.memory_allocated() / 1024**2, 2),
            "reserved_mib": round(torch.cuda.memory_reserved() / 1024**2, 2),
            "peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 1024**2, 2),
        }
    except Exception:
        return {"allocated_mib": None, "reserved_mib": None, "peak_allocated_mib": None}


def _reset_gpu_peak() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def candidate_prediction(result: dict[str, Any]) -> dict[str, Any]:
    decision = result.get("decision")
    if not isinstance(decision, dict):
        return {
            "intent": "__no_decision__",
            "target_id": None,
            "conflict_type": "__no_decision__",
            "clarification_required": False,
            "action": "__blocked__",
        }
    return {
        "intent": str(decision.get("intent") or "__no_decision__"),
        "target_id": decision.get("target_id"),
        "conflict_type": str(decision.get("conflict_type") or "__no_decision__"),
        "clarification_required": bool(decision.get("clarification_required")),
        "action": str(decision.get("action") or "__blocked__"),
    }


def score(rows: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    truth = [row["ground_truth"] for row in rows]
    targeted = [(gold, prediction) for gold, prediction in zip(truth, predictions) if gold.get("target_id") is not None]
    return {
        "sample_count": len(rows),
        "intent_accuracy": sum(pred["intent"] == gold["intent"] for gold, pred in zip(truth, predictions)) / len(rows),
        "intent_macro_f1": f1_score([gold["intent"] for gold in truth], [pred["intent"] for pred in predictions], average="macro", zero_division=0),
        "target_binding_accuracy_on_targeted": sum(pred["target_id"] == gold["target_id"] for gold, pred in targeted) / len(targeted) if targeted else None,
        "conflict_accuracy": sum(pred["conflict_type"] == gold["conflict_type"] for gold, pred in zip(truth, predictions)) / len(rows),
        "action_accuracy": sum(pred["action"] == gold["expected_action"] for gold, pred in zip(truth, predictions)) / len(rows),
        "clarification_accuracy": sum(pred["clarification_required"] == (gold["expected_action"] == "clarify") for gold, pred in zip(truth, predictions)) / len(rows),
    }


def _repository_state() -> dict[str, str]:
    def command(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=ROOT, check=False, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff"], cwd=ROOT, check=False, capture_output=True).stdout
    return {"head": command("rev-parse", "HEAD"), "branch": command("branch", "--show-current"), "tracked_diff_sha256": hashlib.sha256(diff).hexdigest()}


def _stage_summary(records: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    selected = [record.get("runtime", {}).get("stages", {}).get(stage, {}) for record in records]
    statuses = Counter(str(item.get("status", "missing")) for item in selected)
    values = [float(item["latency_ms"]) for item in selected if isinstance(item.get("latency_ms"), (int, float))]
    return {"statuses": dict(statuses), "latency_ms": _summary(values)}


def evaluate(
    dataset: Path,
    output: Path,
    *,
    confidence_threshold: float = DEFAULT_THRESHOLD,
    max_samples: int | None = None,
    vlm_smoke_count: int = 0,
) -> dict[str, Any]:
    dataset = dataset.resolve()
    rows = read_rows(dataset)
    manifest = json.loads((dataset.parent / "manifest.json").read_text(encoding="utf-8"))
    errors = [error for row in rows for error in validate_episode_media(row, dataset.parent, require_media=True)]
    if errors:
        raise ValueError("media validation failed: " + "; ".join(errors[:3]))
    train = [row for row in rows if row["split"] == "train"]
    validation = [row for row in rows if row["split"] == "validation"]
    test = [row for row in rows if row["split"] == "test"]
    if max_samples is not None:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        # This deterministic prefix is selected by frozen file order, not labels.
        test = test[:max_samples]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True)
    classifier = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42).fit(
        vectorizer.fit_transform([transcript(row) for row in train]), [row["ground_truth"]["intent"] for row in train]
    )
    b1 = simple_b1(test, classifier, vectorizer)
    runtime = Topic5Runtime()
    status_before = runtime.status()
    if not status_before["gpu"].get("cuda_available"):
        raise RuntimeError("CUDA is required for the actual ASR runtime; use the pytorch environment")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, row in enumerate(test):
        _reset_gpu_peak()
        start = time.perf_counter()
        result = runtime.infer(
            workspace_root=dataset.parent,
            audio_ref=audio_ref(row),
            image_ref=image_ref(row),
            events=runtime_events(row),
            target_id=explicit_target_id(row),
            project_id=None,
            run_ocr=True,
            run_vlm=index < vlm_smoke_count,
            confidence_threshold=confidence_threshold,
            audio_start_ms=int(speech_event(row).get("start_ms") or 0),
            image_start_ms=next((int(event.get("start_ms") or 0) for event in row["events"] if event.get("modality") == "screen"), 0),
            max_new_tokens=32,
        )
        wall_ms = (time.perf_counter() - start) * 1000
        prediction = candidate_prediction(result)
        record = {
            "episode_id": row["episode_id"],
            "split": row["split"],
            "template_id": row["template_id"],
            "run_order": index,
            "vlm_requested": index < vlm_smoke_count,
            "input": {"audio_ref": audio_ref(row), "image_ref": image_ref(row), "explicit_target_id": explicit_target_id(row)},
            "gold": row["ground_truth"],
            "b1_original_transcript": {"transcript": transcript(row), "prediction": b1[index]},
            "runtime": result,
            "candidate_prediction": prediction,
            "wall_clock_latency_ms": round(wall_ms, 3),
            "gpu_memory_mib": _gpu_memory(),
        }
        records.append(record)
        if result.get("status") not in {"pass", "partial"} or result.get("diagnostics"):
            failures.append({"episode_id": row["episode_id"], "kind": "runtime", "status": result.get("status"), "diagnostics": result.get("diagnostics", []), "stages": result.get("stages", {})})
        if prediction["action"] != row["ground_truth"]["expected_action"]:
            failures.append({"episode_id": row["episode_id"], "kind": "decision", "gold": row["ground_truth"], "candidate": prediction, "asr_text": result.get("speech_text", "")})
    candidate = [record["candidate_prediction"] for record in records]
    runtime_total = [float(record["runtime"].get("latency_ms", {}).get("total", 0.0)) for record in records]
    wall_total = [float(record["wall_clock_latency_ms"]) for record in records]
    fast_path_records = [record for record in records if not record["vlm_requested"]]
    fast_path_hot_records = [record for record in fast_path_records if record["run_order"] > 0]
    vlm_records = [record for record in records if record["vlm_requested"]]
    def latency_for(selected: list[dict[str, Any]], key: str) -> list[float]:
        if key == "runtime":
            return [float(item["runtime"].get("latency_ms", {}).get("total", 0.0)) for item in selected]
        return [float(item["wall_clock_latency_ms"]) for item in selected]
    result = {
        "schema_version": "souldrive_pc_mmi_media_runtime_metrics_v0.2",
        "dataset_id": manifest["dataset_id"],
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "evaluated_split": "test",
        "evaluated_count": len(test),
        "full_test": len(test) == sum(row["split"] == "test" for row in rows),
        "selection_policy": "frozen test file order; no test labels used for threshold/model selection",
        "threshold": {"value": confidence_threshold, "source": "validation-only selection from B1/Candidate harness"},
        "b1_original_transcript_test": score(test, b1),
        "candidate_actual_asr_ocr_soulalign_test": score(test, candidate),
        "runtime": {
            "status_before": status_before,
            "vlm_smoke_count": vlm_smoke_count,
            "stage_summary": {name: _stage_summary(records, name) for name in ("asr", "ocr", "vlm", "soulalign")},
            "runtime_total_latency_ms": _summary(runtime_total),
            "wall_clock_latency_ms": _summary(wall_total),
            "cold_start": {
                "episode_id": records[0]["episode_id"] if records else None,
                "runtime_total_ms": runtime_total[0] if runtime_total else None,
                "wall_clock_ms": wall_total[0] if wall_total else None,
            },
            "fast_path_all_latency_ms": {"runtime": _summary(latency_for(fast_path_records, "runtime")), "wall_clock": _summary(latency_for(fast_path_records, "wall"))},
            "fast_path_hot_latency_ms": {"runtime": _summary(latency_for(fast_path_hot_records, "runtime")), "wall_clock": _summary(latency_for(fast_path_hot_records, "wall"))},
            "vlm_sample_latency_ms": {"runtime": _summary(latency_for(vlm_records, "runtime")), "wall_clock": _summary(latency_for(vlm_records, "wall"))},
            "gpu_peak_allocated_mib": _summary([float(record["gpu_memory_mib"]["peak_allocated_mib"] or 0.0) for record in records]),
        },
        "failure_count": len(failures),
        "limitations": [
            "This is an actual local SenseVoice + RapidOCR + SoulAlign runtime run on synthetic TTS/UI media, not a Huawei Topic 5 competition score.",
            "The test split is used only once for post-hoc evaluation. Threshold 0.7 is fixed before this run from validation; no test-driven tuning is permitted.",
            "B1 sees the provided synthetic transcript; Candidate transcribes local WAV and reads local synthetic screenshots. The two scores do not measure a public benchmark or human interaction quality.",
            "VLM is sampled only on a fixed prefix when requested. Fast-path P95 and VLM-sampled P95 must be reported separately and cannot be averaged into a competition claim.",
        ],
    }
    (output / "predictions.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (output / "latency.jsonl").write_text("".join(json.dumps({"episode_id": record["episode_id"], "runtime": record["runtime"].get("stages", {}), "runtime_total": record["runtime"].get("latency_ms", {}), "wall_clock_latency_ms": record["wall_clock_latency_ms"], "gpu_memory_mib": record["gpu_memory_mib"]}, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (output / "failures.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": output.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": _repository_state(),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "conda_environment": "pytorch"},
        "inputs": {"dataset": dataset.relative_to(ROOT).as_posix(), "dataset_sha256": result["dataset_sha256"], "media_validated": True},
        "baseline": "B1: synthetic transcript only, TF-IDF char 2-5 + balanced LogisticRegression trained on train split",
        "candidate": "SenseVoiceSmall CUDA ASR + RapidOCR + SoulAlign; VLM disabled except fixed requested smoke prefix",
        "test_used_for_model_selection": False,
        "validation_only_parameters": result["threshold"],
        "outputs": ["predictions.jsonl", "latency.jsonl", "failures.jsonl", "metrics.json", "summary.md"],
        "limitations": result["limitations"],
    }
    (output / "run_manifest.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = "# 合成媒体真实 Runtime 评测\n\n"
    summary += f"- 测试样本：`{len(test)}`；完整封闭 test：`{result['full_test']}`\n"
    summary += f"- B1 原转写 intent accuracy：`{result['b1_original_transcript_test']['intent_accuracy']:.4f}`\n"
    summary += f"- Candidate ASR+OCR+SoulAlign intent accuracy：`{result['candidate_actual_asr_ocr_soulalign_test']['intent_accuracy']:.4f}`\n"
    summary += f"- Candidate fast-path（热运行）P95：`{result['runtime']['fast_path_hot_latency_ms']['runtime']['p95']:.3f} ms`\n"
    summary += f"- 冷启动首条 runtime：`{result['runtime']['cold_start']['runtime_total_ms']:.3f} ms`\n"
    summary += f"- ASR status：`{result['runtime']['stage_summary']['asr']['statuses']}`\n"
    summary += f"- OCR status：`{result['runtime']['stage_summary']['ocr']['statuses']}`\n\n"
    summary += "## 限制\n\n" + "\n".join(f"- {item}" for item in result["limitations"]) + "\n"
    (output / "summary.md").write_text(summary, encoding="utf-8")
    runtime.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--max-samples", type=int, default=None, help="固定 test 文件顺序的前 N 条，仅用于 smoke")
    parser.add_argument("--vlm-smoke-count", type=int, default=0, help="仅固定前 N 条请求 VLM，默认快路径不启用")
    args = parser.parse_args()
    if not 0.0 <= args.confidence_threshold <= 1.0:
        parser.error("confidence threshold must be in [0, 1]")
    if args.vlm_smoke_count < 0:
        parser.error("vlm smoke count must be non-negative")
    report = evaluate(
        args.dataset,
        args.output_dir.resolve(),
        confidence_threshold=args.confidence_threshold,
        max_samples=args.max_samples,
        vlm_smoke_count=args.vlm_smoke_count,
    )
    print(json.dumps({"output": str(args.output_dir.resolve()), "evaluated_count": report["evaluated_count"], "full_test": report["full_test"], "failure_count": report["failure_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
