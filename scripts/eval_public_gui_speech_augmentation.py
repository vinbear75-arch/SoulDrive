"""Evaluate the frozen current runtime on public-GUI/synthetic-speech data."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.topic5_runtime import Topic5Runtime


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def _normalize(text: str) -> str:
    return "".join(ch.lower() for ch in str(text) if ch.isalnum())


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * q + 0.999999) - 1))] if ordered else 0.0


def evaluate(dataset_root: Path, output: Path) -> dict[str, Any]:
    episodes_path = dataset_root / "episodes.jsonl"
    rows = [json.loads(line) for line in episodes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    runtime = Topic5Runtime()
    records = []
    try:
        for index, row in enumerate(rows):
            speech = next(event for event in row["events"] if event["modality"] == "speech_text")["payload"]["text"]
            audio = next(event for event in row["events"] if event["modality"] == "audio")["payload_ref"]
            screen = next(event for event in row["events"] if event["modality"] == "screen")["payload_ref"]
            target = row["ground_truth"].get("target_id")
            events = [event for event in row["events"] if event["modality"] not in {"speech_text", "audio", "screen"}]
            started = time.perf_counter()
            result = runtime.infer(workspace_root=dataset_root, audio_ref=audio, image_ref=screen, events=events, target_id=target, project_id=row.get("project_id"), run_ocr=True, run_vlm=False, visual_mode="fast")
            predicted_text = str(result.get("speech_text") or "")
            reference = _normalize(speech)
            prediction = _normalize(predicted_text)
            decision = result.get("decision") or {}
            records.append({
                "episode_id": row["episode_id"], "run_order": index, "reference_text": speech, "predicted_text": predicted_text,
                "cer": _distance(reference, prediction) / max(1, len(reference)),
                "ocr_status": (result.get("stages") or {}).get("ocr", {}).get("status"),
                "predicted_intent": decision.get("intent"), "predicted_action": decision.get("action"),
                "target_correct": decision.get("target_id") == target,
                "wall_clock_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "runtime": result,
            })
    finally:
        runtime.close()
    latencies = [record["wall_clock_latency_ms"] for record in records]
    warm = latencies[1:]
    metrics = {
        "schema_version": "souldrive_public_gui_speech_baseline_v0.1",
        "dataset_sha256": hashlib.sha256(episodes_path.read_bytes()).hexdigest(),
        "sample_count": len(records),
        "asr_mean_cer": statistics.mean(record["cer"] for record in records),
        "asr_exact_match": sum(record["cer"] == 0 for record in records) / len(records),
        "ocr_pass_rate": sum(record["ocr_status"] == "pass" for record in records) / len(records),
        "target_binding_accuracy": sum(record["target_correct"] for record in records) / len(records),
        "intent_distribution": dict(Counter(str(record["predicted_intent"]) for record in records)),
        "action_distribution": dict(Counter(str(record["predicted_action"]) for record in records)),
        "cold_start_ms": latencies[0],
        "warm_latency_ms": {"count": len(warm), "p50": statistics.median(warm), "p95": _percentile(warm, 0.95), "max": max(warm)},
        "claim_policy": {"baseline_only": True, "real_user_speech": False, "public_benchmark_score": False, "huawei_score": False},
        "limitations": ["ScreenSpot 真实 GUI 与合成中文语音的派生训练集，不是官方 ScreenSpot 协议。", "显式 target_id 测量对齐管线，不等价于从截图自主预测 bbox。", "当前 baseline 不包含项目检索或生成回答。"],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "predictions.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metrics = evaluate(args.dataset_root.resolve(), args.output.resolve())
    print(json.dumps({key: metrics[key] for key in ("sample_count", "asr_mean_cer", "ocr_pass_rate", "target_binding_accuracy", "warm_latency_ms")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
