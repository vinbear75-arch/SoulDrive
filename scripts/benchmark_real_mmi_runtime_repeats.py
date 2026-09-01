"""Benchmark repeated local perception runs on the user-authorized pilot.

This is a runtime diagnostic, not a competition score.  It deliberately keeps
the number of unique episodes separate from repeated measurements and marks
retrieval/answer stages as not integrated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.topic5_runtime import Topic5Runtime


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * q + 0.999999) - 1))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": statistics.median(values) if values else 0.0,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else 0.0,
    }


def _media_ref(row: dict[str, Any], modalities: set[str], kind: str) -> str:
    for event in row["events"]:
        if event.get("modality") in modalities and event.get("payload_ref"):
            return str(event["payload_ref"])
    raise ValueError(f"{row.get('episode_id')}: missing {kind} media event")


def _events(row: dict[str, Any]) -> list[dict[str, Any]]:
    media_modalities = {"speech_text", "audio", "speech_audio", "screen"}
    return [event for event in row["events"] if event.get("modality") not in media_modalities or not event.get("payload_ref")]


def benchmark(collection: Path, output: Path, repeats: int) -> dict[str, Any]:
    rows = _rows(collection / "episodes.jsonl")
    if not rows:
        raise ValueError("pilot collection is empty")
    if repeats < 1:
        raise ValueError("repeats must be positive")

    runtime = Topic5Runtime()
    records: list[dict[str, Any]] = []
    try:
        for repeat in range(repeats):
            for row in rows:
                audio_ref = _media_ref(row, {"audio", "speech_audio"}, "audio")
                image_ref = _media_ref(row, {"screen", "ui_selection"}, "screen")
                target_id = row["ground_truth"].get("target_id")
                started = time.perf_counter()
                result = runtime.infer(
                    workspace_root=collection,
                    audio_ref=audio_ref,
                    image_ref=image_ref,
                    events=_events(row),
                    target_id=target_id,
                    project_id=row.get("project_id"),
                    run_ocr=True,
                    run_vlm=False,
                    visual_mode="fast",
                    max_new_tokens=24,
                )
                records.append({
                    "episode_id": row["episode_id"],
                    "repeat": repeat + 1,
                    "status": result.get("status"),
                    "wall_clock_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "runtime_latency_ms": float((result.get("latency_ms") or {}).get("total") or 0.0),
                    "stage_latency_ms": {
                        name: float(stage.get("latency_ms") or 0.0)
                        for name, stage in (result.get("stages") or {}).items()
                        if isinstance(stage, dict) and stage.get("latency_ms") is not None
                    },
                    "retrieval": {"status": "not_integrated"},
                    "first_token": {"status": "not_integrated"},
                    "complete_response": {"status": "not_integrated"},
                })
    finally:
        runtime.close()

    # The first request initializes local ASR/OCR models; every later request is warm.
    warm = records[1:]
    wall = [record["wall_clock_latency_ms"] for record in warm]
    runtime_total = [record["runtime_latency_ms"] for record in warm]
    report = {
        "schema_version": "souldrive_runtime_repeat_benchmark_v0.2",
        "dataset_id": _dataset_id(collection),
        "dataset_sha256": hashlib.sha256((collection / "episodes.jsonl").read_bytes()).hexdigest(),
        "unique_episode_count": len(rows),
        "repeat_count": repeats,
        "measurement_count": len(records),
        "cold_start_measurement_count": min(1, len(records)),
        "warm_measurement_count": len(warm),
        "latency_ms": {
            "warm_wall_clock": _summary(wall),
            "warm_runtime_total": _summary(runtime_total),
        },
        "stage_latency_ms": {
            stage: _summary([
                float(record["stage_latency_ms"].get(stage) or 0.0)
                for record in warm
                if stage in record["stage_latency_ms"]
            ])
            for stage in ("asr", "ocr", "soulalign")
        },
        "pipeline_coverage": {
            "perception": "measured",
            "retrieval": "not_integrated",
            "first_token": "not_integrated",
            "complete_response": "not_integrated",
        },
        "records": records,
        "limitations": [
            "重复运行不增加独立 episode 数；固定 fixture 仅用于本机 Runtime 回归。",
            "仅测量真实 ASR + OCR + SoulAlign 快路径。",
            "检索、首 token 和完整回答尚未接入该 Harness，不能据此宣称赛题五完整 P95。",
            "不是公开数据集成绩，也不是华为隐藏集成绩。",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _dataset_id(collection: Path) -> str:
    manifest_path = collection / "manifest.json"
    if not manifest_path.exists():
        return collection.name
    try:
        return str(json.loads(manifest_path.read_text(encoding="utf-8")).get("dataset_id") or collection.name)
    except json.JSONDecodeError:
        return collection.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    report = benchmark(args.collection_root.resolve(), args.output.resolve(), args.repeats)
    print(json.dumps({
        "unique_episode_count": report["unique_episode_count"],
        "measurement_count": report["measurement_count"],
        "warm_p95_ms": report["latency_ms"]["warm_wall_clock"]["p95"],
        "pipeline_coverage": report["pipeline_coverage"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
