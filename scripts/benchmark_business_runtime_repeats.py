"""Measure the local perception-to-project-RAG response path on fixed inputs.

The supplied query is deliberately recorded as a structured benchmark input.
It is not inferred from the public smoke audio and therefore is not presented
as a speech-to-business-query accuracy result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.knowledge_base import LocalKnowledgeBase
from core.model_runtime import model_runtime_diagnostics
from core.rag_engine import RAGEngine
from core.topic5_runtime import Topic5Runtime
from core.workspace import SoulDriveWorkspace


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int(len(values) * q + 0.999999) - 1))]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": round(statistics.median(values), 3) if values else 0.0,
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


def _dataset_id(collection: Path) -> str:
    manifest = collection / "manifest.json"
    if not manifest.exists():
        return collection.name
    return str(json.loads(manifest.read_text(encoding="utf-8")).get("dataset_id") or collection.name)


def _media_ref(row: dict[str, Any], modalities: set[str], kind: str) -> str:
    for event in row.get("events") or []:
        if event.get("modality") in modalities and event.get("payload_ref"):
            return str(event["payload_ref"])
    raise ValueError(f"{row.get('episode_id')}: missing {kind} media event")


def _events(row: dict[str, Any]) -> list[dict[str, Any]]:
    media_modalities = {"speech_text", "audio", "speech_audio", "screen", "ui_selection"}
    return [event for event in row.get("events") or [] if event.get("modality") not in media_modalities or not event.get("payload_ref")]


def _stage_latency(result: dict[str, Any], name: str) -> float:
    return float(((result.get("stages") or {}).get(name) or {}).get("latency_ms") or 0.0)


def _resources() -> dict[str, float | None]:
    try:
        import psutil

        memory_mib = round(psutil.Process().memory_info().rss / 1024**2, 3)
    except Exception:
        memory_mib = None
    try:
        import torch

        gpu_mib = round(torch.cuda.max_memory_allocated() / 1024**2, 3) if torch.cuda.is_available() else None
    except Exception:
        gpu_mib = None
    return {"process_memory_mib": memory_mib, "torch_peak_gpu_memory_mib": gpu_mib}


def _runtime_environment(workspace_path: str) -> dict[str, Any]:
    try:
        import llama_cpp

        llama = {
            "version": str(getattr(llama_cpp, "__version__", "unknown")),
            "gpu_offload_supported": bool(llama_cpp.llama_supports_gpu_offload()),
        }
    except Exception:
        llama = {"version": "unavailable", "gpu_offload_supported": False}
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "llama_cpp": llama,
        "model_runtime": model_runtime_diagnostics(workspace_path),
    }


def _copy_media(collection: Path, workspace: Path, ref: str) -> str:
    source = (collection / ref).resolve()
    try:
        source.relative_to(collection.resolve())
    except ValueError as exc:
        raise ValueError("fixture media reference escapes collection") from exc
    if not source.is_file():
        raise ValueError(f"fixture media missing: {ref}")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    destination = workspace / "runtime" / "benchmark_media" / f"{digest}{source.suffix.lower()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
    elif hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
        raise ValueError("managed benchmark media hash mismatch")
    return destination.relative_to(workspace).as_posix()


def benchmark(
    collection: Path,
    workspace_path: Path,
    output: Path,
    repeats: int,
    query: str,
    project_id_override: str | None = None,
) -> dict[str, Any]:
    rows = _rows(collection / "episodes.jsonl")
    if not rows:
        raise ValueError("collection is empty")
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if not query.strip():
        raise ValueError("query is required")

    workspace = SoulDriveWorkspace(str(workspace_path)).ensure()
    media = {
        row["episode_id"]: (
            _copy_media(collection, Path(workspace.root_path), _media_ref(row, {"audio", "speech_audio"}, "audio")),
            _copy_media(collection, Path(workspace.root_path), _media_ref(row, {"screen", "ui_selection"}, "screen")),
        )
        for row in rows
    }
    runtime = Topic5Runtime()
    kb = rag = None
    records: list[dict[str, Any]] = []
    try:
        for repeat in range(repeats):
            for row in rows:
                started = time.perf_counter()
                audio_ref, image_ref = media[row["episode_id"]]
                project_id = project_id_override or row.get("project_id") or "project_souldrive_agent"
                perception = runtime.infer(
                    workspace_root=Path(workspace.root_path), audio_ref=audio_ref, image_ref=image_ref,
                    events=_events(row), target_id=(row.get("ground_truth") or {}).get("target_id"),
                    project_id=project_id, run_ocr=True,
                    run_vlm=False, visual_mode="fast", max_new_tokens=24,
                )
                perception_done = time.perf_counter()
                rag_init_ms = 0.0
                if rag is None:
                    init_started = time.perf_counter()
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    rag = RAGEngine(kb=kb, graph_db_path=workspace.graph_db_path, workspace_path=workspace.root_path)
                    rag_init_ms = (time.perf_counter() - init_started) * 1000

                retrieval = {}
                first_model_token_ms = None
                response_started = time.perf_counter()

                def retrieved(value: dict[str, Any]) -> None:
                    retrieval.update(value)

                def first_chunk(_: str) -> None:
                    nonlocal first_model_token_ms
                    if first_model_token_ms is None:
                        first_model_token_ms = (time.perf_counter() - started) * 1000

                interaction_context = {
                    "query_timestamp_ms": int(time.time() * 1000),
                    "active_context": {"project_id": project_id},
                    "events": list(perception.get("perception_events") or []),
                }
                chunks = list(rag.generate_response_stream(
                    query, top_k=6, interaction_context=interaction_context,
                    on_retrieval_completed=retrieved, on_generation_chunk=first_chunk,
                ))
                completed = time.perf_counter()
                first_response_chunk_ms = (completed - started) * 1000 if chunks else None
                response_text = "".join(chunks)
                evidence_count = int(retrieval.get("evidence_count") or 0)
                response_has_citation = bool(re.search(r"\[E\d+\]", response_text))
                evidence_backed_response = evidence_count > 0 and response_has_citation
                statuses = [
                    perception.get("status"),
                    "pass" if chunks else "failed",
                    "pass" if evidence_backed_response else "partial",
                ]
                record_status = "pass" if all(status == "pass" for status in statuses) else (
                    "partial" if perception.get("status") == "pass" and chunks else "failed"
                )
                records.append({
                    "episode_id": row["episode_id"],
                    "repeat": repeat + 1,
                    "status": record_status,
                    "query": query,
                    "query_source": "frozen_structured_business_query",
                    "project_id": project_id,
                    "asr_text": perception.get("speech_text", ""),
                    "response_chars": len(response_text),
                    "response_has_citation": response_has_citation,
                    "evidence_backed_response": evidence_backed_response,
                    "stage_latency_ms": {
                        "asr": _stage_latency(perception, "asr"),
                        "ocr": _stage_latency(perception, "ocr"),
                        "soulalign": _stage_latency(perception, "soulalign"),
                        "perception": round((perception_done - started) * 1000, 3),
                        "rag_initialization": round(rag_init_ms, 3),
                        "retrieval_and_context_gate": retrieval.get("latency_ms"),
                        "model_first_token": round(first_model_token_ms, 3) if first_model_token_ms is not None else None,
                        "first_response_chunk": round(first_response_chunk_ms, 3) if first_response_chunk_ms is not None else None,
                        "complete_response": round((completed - started) * 1000, 3),
                        "rag_response": round((completed - response_started) * 1000, 3),
                    },
                    "retrieval_evidence_count": evidence_count,
                    "resources": _resources(),
                })
    finally:
        if rag is not None:
            rag.close()
        if kb is not None:
            kb.close()
        runtime.close()

    warm = records[1:]
    stages = ("asr", "ocr", "soulalign", "perception", "retrieval_and_context_gate", "model_first_token", "first_response_chunk", "complete_response", "rag_response")
    report = {
        "schema_version": "souldrive_business_runtime_repeat_benchmark_v0.1",
        "dataset_id": _dataset_id(collection),
        "dataset_sha256": hashlib.sha256((collection / "episodes.jsonl").read_bytes()).hexdigest(),
        "workspace": str(Path(workspace.root_path)),
        "environment": _runtime_environment(workspace.root_path),
        "unique_episode_count": len(rows),
        "repeat_count": repeats,
        "measurement_count": len(records),
        "cold_start_measurement_count": min(1, len(records)),
        "warm_measurement_count": len(warm),
        "query_source": "frozen_structured_business_query",
        "project_id_override": project_id_override,
        "pipeline_coverage": {"asr": "measured", "ocr": "measured", "soulalign": "measured", "project_scoped_retrieval": "measured", "model_first_token": "measured", "complete_response": "measured"},
        "warm_stage_latency_ms": {stage: _summary([float(row["stage_latency_ms"][stage]) for row in warm if row["stage_latency_ms"].get(stage) is not None]) for stage in stages},
        "records": records,
        "limitations": [
            "固定公开媒体 fixture 仅验证本机链路，不是公开数据集或华为隐藏集成绩。",
            "业务查询是冻结结构化输入；公开示例音频的 ASR 文本单独保留，未伪称为同一条业务查询。",
            "model_first_token 是模型生成的首 token；为完成引用校验，RAG 当前在验证后才向 UI 输出首个响应块。",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--project-id",
        default=None,
        help="Optional project binding override recorded in the report; does not modify fixture files.",
    )
    parser.add_argument("--repeats", type=int, default=11)
    args = parser.parse_args()
    report = benchmark(
        args.collection_root.resolve(),
        args.workspace.resolve(),
        args.output.resolve(),
        args.repeats,
        args.query,
        args.project_id,
    )
    print(json.dumps({"measurement_count": report["measurement_count"], "warm_complete_response_p95_ms": report["warm_stage_latency_ms"]["complete_response"]["p95"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
