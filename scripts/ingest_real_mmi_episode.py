"""Add one user-authorized real media episode to the local MMI collection."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media


TARGET_KINDS = {"text", "icon", "chart", "table", "window", "other", "none", "unknown"}


def _load_tasks(root: Path) -> dict[str, dict[str, Any]]:
    rows = json.loads((root / "tasks.json").read_text(encoding="utf-8"))
    return {str(row["task_id"]): row for row in rows}


def _copy_media(source: Path, destination: Path) -> str:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"media file does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.relative_to(destination.parents[2]).as_posix()


def _parse_target_bbox(value: str) -> dict[str, float] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numbers = [float(part.strip()) for part in text.split(",")]
    except ValueError as exc:
        raise ValueError("target-bbox must be x,y,width,height in normalized coordinates") from exc
    if len(numbers) != 4 or not all(math.isfinite(number) for number in numbers):
        raise ValueError("target-bbox must be x,y,width,height in normalized coordinates")
    x, y, width, height = numbers
    if (
        x < 0.0 or y < 0.0 or width <= 0.0 or height <= 0.0
        or x > 1.0 or y > 1.0 or width > 1.0 or height > 1.0
        or x + width > 1.0 or y + height > 1.0
    ):
        raise ValueError("target-bbox must stay inside the normalized image")
    return {key: round(number, 6) for key, number in zip(("x", "y", "width", "height"), numbers)}


def _image_size(path: Path) -> dict[str, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        return {"width": int(width), "height": int(height)}
    except Exception:
        return None


def build_episode(args: argparse.Namespace, task: dict[str, Any], root: Path) -> dict[str, Any]:
    episode_id = f"REAL-{args.task_id}"
    audio_name = f"{episode_id}.wav"
    screen_name = f"{episode_id}.png"
    target_bbox = _parse_target_bbox(args.target_bbox)
    audio_ref = _copy_media(Path(args.audio), root / "media" / "audio" / audio_name)
    screen_ref = _copy_media(Path(args.screen), root / "media" / "screens" / screen_name)
    target_id = args.target_id or None
    selection_label = str(getattr(args, "selection_text", "") or "").strip()
    selected_text = str(getattr(args, "selected_text", "") or "").strip()
    target_kind = args.target_kind or ("unknown" if target_id or selection_label or selected_text else "none")
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unsupported target kind: {target_kind}")
    screen_size = _image_size(root / screen_ref)
    selection_payload = {
        "target_id": target_id,
        "project_id": args.project_id,
        "text": selection_label,
    }
    if selected_text:
        selection_payload["selected_text"] = selected_text[:4000]
        selection_payload["selected_text_source"] = str(
            getattr(args, "selection_source", "manual_text") or "manual_text"
        )[:32]
    if target_bbox is not None:
        selection_payload["bbox"] = target_bbox
    if target_kind != "none":
        selection_payload["target_kind"] = target_kind
    screen_payload = {
        "window_title": args.window_title or "",
        "project_id": args.project_id,
        "application": args.application or "",
    }
    if screen_size is not None:
        screen_payload["screen_size"] = screen_size
    events = [
        {"event_id": "speech", "modality": "speech_text", "start_ms": 0, "end_ms": max(1, args.duration_ms), "confidence": 1.0, "privacy_level": "project", "payload_ref": None, "payload": {"text": args.transcript}},
        {"event_id": "audio", "modality": "audio", "start_ms": 0, "end_ms": max(1, args.duration_ms), "confidence": 1.0, "privacy_level": "private", "payload_ref": audio_ref, "payload": {"format": "wav", "user_authorized": True}},
        {"event_id": "screen", "modality": "screen", "start_ms": 0, "end_ms": max(1, args.duration_ms), "confidence": 1.0, "privacy_level": "private", "payload_ref": screen_ref, "payload": screen_payload},
    ]
    if target_id or selection_label or selected_text:
        events.append({"event_id": "selection", "modality": "ui_selection", "start_ms": 100, "end_ms": max(101, args.duration_ms), "confidence": args.selection_confidence, "privacy_level": "project", "payload_ref": None, "payload": selection_payload})
    aligned = ["speech", "audio", "screen"] + (["selection"] if len(events) == 4 else [])
    return {
        "schema_version": "souldrive_episode_v0.1",
        "episode_id": episode_id,
        "template_id": f"real_{task['intent']}",
        "split": args.split,
        "project_id": args.project_id,
        "profile_id": args.profile_id,
        "speaker_id": args.speaker_id,
        "capture": {
            "application": args.application or None,
            "speaker_id": args.speaker_id,
            "target_kind": target_kind,
            "screen_size": screen_size,
        },
        "events": events,
        "ground_truth": {
            "intent": task["intent"],
            "slots": {"task_id": args.task_id},
            "target_id": target_id,
            "target_bbox": target_bbox,
            "aligned_event_ids": aligned,
            "conflict_type": args.conflict_type,
            "expected_action": args.expected_action or task["expected_action"],
            "expected_response": None,
        },
        "provenance": {
            "source": "user_authorized_real_pc_capture",
            "license": "user_owned_or_authorized_local_material",
            "generator": "scripts/ingest_real_mmi_episode.py",
            "random_seed": None,
            "review_status": "unreviewed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--screen", type=Path, required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--target-id")
    parser.add_argument(
        "--selection-text",
        default="",
        help="兼容旧参数：选区/控件标签，不作为正文证据；正文请使用 --selected-text。",
    )
    parser.add_argument(
        "--selected-text",
        default="",
        help="用户明确确认的选区原文；只有该字段可进入改写或事实检索上下文。",
    )
    parser.add_argument(
        "--selection-source",
        choices=["manual_text", "windows_uia", "ocr"],
        default="manual_text",
        help="selected-text 的采集来源，默认仅表示人工录入，不代表 UIA 已读取。",
    )
    parser.add_argument("--selection-confidence", type=float, default=1.0)
    parser.add_argument("--window-title", default="")
    parser.add_argument("--application", default="")
    parser.add_argument("--speaker-id", default="speaker_unassigned")
    parser.add_argument("--target-kind", choices=sorted(TARGET_KINDS), default="")
    parser.add_argument("--target-bbox", default="")
    parser.add_argument("--duration-ms", type=int, default=3000)
    parser.add_argument("--conflict-type", default="none")
    parser.add_argument("--expected-action")
    parser.add_argument("--profile-id", default="profile_real_pilot")
    parser.add_argument("--split", choices=["seed", "train", "validation", "test"], default="seed")
    args = parser.parse_args()
    root = args.collection_root.resolve()
    tasks = _load_tasks(root)
    if args.task_id not in tasks:
        raise SystemExit(f"unknown task id: {args.task_id}")
    episodes_path = root / "episodes.jsonl"
    existing = [json.loads(line) for line in episodes_path.read_text(encoding="utf-8").splitlines() if line.strip()] if episodes_path.exists() else []
    if any(row.get("episode_id") == f"REAL-{args.task_id}" for row in existing):
        raise SystemExit(f"episode already exists: REAL-{args.task_id}")
    episode = build_episode(args, tasks[args.task_id], root)
    errors = validate_episode_media(episode, root, require_media=True)
    if errors:
        raise SystemExit("media validation failed: " + "; ".join(errors))
    with episodes_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(episode, ensure_ascii=False) + "\n")
    manifest_path = root / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["episode_count"] = len(existing) + 1
    manifest["status"] = "pilot_in_progress"
    manifest["last_episode_id"] = episode["episode_id"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"episode_id": episode["episode_id"], "status": "appended", "media_valid": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
