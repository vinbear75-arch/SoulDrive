"""Audit a SoulDrive real-MMI collection before runtime evaluation.

The audit is read-only with respect to the collection.  It validates the
episode schema, media references, cross-row identity, and the minimum
diversity/difficulty coverage required by the competition roadmap.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media


SCHEMA_PATH = ROOT / "eval" / "schemas" / "episode_v0.1.schema.json"
UNASSIGNED_VALUES = {"", "unknown", "unassigned", "speaker_unassigned"}
TARGET_KINDS = {"text", "icon", "chart", "table", "window", "other", "none", "unknown"}


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.is_file():
        return rows, [f"missing episodes file: {path.name}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return rows, [f"{path.name}: unable to read UTF-8 JSONL ({exc})"]
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"episodes.jsonl:{line_number}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(value, dict):
            errors.append(f"episodes.jsonl:{line_number}: episode must be an object")
            continue
        rows.append(value)
    return rows, errors


def _path_text(path: Any) -> str:
    parts = [str(item) for item in path]
    return ".".join(parts) if parts else "<root>"


def _schema_errors(validator: Draft202012Validator, row: dict[str, Any], row_number: int) -> list[str]:
    messages = []
    for error in sorted(validator.iter_errors(row), key=lambda item: list(item.path)):
        messages.append(
            f"episode[{row_number}] {_path_text(error.path)}: {error.message}"
        )
    return messages


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _capture_value(row: dict[str, Any], key: str) -> Any:
    capture = row.get("capture")
    if not isinstance(capture, dict):
        return None
    return capture.get(key)


def _has_real_value(value: Any) -> bool:
    return str(value or "").strip().lower() not in UNASSIGNED_VALUES


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _selected_text(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return str(payload.get("selected_text") or event.get("selected_text") or "").strip()


def _difficulty_coverage(rows: list[dict[str, Any]]) -> dict[str, bool]:
    coverage = {
        "text_target": False,
        "icon_target": False,
        "chart_target": False,
        "time_misalignment": False,
        "project_conflict": False,
        "low_confidence": False,
        "out_of_scope": False,
    }
    for row in rows:
        event_rows = row.get("events") if isinstance(row.get("events"), list) else []
        kind = str(_capture_value(row, "target_kind") or "").strip().lower()
        if kind in {"text", "icon", "chart"}:
            coverage[f"{kind}_target"] = True
        truth = row.get("ground_truth") if isinstance(row.get("ground_truth"), dict) else {}
        slots = truth.get("slots") if isinstance(truth.get("slots"), dict) else {}
        conflict_type = str(truth.get("conflict_type") or "").strip().lower()
        if conflict_type == "time_misalignment" or slots.get("time_misalignment") is True:
            coverage["time_misalignment"] = True
        if conflict_type == "project_mismatch" or slots.get("conflict") == "project_mismatch":
            coverage["project_conflict"] = True
        if slots.get("low_confidence") is True or any(
            _safe_float(event.get("confidence", 1.0), default=0.0) < 0.7
            for event in event_rows
            if isinstance(event, dict)
        ):
            coverage["low_confidence"] = True
        if str(truth.get("intent") or "").strip().lower() == "out_of_scope" or truth.get("expected_action") == "refuse":
            coverage["out_of_scope"] = True
    return coverage


def audit_collection(
    collection_root: Path,
    *,
    minimum_episodes: int = 30,
    require_media: bool = False,
    require_real_capture: bool = False,
    require_reviewed: bool = False,
    minimum_speakers: int = 2,
    minimum_resolutions: int = 2,
    minimum_projects: int = 2,
) -> dict[str, Any]:
    root = collection_root.resolve()
    minimum_episodes = max(0, int(minimum_episodes))
    errors: list[str] = []
    warnings: list[str] = []
    rows, parse_errors = _load_jsonl(root / "episodes.jsonl")
    errors.extend(parse_errors)

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "souldrive_real_mmi_collection_audit_v0.1",
            "collection_root": str(root),
            "dataset_id": None,
            "episode_count": len(rows),
            "manifest_episode_count": None,
            "require_media": bool(require_media or require_real_capture),
            "require_real_capture": bool(require_real_capture),
            "require_reviewed": bool(require_reviewed),
            "requirements": {
                "minimum_episode_count": len(rows) >= minimum_episodes,
                "unique_episode_ids": False,
                "unique_task_coverage": False,
                "schema_valid": False,
                "media_valid": False,
            },
            "diversity": {},
            "split_counts": {},
            "difficulty_coverage": {},
            "errors": [f"episode schema unavailable: {exc}"],
            "warnings": [],
            "error_count": 1,
            "warning_count": 0,
            "valid": False,
            "limitations": [
                "集合审计只能证明数据契约和覆盖条件，不能证明模型准确率或端到端延迟。",
                "真实样本必须由用户主动授权采集；审计器不会生成、补齐或修改媒体与标签。",
            ],
        }
    
    validator = Draft202012Validator(schema)
    episode_ids: Counter[str] = Counter()
    task_ids: Counter[str] = Counter()
    speaker_ids: Counter[str] = Counter()
    project_ids: Counter[str] = Counter()
    resolutions: Counter[str] = Counter()
    applications: Counter[str] = Counter()
    target_kinds: Counter[str] = Counter()
    selection_event_count = 0
    selection_text_count = 0
    selection_label_only_count = 0
    actual_split_counts: Counter[str] = Counter()
    schema_error_count = 0
    media_error_count = 0

    tasks: dict[str, dict[str, Any]] = {}
    tasks_path = root / "tasks.json"
    if tasks_path.is_file():
        try:
            raw_tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
            if isinstance(raw_tasks, list):
                tasks = {
                    str(item.get("task_id")): item
                    for item in raw_tasks
                    if isinstance(item, dict) and str(item.get("task_id") or "").strip()
                }
            else:
                errors.append("tasks.json: task cards must be an array")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"tasks.json: invalid JSON or unreadable UTF-8 ({exc})")

    for row_number, row in enumerate(rows, 1):
        row_schema_errors = _schema_errors(validator, row, row_number)
        schema_error_count += len(row_schema_errors)
        errors.extend(row_schema_errors)
        episode_id = str(row.get("episode_id") or "").strip()
        episode_ids[episode_id] += 1
        actual_split_counts[str(row.get("split") or "")] += 1
        event_rows = [
            event for event in (row.get("events") if isinstance(row.get("events"), list) else [])
            if isinstance(event, dict)
        ]
        for event in event_rows:
            if str(event.get("modality") or "").strip().lower() not in {"ui_selection", "ui_automation"}:
                continue
            selection_event_count += 1
            if _selected_text(event):
                selection_text_count += 1
            elif str(_event_payload(event).get("text") or event.get("text") or "").strip():
                selection_label_only_count += 1
        event_ids = [str(event.get("event_id") or "").strip() for event in event_rows]
        duplicate_event_ids = sorted({item for item, count in Counter(event_ids).items() if item and count > 1})
        if duplicate_event_ids:
            errors.append(f"episode[{row_number}] duplicate event_id: {duplicate_event_ids}")
        for event in event_rows:
            try:
                start_ms = int(event.get("start_ms", 0))
                end_ms = int(event.get("end_ms", 0))
            except (TypeError, ValueError):
                continue
            if end_ms < start_ms:
                errors.append(f"episode[{row_number}] event {event.get('event_id')}: end_ms before start_ms")
        truth = _mapping(row.get("ground_truth"))
        slots = _mapping(truth.get("slots"))
        aligned = truth.get("aligned_event_ids") if isinstance(truth.get("aligned_event_ids"), list) else []
        missing_aligned = sorted({str(item) for item in aligned} - set(event_ids))
        if missing_aligned:
            errors.append(f"episode[{row_number}] aligned_event_ids reference missing events: {missing_aligned}")

        task_id = str(slots.get("task_id") or "").strip()
        if task_id:
            task_ids[task_id] += 1
            if tasks and task_id not in tasks:
                errors.append(f"episode[{row_number}] unknown task_id: {task_id}")
        project_id = str(row.get("project_id") or "").strip()
        if project_id:
            project_ids[project_id] += 1
        speaker_id = str(row.get("speaker_id") or _capture_value(row, "speaker_id") or "").strip()
        if speaker_id:
            speaker_ids[speaker_id] += 1
        screen_size = _capture_value(row, "screen_size")
        screen_width = _safe_int(_mapping(screen_size).get("width"))
        screen_height = _safe_int(_mapping(screen_size).get("height"))
        if screen_width > 0 and screen_height > 0:
            resolutions[f"{screen_width}x{screen_height}"] += 1
        application = str(_capture_value(row, "application") or "").strip()
        if application:
            applications[application] += 1
        target_kind = str(_capture_value(row, "target_kind") or "").strip().lower()
        if target_kind:
            target_kinds[target_kind] += 1

        if require_media or require_real_capture:
            try:
                row_media_errors = validate_episode_media(row, root, require_media=True)
            except Exception as exc:
                row_media_errors = [f"episode[{row_number}] media validator failed closed: {exc}"]
            media_error_count += len(row_media_errors)
            errors.extend(row_media_errors)
        if require_reviewed and _mapping(row.get("provenance")).get("review_status") not in {"reviewed", "sealed"}:
            errors.append(f"episode[{row_number}] provenance.review_status is not reviewed")

        if require_real_capture:
            if not _has_real_value(project_id):
                errors.append(f"episode[{row_number}] missing project_id")
            if not _has_real_value(speaker_id):
                errors.append(f"episode[{row_number}] missing anonymized speaker_id")
            if not isinstance(screen_size, dict) or screen_width < 1 or screen_height < 1:
                errors.append(f"episode[{row_number}] missing positive capture.screen_size")
            if not _has_real_value(application):
                errors.append(f"episode[{row_number}] missing capture.application")
            if target_kind not in TARGET_KINDS or target_kind == "unknown":
                errors.append(f"episode[{row_number}] missing explicit capture.target_kind")
            if str(truth.get("target_id") or "").strip() and not isinstance(truth.get("target_bbox"), dict):
                errors.append(f"episode[{row_number}] targeted episode missing ground_truth.target_bbox")
            modalities = Counter(str(event.get("modality") or "") for event in event_rows)
            for required_modality in ("speech_text", "audio", "screen"):
                if modalities[required_modality] != 1:
                    errors.append(f"episode[{row_number}] requires exactly one {required_modality} event")
            source = str(_mapping(row.get("provenance")).get("source") or "")
            if not source.startswith("user_authorized_real"):
                errors.append(f"episode[{row_number}] provenance.source is not a real authorized capture")

    duplicate_episode_ids = sorted({item for item, count in episode_ids.items() if not item or count > 1})
    if duplicate_episode_ids:
        errors.append(f"duplicate or empty episode_id: {duplicate_episode_ids}")
    duplicate_task_ids = sorted({item for item, count in task_ids.items() if count > 1})
    if duplicate_task_ids:
        errors.append(f"duplicate task coverage: {duplicate_task_ids}")
    if len(rows) < minimum_episodes:
        errors.append(f"episode count {len(rows)} is below minimum {minimum_episodes}")

    manifest = None
    for filename in ("collection_manifest.json", "manifest.json"):
        manifest_path = root / filename
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    errors.append(f"{filename}: manifest must be an object")
                    manifest = None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"{filename}: invalid JSON or unreadable UTF-8 ({exc})")
            break
    if isinstance(manifest, dict) and manifest.get("episode_count") is not None and manifest.get("episode_count") != len(rows):
        errors.append(
            f"manifest episode_count {manifest.get('episode_count')} does not match episodes.jsonl {len(rows)}"
        )

    if require_real_capture:
        if len(speaker_ids) < max(1, int(minimum_speakers)):
            errors.append(f"speaker diversity {len(speaker_ids)} is below minimum {minimum_speakers}")
        if len(resolutions) < max(1, int(minimum_resolutions)):
            errors.append(f"screen resolution diversity {len(resolutions)} is below minimum {minimum_resolutions}")
        if len(project_ids) < max(1, int(minimum_projects)):
            errors.append(f"project diversity {len(project_ids)} is below minimum {minimum_projects}")
        if tasks and len(rows) >= len(tasks):
            missing_tasks = sorted(set(tasks) - set(task_ids))
            if missing_tasks:
                errors.append(f"task cards not covered: {missing_tasks}")
        for name, present in _difficulty_coverage(rows).items():
            if not present:
                errors.append(f"required difficult-sample coverage missing: {name}")

    requirements = {
        "minimum_episode_count": len(rows) >= minimum_episodes,
        "unique_episode_ids": not any(count > 1 or not item for item, count in episode_ids.items()),
        "unique_task_coverage": not duplicate_task_ids,
        "schema_valid": schema_error_count == 0,
        "media_valid": media_error_count == 0,
    }
    if require_real_capture:
        requirements.update({
            "speaker_diversity": len(speaker_ids) >= max(1, int(minimum_speakers)),
            "resolution_diversity": len(resolutions) >= max(1, int(minimum_resolutions)),
            "project_diversity": len(project_ids) >= max(1, int(minimum_projects)),
            "difficulty_coverage": all(_difficulty_coverage(rows).values()),
        })
    return {
        "schema_version": "souldrive_real_mmi_collection_audit_v0.1",
        "collection_root": str(root),
        "dataset_id": (manifest or {}).get("dataset_id") if isinstance(manifest, dict) else None,
        "episode_count": len(rows),
        "manifest_episode_count": (manifest or {}).get("episode_count") if isinstance(manifest, dict) else None,
        "require_media": bool(require_media or require_real_capture),
        "require_real_capture": bool(require_real_capture),
        "require_reviewed": bool(require_reviewed),
        "requirements": requirements,
        "diversity": {
            "speakers": dict(sorted(speaker_ids.items())),
            "resolutions": dict(sorted(resolutions.items())),
            "projects": dict(sorted(project_ids.items())),
            "applications": dict(sorted(applications.items())),
            "target_kinds": dict(sorted(target_kinds.items())),
        },
        "split_counts": dict(sorted(actual_split_counts.items())),
        "difficulty_coverage": _difficulty_coverage(rows),
        "selection_text_coverage": {
            "selection_event_count": selection_event_count,
            "with_selected_text": selection_text_count,
            "label_only": selection_label_only_count,
        },
        "errors": errors[:200],
        "warnings": warnings[:200],
        "error_count": len(errors),
        "warning_count": len(warnings),
        "valid": not errors,
        "limitations": [
            "集合审计只能证明数据契约和覆盖条件，不能证明模型准确率或端到端延迟。",
            "真实样本必须由用户主动授权采集；审计器不会生成、补齐或修改媒体与标签。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-episodes", type=int, default=30)
    parser.add_argument("--require-media", action="store_true")
    parser.add_argument("--require-real-capture", action="store_true")
    parser.add_argument("--require-reviewed", action="store_true")
    parser.add_argument("--minimum-speakers", type=int, default=2)
    parser.add_argument("--minimum-resolutions", type=int, default=2)
    parser.add_argument("--minimum-projects", type=int, default=2)
    args = parser.parse_args()
    report = audit_collection(
        args.collection_root,
        minimum_episodes=args.minimum_episodes,
        require_media=args.require_media,
        require_real_capture=args.require_real_capture,
        require_reviewed=args.require_reviewed,
        minimum_speakers=args.minimum_speakers,
        minimum_resolutions=args.minimum_resolutions,
        minimum_projects=args.minimum_projects,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
