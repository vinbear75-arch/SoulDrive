"""Re-run one user-authorized real MMI episode through the visual slow path.

This is an acceptance smoke test for the actual local ASR + OCR + VLM +
SoulAlign runtime.  It records raw stage outputs and does not claim a
competition score or a complete RAG answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media
from core.topic5_runtime import Topic5Runtime


def _load_rows(collection: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (collection / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event(row: dict[str, Any], modality: str) -> dict[str, Any]:
    return next(event for event in row["events"] if event.get("modality") == modality)


def _audio_info(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as handle:
        return {
            "bytes": path.stat().st_size,
            "channels": handle.getnchannels(),
            "sample_width": handle.getsampwidth(),
            "sample_rate": handle.getframerate(),
            "frames": handle.getnframes(),
            "duration_s": round(handle.getnframes() / handle.getframerate(), 3),
        }


def _image_info(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        return {"bytes": path.stat().st_size, "format": image.format, "size": list(image.size)}


def evaluate(
    collection: Path,
    episode_id: str,
    output: Path,
    *,
    warmup_vlm: bool = False,
) -> dict[str, Any]:
    collection = collection.resolve()
    rows = _load_rows(collection)
    row = next((item for item in rows if item.get("episode_id") == episode_id), None)
    if row is None:
        raise ValueError(f"episode not found: {episode_id}")

    errors = validate_episode_media(row, collection, require_media=True)
    if errors:
        raise ValueError("media validation failed: " + "; ".join(errors))

    audio_event = _event(row, "audio")
    screen_event = _event(row, "screen")
    audio_ref = str(audio_event["payload_ref"])
    image_ref = str(screen_event["payload_ref"])
    audio_path = (collection / audio_ref).resolve()
    image_path = (collection / image_ref).resolve()
    events = [
        event
        for event in row["events"]
        if event.get("modality") not in {"speech_text", "audio", "screen"}
    ]

    runtime = Topic5Runtime()
    status_before = runtime.status()
    warmup_started = time.perf_counter()
    warmup_result = runtime.warmup_vlm() if warmup_vlm else None
    warmup_wall_clock_ms = round((time.perf_counter() - warmup_started) * 1000, 3) if warmup_vlm else None
    started = time.perf_counter()
    result = runtime.infer(
        workspace_root=collection,
        audio_ref=audio_ref,
        image_ref=image_ref,
        events=events,
        # Do not inject gold target/project values into the runtime.  The
        # authorized selection event is the only context available during
        # this re-test; gold is used only for post-inference comparison.
        target_id=None,
        project_id=None,
        run_ocr=True,
        run_vlm=True,
        visual_mode="slow",
        confidence_threshold=0.7,
        audio_start_ms=int(audio_event.get("start_ms") or 0),
        image_start_ms=int(screen_event.get("start_ms") or 0),
        max_new_tokens=48,
    )
    wall_clock_ms = round((time.perf_counter() - started) * 1000, 3)
    status_after = runtime.status()
    decision = result.get("decision") or {}
    gold = row.get("ground_truth") or {}
    candidate = {
        "intent": decision.get("intent"),
        "target_id": decision.get("target_id"),
        "conflict_type": decision.get("conflict_type"),
        "action": decision.get("action"),
        "clarification_required": bool(decision.get("clarification_required")),
    }
    mismatches = {
        "intent": candidate["intent"] != gold.get("intent"),
        "target_id": candidate["target_id"] != gold.get("target_id"),
        "action": candidate["action"] != gold.get("expected_action"),
    }
    report = {
        "schema_version": "souldrive_real_mmi_vlm_retest_v0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "episode_id": episode_id,
        "collection": {
            "dataset_id": "SoulDrive-PC-MMI-Real-pilot-v0.1",
            "episodes_sha256": hashlib.sha256((collection / "episodes.jsonl").read_bytes()).hexdigest(),
            "source": row.get("provenance"),
        },
        "input": {
            "audio_ref": audio_ref,
            "screen_ref": image_ref,
            "audio": _audio_info(audio_path),
            "screen": _image_info(image_path),
            "project_id": row.get("project_id"),
            "explicit_target_id": (row.get("ground_truth") or {}).get("target_id"),
            "events": events,
        },
        "runtime_status_before": status_before,
        "runtime_status_after": status_after,
        "vlm_warmup": {
            "requested": warmup_vlm,
            "wall_clock_latency_ms": warmup_wall_clock_ms,
            "result": warmup_result,
        },
        "raw": {
            "status": result.get("status"),
            "speech_text": result.get("speech_text"),
            "stages": result.get("stages"),
            "perception_events": result.get("perception_events"),
            "decision": decision,
            "diagnostics": result.get("diagnostics"),
            "policy": result.get("policy"),
            "latency_ms": result.get("latency_ms"),
        },
        "gold": gold,
        "candidate": candidate,
        "mismatches": mismatches,
        "wall_clock_latency_ms": wall_clock_ms,
        "acceptance_checks": {
            "audio_file_readable": bool(audio_path.is_file()),
            "screen_file_readable": bool(image_path.is_file()),
            "asr_pass": (result.get("stages") or {}).get("asr", {}).get("status") == "pass",
            "ocr_pass": (result.get("stages") or {}).get("ocr", {}).get("status") == "pass",
            "vlm_pass": (result.get("stages") or {}).get("vlm", {}).get("status") == "pass",
            "vlm_warmup_pass": (
                not warmup_vlm
                or (warmup_result or {}).get("status") == "pass"
            ),
            "slow_route_used": (result.get("stages") or {}).get("visual_route", {}).get("route") == "slow",
            "soulalign_pass": (result.get("stages") or {}).get("soulalign", {}).get("status") == "pass",
            "target_bound_to_expected": candidate["target_id"] == gold.get("target_id"),
            "project_context_available": any(
                isinstance(event, dict)
                and event.get("payload", {}).get("project_id") == row.get("project_id")
                for event in events
            ),
            "no_side_effect": (result.get("policy") or {}).get("side_effect") == "none",
        },
        "limitations": [
            "本次只验证一条用户授权 pilot 的本地感知/对齐链路，不是比赛成绩。",
            "没有把 RAG 检索、回答生成或 PPT/邮件副作用接入本次判定。",
            "VLM 慢路径的延迟与快路径分开记录，不能合并成一个 P95。",
            *( ["显式 VLM 预热成本单独记录；预热后的请求耗时仍未达到竞赛门槛时，不得把预热隐藏为零成本。"] if warmup_vlm else []),
        ],
    }
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "souldrive_run_manifest_v0.1",
                "run_id": output.name,
                "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "conda_environment": "pytorch"},
                "dataset_sha256": report["collection"]["episodes_sha256"],
                "episode_id": episode_id,
                "visual_mode": "slow",
                "vlm_warmup_requested": warmup_vlm,
                "test_used_for_model_selection": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checks = report["acceptance_checks"]
    summary = [
        "# 真实授权 pilot VLM 慢路径复测",
        "",
        f"- episode：`{episode_id}`",
        f"- 显式 VLM 预热：`{warmup_vlm}`；预热耗时：`{warmup_wall_clock_ms if warmup_wall_clock_ms is not None else 'not_requested'} ms`",
        f"- 状态：`{report['raw']['status']}`；总耗时：`{wall_clock_ms:.3f} ms`",
        f"- ASR：`{checks['asr_pass']}`；OCR：`{checks['ocr_pass']}`；VLM：`{checks['vlm_pass']}`；SoulAlign：`{checks['soulalign_pass']}`",
        f"- 实际路由：`{(result.get('stages') or {}).get('visual_route', {}).get('route')}`",
        f"- ASR 原文：`{result.get('speech_text')}`",
        f"- VLM 原文：`{(result.get('stages') or {}).get('vlm', {}).get('text', '')}`",
        f"- 目标绑定：`{candidate['target_id']}`（期望 `{gold.get('target_id')}`）",
        f"- 项目上下文来自授权事件：`{checks['project_context_available']}`；副作用：`{(result.get('policy') or {}).get('side_effect')}`",
        "",
        "## 限制",
        "",
        *[f"- {item}" for item in report["limitations"]],
    ]
    (output / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    runtime.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--warmup-vlm",
        action="store_true",
        help="在真实慢路径请求前显式加载本地 VLM，并单独记录预热成本",
    )
    args = parser.parse_args()
    report = evaluate(
        args.collection_root,
        args.episode_id,
        args.output_dir,
        warmup_vlm=args.warmup_vlm,
    )
    print(json.dumps({"output": str(args.output_dir.resolve()), "status": report["raw"]["status"], "acceptance_checks": report["acceptance_checks"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
