"""Local, explainable multimodal decision gate for SoulDrive PC.

The rule layer is deliberately kept as the final safety boundary. A small
trained intent head may improve coverage for paraphrases, but it can never
turn an unrecognised request into an external side effect.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any


MODEL_SCHEMA_VERSION = "souldrive_soulalign_lite_v0.1"
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent / "configs" / "soulalign_lite_v0.1.json"
_TEXT_FEATURE_LIMIT = 600
TARGET_EVENT_MODALITIES = {"ui_automation", "ui_selection", "ocr_region", "image_region"}
TARGET_MODALITY_RELIABILITY = {
    "ui_automation": 1.0,
    "ui_selection": 0.96,
    "ocr_region": 0.78,
    "image_region": 0.7,
    # VLM proposals are useful candidates, but never outrank an explicit UI
    # selection or accessibility node on their own.
    "vlm": 0.55,
}
TARGET_ALIGNMENT_WINDOW_MS = 2500
TARGET_MIN_CONFIDENCE = 0.7
TARGET_MIN_SCORE = 0.62
TARGET_MIN_MARGIN = 0.08
DOCUMENT_SEARCH_ACTIONS = ("搜索", "查找", "检索", "寻找", "查出")
DOCUMENT_SEARCH_CONTEXT = ("项目", "文档", "资料", "记录", "证据", "来源", "文件")
ANSWER_REQUEST_MARKERS = (
    "回答", "说明", "解释", "多少", "是否", "给出答案", "有依据的回答",
)
CLARIFICATION_SIGNALS = (
    "澄清", "确认对象", "目标不明确", "缺少明确目标", "当前指代不清",
    "发给王老师", "上个月", "上周", "去年", "本月", "指向内容", "歧义",
)
CLARIFICATION_PATTERNS = (
    re.compile(r"(?:这个|那个|刚才|上述|前面|该)\s*(?:文件|文档|内容|结果|对象|项目|实验|版本)?\s*(?:是什么|哪个|哪一|指什么|确认|发送|比较|对比|总结|改写|处理)"),
    re.compile(r"(?:确认|发送|比较|对比|总结|改写|处理)\s*(?:这个|那个|刚才|上述|前面|该)\s*(?:文件|文档|内容|结果|对象|项目|实验|版本)?"),
    re.compile(r"(?:确认|选择|发送|比较|对比)\s*(?:哪个|哪一个|哪一份|哪项|哪条|什么对象|这一个|那个)"),
    re.compile(r"(?:上个月|上周|去年|本月|最近)\s*(?:的|那次|哪个|哪一|相关|实验|版本|结果)?"),
)


def normalize_text(value: str) -> str:
    """Remove fixture-only identifiers before feature extraction."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = re.sub(r"\d+", "<num>", text)
    text = re.sub(r"\b(?:train|validation|test)\b", "<split>", text)
    return text[:_TEXT_FEATURE_LIMIT]


def build_features(speech_text: str, events: list[dict[str, Any]]) -> dict[str, float]:
    """Build sparse text and time/context features shared by train and runtime."""
    text = normalize_text(speech_text)
    features: dict[str, float] = {}
    for width in range(1, 5):
        for index in range(max(0, len(text) - width + 1)):
            token = text[index:index + width]
            if token.strip():
                features[f"char{width}:{token}"] = 1.0

    speech_event = next((event for event in events if event.get("modality") == "speech_text"), None)
    speech_time = int((speech_event or {}).get("start_ms") or 0)
    targets = []
    projects: set[str] = set()
    aligned_targets = 0
    out_of_window_targets = 0
    low_confidence_targets = 0
    for event in events:
        modality = str(event.get("modality") or "unknown")[:32]
        features[f"modality:{modality}"] = 1.0
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        target = _event_target_id(event) if modality in TARGET_EVENT_MODALITIES else None
        if not target:
            continue
        targets.append(str(target))
        project = payload.get("project_id")
        if project:
            projects.add(str(project))
        delta = abs(int(event.get("start_ms") or 0) - speech_time)
        if delta <= 2500:
            aligned_targets += 1
        else:
            out_of_window_targets += 1
        if float(event.get("confidence", 1.0)) < 0.7:
            low_confidence_targets += 1

    features[f"target_count:{min(len(targets), 3)}"] = 1.0
    features[f"aligned_target_count:{min(aligned_targets, 3)}"] = 1.0
    features[f"out_of_window_target_count:{min(out_of_window_targets, 3)}"] = 1.0
    features[f"low_confidence_target_count:{min(low_confidence_targets, 3)}"] = 1.0
    features[f"project_count:{min(len(projects), 3)}"] = 1.0
    return features


def _default_model_path() -> Path:
    configured = os.environ.get("SOULDRIVE_SOULALIGN_MODEL", "").strip()
    return Path(configured) if configured else DEFAULT_MODEL_PATH


@lru_cache(maxsize=4)
def _load_model(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != MODEL_SCHEMA_VERSION:
        return None
    classes = payload.get("classes")
    names = payload.get("feature_names")
    weights = payload.get("weights")
    intercepts = payload.get("intercepts")
    if not (
        isinstance(classes, list)
        and isinstance(names, list)
        and isinstance(weights, list)
        and isinstance(intercepts, list)
        and classes
        and len(classes) == len(weights) == len(intercepts)
        and len(names) <= 100_000
        and all(isinstance(item, str) and item for item in classes)
        and all(isinstance(item, str) and item for item in names)
        and all(isinstance(row, list) and len(row) == len(names) for row in weights)
    ):
        return None
    try:
        numeric_weights = [[float(value) for value in row] for row in weights]
        numeric_intercepts = [float(value) for value in intercepts]
        minimum_confidence = float(payload.get("minimum_confidence", 0.65))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= minimum_confidence <= 1.0:
        return None
    payload["weights"] = numeric_weights
    payload["intercepts"] = numeric_intercepts
    payload["minimum_confidence"] = minimum_confidence
    payload["feature_index"] = {name: index for index, name in enumerate(names)}
    return payload


def clear_model_cache() -> None:
    """Test and deployment helper after replacing a local JSON checkpoint."""
    _load_model.cache_clear()


def predict_lite_intent(
    speech_text: str,
    events: list[dict[str, Any]],
    *,
    model_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Run the JSON-only linear head; malformed or absent checkpoints are ignored."""
    path = Path(model_path) if model_path is not None else _default_model_path()
    model = _load_model(str(path.resolve()))
    if model is None:
        return None
    values = build_features(speech_text, events)
    indices = model["feature_index"]
    active = [(indices[name], value) for name, value in values.items() if name in indices]
    scores = [
        intercept + sum(row[index] * value for index, value in active)
        for row, intercept in zip(model["weights"], model["intercepts"])
    ]
    if not scores or not all(math.isfinite(value) for value in scores):
        return None
    ceiling = max(scores)
    exponentials = [math.exp(min(0.0, value - ceiling)) for value in scores]
    total = sum(exponentials)
    if total <= 0.0 or not math.isfinite(total):
        return None
    winner = max(range(len(scores)), key=scores.__getitem__)
    return {
        "intent": model["classes"][winner],
        "confidence": exponentials[winner] / total,
        "minimum_confidence": model["minimum_confidence"],
        "checkpoint": str(model.get("checkpoint_id") or path.name),
    }


def decide(
    speech_text: str,
    events: list[dict[str, Any]],
    *,
    confidence_threshold: float = 0.7,
    active_project_id: str | None = None,
    target_required: bool = False,
    model_path: Path | str | None = None,
) -> dict[str, Any]:
    """Fuse authorised events without executing external side effects."""
    started = time.perf_counter()
    speech_event = next((event for event in events if event.get("modality") == "speech_text"), None)
    speech_id = str((speech_event or {}).get("event_id") or "speech")
    speech_time = int((speech_event or {}).get("start_ms") or 0)
    text = speech_text.strip()
    rule_intent = _classify(text)
    learned = predict_lite_intent(text, events, model_path=model_path)
    if (
        rule_intent == "clarify_target"
        and not _has_explicit_clarification_signal(text)
        and learned
        and learned["confidence"] >= learned["minimum_confidence"]
    ):
        intent = str(learned["intent"])
        intent_source = "learned_lite"
    else:
        intent = rule_intent
        intent_source = "rules"
    target_verification = verify_target_candidates(
        text,
        events,
        confidence_threshold=confidence_threshold,
        active_project_id=active_project_id,
        require_target=target_required,
    )
    aligned = [speech_id]
    target_events: list[dict[str, Any]] = []
    all_target_events: list[dict[str, Any]] = []
    for event in events:
        if event is speech_event:
            continue
        target_id = _event_target_id(event)
        if target_id and event.get("modality") in TARGET_EVENT_MODALITIES:
            all_target_events.append(event)
        if abs(int(event.get("start_ms") or 0) - speech_time) <= TARGET_ALIGNMENT_WINDOW_MS:
            aligned.append(str(event.get("event_id") or f"event-{len(aligned)}"))
            if target_id and event.get("modality") in TARGET_EVENT_MODALITIES:
                target_events.append(event)
    target_ids = set(target_verification.get("target_ids") or [])
    project_ids = set(target_verification.get("project_ids") or [])
    project_ids.update(
        str(event.get("payload", {}).get("project_id"))
        for event in target_events
        if event.get("payload", {}).get("project_id")
    )
    conflict_type = (
        "project_mismatch"
        if len(project_ids) > 1 or "project_mismatch" in (target_verification.get("reason_codes") or [])
        else "none"
    )
    low_confidence = bool(target_verification.get("low_confidence")) or any(
        float(event.get("confidence", 1.0)) < confidence_threshold for event in target_events
    )
    direct_external_send = _is_direct_external_send(text)
    # An attached screenshot is evidence for the following RAG request, not
    # an implicit request to resolve an interactive target.  Target review is
    # reserved for an explicit UI Automation target (or an explicit caller
    # requirement); otherwise OCR/VLM candidates must not block an ordinary
    # knowledge question before it reaches document retrieval.
    target_review_required = target_required or intent in {
        "selection_summary",
        "rewrite_selection",
        "project_compare",
        "search_document",
        "extract_tasks",
        "schedule_draft",
        "email_draft",
    } or any(
        str(event.get("modality") or "").lower() == "ui_automation"
        for event in events
    )
    if (
        intent == "clarify_target"
        and not target_required
        and not _has_explicit_clarification_signal(text)
        and not all_target_events
        and not direct_external_send
    ):
        # The safe default for an unclassified text question is a normal
        # knowledge query.  RAG still owns the evidence/refusal decision.
        intent = "private_qa"
        intent_source = "rules_default_knowledge_query"
    clarification_required = (
        intent == "clarify_target"
        or conflict_type != "none"
        or (target_review_required and low_confidence)
        or (target_review_required and len(all_target_events) != len(target_events))
        or (target_review_required and bool(target_verification.get("required")))
        or direct_external_send
    )
    if intent == "out_of_scope":
        action = "refuse"
    elif intent in {"confirm", "cancel"}:
        action = intent
    else:
        action = "clarify" if clarification_required else "respond"
    clarification = _build_clarification_contract(
        clarification_required=clarification_required,
        intent=intent,
        conflict_type=conflict_type,
        low_confidence=low_confidence,
        has_unaligned_target=len(all_target_events) != len(target_events),
        direct_external_send=direct_external_send,
        target_ids=sorted(target_ids),
        target_reason_codes=target_verification.get("reason_codes") or [],
    )
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "schema_version": "souldrive_soulalign_decision_v0.3",
        "intent": intent,
        "intent_source": intent_source,
        "intent_model": learned,
        "target_id": target_verification.get("selected_target_id"),
        "target_verification": target_verification,
        "aligned_event_ids": aligned,
        "conflict_type": conflict_type,
        "clarification_required": clarification_required,
        "clarification": clarification,
        "action": action,
        "confidence": 0.55 if clarification_required else 0.82,
        "latency_ms": {"total": elapsed},
        "side_effect": "none",
    }


def verify_target_candidates(
    speech_text: str,
    events: list[dict[str, Any]],
    *,
    confidence_threshold: float = TARGET_MIN_CONFIDENCE,
    active_project_id: str | None = None,
    require_target: bool = False,
) -> dict[str, Any]:
    """Select a grounded target only when independent signals agree.

    The verifier is intentionally deterministic and model-agnostic. It accepts
    candidate regions from UI Automation/OCR/VLM adapters, then combines source
    reliability, temporal alignment, explicit selection, focus and project
    consistency. A close score margin is never auto-selected.
    """
    speech_event = next((event for event in events if event.get("modality") == "speech_text"), None)
    speech_time = _event_time(speech_event)
    candidates: dict[str, dict[str, Any]] = {}
    all_target_events = 0
    aligned_target_events = 0
    raw_project_ids: set[str] = set()
    project_mismatch_detected = False
    for event_index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("modality") not in TARGET_EVENT_MODALITIES:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        raw_candidates = payload.get("target_candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = event.get("target_candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            fallback = dict(payload)
            for key in (
                "target_id", "target_kind", "project_id", "window_id", "window_title",
                "is_focused", "selected", "explicit", "bbox", "text", "confidence",
            ):
                if key in event and key not in fallback:
                    fallback[key] = event[key]
            raw_candidates = [fallback] if fallback.get("target_id") or event.get("target_id") else []
        if not raw_candidates:
            continue
        all_target_events += 1
        event_start = _event_time(event)
        event_end = _event_end(event, event_start)
        aligned = _events_temporally_aligned(speech_time, event_start, event_end)
        if aligned:
            aligned_target_events += 1
        for candidate_index, raw_candidate in enumerate(raw_candidates[:12]):
            if not isinstance(raw_candidate, dict):
                continue
            target_id = str(
                raw_candidate.get("target_id")
                or raw_candidate.get("id")
                or raw_candidate.get("name")
                or ""
            ).strip()[:128]
            if not target_id:
                continue
            project_id = str(raw_candidate.get("project_id") or payload.get("project_id") or "").strip()[:128]
            if project_id:
                raw_project_ids.add(project_id)
            confidence = _bounded_confidence(
                raw_candidate.get("confidence", event.get("confidence", 1.0))
            )
            source = str(raw_candidate.get("source") or event.get("modality") or "unknown").strip().lower()
            reliability = TARGET_MODALITY_RELIABILITY.get(source, TARGET_MODALITY_RELIABILITY.get(event.get("modality"), 0.5))
            focused = bool(raw_candidate.get("is_focused") or raw_candidate.get("focused"))
            explicitly_selected = bool(
                raw_candidate.get("selected")
                or raw_candidate.get("explicit")
                or event.get("modality") == "ui_selection"
            )
            spatial_score, spatial_reference = _spatial_signal(event, payload, raw_candidate)
            project_match = bool(active_project_id and project_id == active_project_id)
            candidate_project_mismatch = bool(active_project_id and project_id and project_id != active_project_id)
            if candidate_project_mismatch:
                project_mismatch_detected = True
            alignment_score = 1.0 if aligned else 0.0
            score = (
                0.45 * confidence
                + 0.22 * alignment_score
                + 0.18 * reliability
                + (0.08 if focused else 0.0)
                + (0.05 if explicitly_selected else 0.0)
                + (0.04 if project_match else 0.0)
                + (0.10 * spatial_score if spatial_score is not None else 0.0)
                - (0.18 if candidate_project_mismatch else 0.0)
            )
            candidate = {
                "target_id": target_id,
                "score": round(max(0.0, min(score, 1.0)), 6),
                "confidence": confidence,
                "source": source,
                "source_event_id": str(event.get("event_id") or f"event-{event_index + 1}")[:64],
                "aligned": aligned,
                "is_focused": focused,
                "explicitly_selected": explicitly_selected,
                "project_id": project_id or None,
                "spatial_score": spatial_score,
                "spatial_reference": spatial_reference,
                "support_count": 1,
                "event_ids": [str(event.get("event_id") or f"event-{event_index + 1}")[:64]],
            }
            if isinstance(raw_candidate.get("bbox"), dict):
                candidate["bbox"] = dict(raw_candidate["bbox"])
            existing = candidates.get(target_id)
            if existing is None or candidate["score"] > existing["score"]:
                candidates[target_id] = candidate
            elif existing is not None:
                existing["support_count"] += 1
                existing["event_ids"] = list(dict.fromkeys([*existing["event_ids"], candidate["source_event_id"]]))[:8]
                existing["score"] = round(min(1.0, max(existing["score"], candidate["score"]) + 0.04), 6)

    ranked = sorted(candidates.values(), key=lambda item: (-item["score"], item["target_id"]))
    eligible = [item for item in ranked if item["aligned"] and item["confidence"] >= confidence_threshold]
    reason_codes: list[str] = []
    spatially_eligible = [
        item
        for item in eligible
        if item.get("spatial_score") is None or float(item["spatial_score"]) > 0.0
    ]
    if eligible and not spatially_eligible:
        reason_codes.append("target_outside_spatial_region")
    eligible = spatially_eligible
    top = eligible[0] if eligible else None
    second = eligible[1] if len(eligible) > 1 else None
    margin = round(top["score"] - second["score"], 6) if top and second else None
    if not ranked and (all_target_events or require_target):
        reason_codes.append("target_candidate_missing")
    if all_target_events and aligned_target_events < all_target_events:
        reason_codes.append("target_outside_time_window")
    if ranked and not eligible and "target_outside_spatial_region" not in reason_codes:
        reason_codes.append("low_confidence_target")
    if top and top["score"] < TARGET_MIN_SCORE:
        reason_codes.append("verifier_low_score")
    if top and second and margin < TARGET_MIN_MARGIN:
        reason_codes.append("ambiguous_target_candidates")
    if len(raw_project_ids) > 1:
        reason_codes.append("project_conflict")
    if project_mismatch_detected:
        reason_codes.append("project_mismatch")
    selected = top if top and not reason_codes and top["score"] >= TARGET_MIN_SCORE else None
    if selected is None and ranked and not reason_codes:
        reason_codes.append("verifier_low_score")
    return {
        "schema_version": "souldrive_target_verification_v0.2",
        "selected_target_id": selected["target_id"] if selected else None,
        "confidence": round(float(selected["score"] if selected else (top["score"] if top else 0.0)), 6),
        "required": bool(reason_codes),
        "reason_codes": reason_codes,
        "target_ids": [item["target_id"] for item in (eligible[:5] if eligible else ranked[:5])],
        "project_ids": sorted(raw_project_ids),
        "all_target_event_count": all_target_events,
        "aligned_target_event_count": aligned_target_events,
        "margin": margin,
        "candidates": ranked[:5],
        "verifier": "deterministic_signal_fusion_spatial_v0.2",
    }


def _event_target_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(event.get("target_id") or payload.get("target_id") or "").strip()


def _event_time(event: dict[str, Any] | None) -> int:
    if not isinstance(event, dict):
        return 0
    try:
        return max(0, int(event.get("start_ms") or event.get("timestamp_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _event_end(event: dict[str, Any], start_ms: int) -> int:
    try:
        return max(start_ms, int(event.get("end_ms") or start_ms))
    except (TypeError, ValueError):
        return start_ms


def _events_temporally_aligned(speech_time: int, start_ms: int, end_ms: int) -> bool:
    return start_ms <= speech_time <= end_ms or abs(start_ms - speech_time) <= TARGET_ALIGNMENT_WINDOW_MS


def _bounded_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(number, 1.0)), 6)


def _spatial_signal(
    event: dict[str, Any],
    payload: dict[str, Any],
    raw_candidate: dict[str, Any],
) -> tuple[float | None, str | None]:
    """Return a bounded spatial match for an authorized selection/focus signal."""
    reference_bbox = None
    reference_source = None
    for source, container in (("payload", payload), ("event", event)):
        if not isinstance(container, dict):
            continue
        for key in ("selected_region", "selection_bbox"):
            reference_bbox = _normalise_bbox(container.get(key))
            if reference_bbox is not None:
                reference_source = f"{source}.{key}"
                break
        if reference_bbox is not None:
            break
    if reference_bbox is None and str(event.get("modality") or "") in {
        "ui_selection", "image_region", "ocr_region"
    }:
        reference_bbox = _normalise_bbox(event.get("bbox"))
        if reference_bbox is not None:
            reference_source = "event.bbox"

    reference_point = None
    if reference_bbox is None:
        for source, container in (("payload", payload), ("event", event)):
            if not isinstance(container, dict):
                continue
            for key in ("focus_point", "pointer", "cursor", "point"):
                reference_point = _normalise_point(container.get(key))
                if reference_point is not None:
                    reference_source = f"{source}.{key}"
                    break
            if reference_point is not None:
                break
    if reference_bbox is None and reference_point is None:
        return None, None

    candidate_bbox = _normalise_bbox(raw_candidate.get("bbox"))
    if candidate_bbox is None:
        return 0.0, reference_source
    if reference_point is not None:
        inside = (
            reference_point["x"] >= candidate_bbox["x"]
            and reference_point["x"] <= candidate_bbox["x"] + candidate_bbox["width"]
            and reference_point["y"] >= candidate_bbox["y"]
            and reference_point["y"] <= candidate_bbox["y"] + candidate_bbox["height"]
        )
        return (1.0 if inside else 0.0), reference_source

    left = max(reference_bbox["x"], candidate_bbox["x"])
    top = max(reference_bbox["y"], candidate_bbox["y"])
    right = min(
        reference_bbox["x"] + reference_bbox["width"],
        candidate_bbox["x"] + candidate_bbox["width"],
    )
    bottom = min(
        reference_bbox["y"] + reference_bbox["height"],
        candidate_bbox["y"] + candidate_bbox["height"],
    )
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0.0:
        return 0.0, reference_source
    candidate_area = candidate_bbox["width"] * candidate_bbox["height"]
    reference_area = reference_bbox["width"] * reference_bbox["height"]
    union = candidate_area + reference_area - intersection
    return round(
        max(intersection / candidate_area, intersection / reference_area, intersection / union),
        6,
    ), reference_source


def _normalise_bbox(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        numbers = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(number) and 0.0 <= number <= 1.0 for number in numbers.values()):
        return None
    if (
        numbers["width"] <= 0.0
        or numbers["height"] <= 0.0
        or numbers["x"] + numbers["width"] > 1.0
        or numbers["y"] + numbers["height"] > 1.0
    ):
        return None
    return {key: round(value, 6) for key, value in numbers.items()}


def _normalise_point(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        point = {key: float(value[key]) for key in ("x", "y")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(number) and 0.0 <= number <= 1.0 for number in point.values()):
        return None
    return {key: round(value, 6) for key, value in point.items()}


def _build_clarification_contract(
    *,
    clarification_required: bool,
    intent: str,
    conflict_type: str,
    low_confidence: bool,
    has_unaligned_target: bool,
    direct_external_send: bool,
    target_ids: list[str],
    target_reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if intent == "clarify_target":
        reasons.append("ambiguous_reference")
    if conflict_type != "none":
        reasons.append("project_conflict")
    if low_confidence:
        reasons.append("low_confidence_target")
    if has_unaligned_target:
        reasons.append("target_outside_time_window")
    for reason_code in target_reason_codes or []:
        if reason_code not in reasons:
            reasons.append(reason_code)
    if direct_external_send:
        reasons.append("external_send_confirmation")
    if clarification_required and not reasons:
        reasons.append("target_or_scope_missing")
    prompts = {
        "ambiguous_reference": "请明确要处理的文档、项目、时间范围或确认对象。",
        "project_conflict": "检测到目标属于不同项目，请选择一个项目后继续。",
        "low_confidence_target": "当前屏幕目标识别置信度不足，请重新框选或确认目标。",
        "target_outside_time_window": "屏幕目标与语音时间不一致，请重新选择当前目标。",
        "target_candidate_missing": "当前目标没有可验证的候选区域，请重新框选或选择可读控件。",
        "target_outside_spatial_region": "候选目标不在当前框选或指针区域内，请重新框选目标。",
        "ambiguous_target_candidates": "检测到多个相近的目标候选，请明确要处理哪一个。",
        "verifier_low_score": "目标验证置信度不足，请重新框选或确认当前目标。",
        "project_conflict": "检测到目标属于不同项目，请选择一个项目后继续。",
        "external_send_confirmation": "请确认收件人、邮件内容和是否发送；当前不会直接发送。",
        "target_or_scope_missing": "请补充明确的目标或范围后继续。",
    }
    return {
        "required": clarification_required,
        "reason_codes": reasons,
        "prompt": next((prompts[reason] for reason in reasons if reason in prompts), "") if reasons else "",
        "target_candidates": target_ids[:5],
    }


def _classify(text: str) -> str:
    if _is_direct_external_send(text):
        return "email_draft"
    if _has_explicit_clarification_signal(text):
        return "clarify_target"
    if _is_explicit_document_search(text):
        return "search_document"
    rules = [
        ("out_of_scope", ("删除桌面", "删除所有", "清空电脑", "不可逆", "绕过确认", "直接发送并删除", "系统文件", "全部删", "外部发送", "不要预览", "桌面破坏")),
        ("email_draft", ("邮件", "同步", "联系人", "沟通文本", "汇报信")),
        ("selection_summary", ("总结", "概括", "归纳", "提炼", "要点", "结论", "概览", "摘要", "浓缩")),
        ("rewrite_selection", ("改写", "改成", "润色", "重写", "论文表达", "正式周报", "严谨表述", "改编", "修订", "规范版本", "书面表述", "汇报口吻")),
        ("project_compare", ("比较", "对比", "差异", "区别", "变化", "不同", "前后", "新旧", "两项", "两份")),
        ("private_qa", ("指标", "数值", "回答", "多少", "知识库", "性能数据", "实验结果", "找出答案", "说明数值", "有依据的回答", "说明")),
        ("switch_template", ("模板", "样式", "切换", "版式", "格式")),
        ("search_document", ("搜索", "查找", "检索", "定位", "寻找", "查出", "项目文档")),
        ("extract_tasks", ("待办", "任务", "行动项", "后续", "未完成", "下一步", "工作项", "推进", "后续动作", "待处理事项", "处理事项")),
        ("schedule_draft", ("日程", "安排", "评审", "会议", "时间建议", "日历")),
        ("cancel", ("取消", "不要继续", "撤销", "停止", "结束", "撤回", "不要保留")),
        ("confirm", ("确认", "继续生成", "按当前计划", "允许继续", "接受当前", "同意生成")),
    ]
    for intent, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return intent
    return "clarify_target"


def _has_explicit_clarification_signal(text: str) -> bool:
    return any(signal in text for signal in CLARIFICATION_SIGNALS) or any(
        pattern.search(text) for pattern in CLARIFICATION_PATTERNS
    )


def _is_explicit_document_search(text: str) -> bool:
    """Recognise retrieval actions before broad fact-question keywords.

    A phrase such as ``检索文档后给出有依据的回答`` is still a fact question:
    the requested outcome is an answer, not a document-search operation. Keep
    those phrases on the private-QA route while allowing explicit search tasks
    such as ``搜索项目里的实验记录`` to reach document retrieval.
    """
    normalized = str(text or "").strip()
    if any(marker in normalized for marker in ANSWER_REQUEST_MARKERS):
        return False
    if any(action in normalized for action in DOCUMENT_SEARCH_ACTIONS):
        return any(context in normalized for context in DOCUMENT_SEARCH_CONTEXT)
    return "定位" in normalized and any(
        context in normalized for context in ("证据", "项目文档", "关联")
    )


def _is_direct_external_send(text: str) -> bool:
    normalized = str(text or "")
    return "邮件" in normalized and any(
        marker in normalized
        for marker in ("直接发送", "立刻发送", "马上发送", "发送邮件", "发出去", "直接发")
    )
