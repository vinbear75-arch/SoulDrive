"""Read-only projection over existing project activity facts."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable


EPISODIC_VIEW_SCHEMA_VERSION = "souldrive_episodic_view_v0.1"
EPISODIC_RETRIEVAL_SCHEMA_VERSION = "souldrive_episodic_retrieval_context_v0.1"
EPISODIC_RETRIEVAL_WORKFLOWS = frozenset({"experiment_compare", "meeting_actions"})

_EPISODIC_TIME_MARKERS = (
    "上次",
    "上一个",
    "之前",
    "历史",
    "过去",
    "最近",
    "上个月",
    "上周",
    "去年",
    "本月",
    "本周",
    "当时",
    "回顾",
    "复盘",
    "继续",
    "last",
    "previous",
    "prior",
    "history",
    "recent",
)


def select_episodic_events_for_query(
    view: dict[str, Any] | None,
    *,
    query: str,
    workflow_id: str | None,
    limit: int = 4,
) -> dict[str, Any]:
    """Select bounded activity hints for an explicitly temporal workflow query.

    The projection remains read-only and is deliberately not evidence.  It is
    a deterministic routing hint that helps the bound workflow locate the
    relevant document or confirmed record; ordinary current-turn questions do
    not receive historical activity and an unrelated workflow receives none.
    """
    normalized_workflow = str(workflow_id or "").strip()
    normalized_query = " ".join(str(query or "").split()).lower()
    normalized_view = view if isinstance(view, dict) else {}
    result = {
        "schema_version": EPISODIC_RETRIEVAL_SCHEMA_VERSION,
        "project_id": str(normalized_view.get("project_id") or "").strip() or None,
        "workflow_id": normalized_workflow or None,
        "scope": {
            "project_id": str(normalized_view.get("project_id") or "").strip() or None,
            "as_of": normalized_view.get("as_of"),
            "view_digest": normalized_view.get("view_digest"),
        },
        "selection_reason": "not_selected",
        "events": [],
    }
    if normalized_workflow not in EPISODIC_RETRIEVAL_WORKFLOWS:
        result["selection_reason"] = "workflow_not_enabled"
        return result
    if not any(marker in normalized_query for marker in _EPISODIC_TIME_MARKERS):
        result["selection_reason"] = "temporal_reference_not_explicit"
        return result

    allowed_sources = {
        "experiment_compare": {"experiment_run", "decision", "durable_task"},
        "meeting_actions": {
            "decision",
            "durable_task",
            "conversation_session",
            "project_task",
        },
    }[normalized_workflow]
    source_markers = _episodic_source_markers(normalized_workflow, normalized_query)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for position, event in enumerate(normalized_view.get("events") or []):
        if not isinstance(event, dict):
            continue
        source_type = str(event.get("source_type") or "").strip()
        if source_type not in allowed_sources:
            continue
        event_workflow = str(event.get("workflow_id") or "").strip()
        if event_workflow and event_workflow != normalized_workflow:
            continue
        score = source_markers.get(source_type, 0)
        if source_markers and score <= 0:
            continue
        if not source_markers:
            score = 1
        candidates.append((score, -position, event))

    safe_limit = max(1, min(int(limit), 8))
    candidates.sort(key=lambda item: (-item[0], -item[1]))
    result["events"] = [
        {
            key: event[key]
            for key in (
                "event_id",
                "source_type",
                "source_id",
                "source_ref",
                "project_id",
                "workflow_id",
                "event_time",
            "event_time_semantics",
            "title",
            "summary",
            "status",
            "created_at",
            "updated_at",
            "due_at",
            "completed_at",
        )
            if key in event
        }
        for _, _, event in candidates[:safe_limit]
    ]
    result["selection_reason"] = (
        "temporal_activity_match" if result["events"] else "no_scoped_activity_match"
    )
    return result


def _episodic_source_markers(workflow_id: str, query: str) -> dict[str, int]:
    if workflow_id == "experiment_compare":
        markers = {
            "experiment_run": (4, ("实验", "运行", "指标", "数据集", "版本", "评测", "配置")),
            "decision": (2, ("决策", "决定", "口径", "结论")),
            "durable_task": (2, ("任务", "验证", "进展")),
        }
    else:
        markers = {
            "decision": (4, ("决策", "决定", "结论", "口径")),
            "durable_task": (4, ("行动项", "待办", "任务", "负责人", "截止", "进展")),
            "project_task": (4, ("行动项", "待办", "任务", "负责人", "截止", "进展")),
            "conversation_session": (2, ("会议", "会话", "讨论", "记录")),
        }
    return {
        source_type: weight
        for source_type, (weight, terms) in markers.items()
        if any(term in query for term in terms)
    }


def build_episodic_view(
    *,
    project_id: str,
    sessions: Iterable[dict[str, Any]],
    experiment_runs: Iterable[dict[str, Any]],
    decisions: Iterable[dict[str, Any]],
    durable_tasks: Iterable[dict[str, Any]],
    project_tasks: Iterable[dict[str, Any]] = (),
    limit: int = 40,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Project existing facts into a bounded, provenance-preserving activity view."""
    project_id = str(project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    cutoff = _parse_timestamp(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        raise ValueError("as_of must be an ISO-8601 timestamp or Unix timestamp")
    events: list[dict[str, Any]] = []
    invalid_event_count = 0
    scope_rejected_count = 0

    for session in sessions or []:
        if str(session.get("project_id") or "").strip() != project_id:
            scope_rejected_count += 1
            continue
        event = _event(
            source_type="conversation_session",
            source_id=session.get("session_id"),
            project_id=project_id,
            event_time=session.get("updated_at") or session.get("created_at"),
            event_time_semantics="last_updated",
            title=session.get("title") or "会话窗口",
            summary="会话窗口活动",
            status="available",
            workflow_id=session.get("workflow_id"),
        )
        invalid_event_count += _append_if_visible(events, event, cutoff)

    for task in project_tasks or []:
        if str(task.get("project_id") or "").strip() != project_id:
            scope_rejected_count += 1
            continue
        event = _event(
            source_type="project_task",
            source_id=task.get("task_id"),
            project_id=project_id,
            event_time=task.get("updated_at"),
            event_time_semantics="last_updated",
            title=task.get("title") or "项目任务",
            summary=task.get("summary") or task.get("title") or "已记录",
            status=str(task.get("status") or "unknown").strip() or "unknown",
            workflow_id=task.get("workflow_id"),
        )
        if not event.get("_invalid"):
            for key in ("created_at", "updated_at", "due_at", "completed_at"):
                if task.get(key) is not None:
                    event[key] = str(task.get(key)).strip()
        invalid_event_count += _append_if_visible(events, event, cutoff)

    for run in experiment_runs or []:
        run_project_id = str(run.get("project_id") or "").strip()
        if run_project_id != project_id:
            scope_rejected_count += 1
            continue
        event = _event(
            source_type="experiment_run",
            source_id=run.get("run_id"),
            project_id=project_id,
            event_time=run.get("performed_at"),
            event_time_semantics="performed_at",
            title=run.get("experiment_name") or "实验运行",
            summary=run.get("dataset_version") or "已记录",
            status="recorded",
            workflow_id=run.get("workflow_id"),
        )
        invalid_event_count += _append_if_visible(events, event, cutoff)

    for decision in decisions or []:
        decision_project_id = str(decision.get("project_id") or "").strip()
        if decision_project_id != project_id:
            scope_rejected_count += 1
            continue
        event = _event(
            source_type="decision",
            source_id=decision.get("decision_id"),
            project_id=project_id,
            event_time=decision.get("decided_at"),
            event_time_semantics="decided_at",
            title="项目决策",
            summary=decision.get("statement") or "已记录",
            status="decided",
            workflow_id=decision.get("workflow_id"),
        )
        invalid_event_count += _append_if_visible(events, event, cutoff)

    for task in durable_tasks or []:
        if str(task.get("project_id") or "").strip() != project_id:
            scope_rejected_count += 1
            continue
        status = str(task.get("status") or "unknown").strip() or "unknown"
        current_step = str(task.get("current_step") or "").strip()
        event = _event(
            source_type="durable_task",
            source_id=task.get("task_id"),
            project_id=project_id,
            event_time=task.get("updated_at"),
            event_time_semantics="last_updated",
            title=task.get("kind") or "耐久任务",
            summary=current_step or status,
            status=status,
            workflow_id=task.get("workflow_id"),
        )
        invalid_event_count += _append_if_visible(events, event, cutoff)

    events.sort(key=lambda item: (-item["_event_epoch"], item["event_id"]))
    total_count = len(events)
    safe_limit = max(1, min(int(limit), 100))
    visible_events = [{key: value for key, value in item.items() if not key.startswith("_")} for item in events[:safe_limit]]
    view = {
        "schema_version": EPISODIC_VIEW_SCHEMA_VERSION,
        "project_id": project_id,
        "as_of": as_of,
        "event_count": total_count,
        "returned_count": len(visible_events),
        "invalid_event_count": invalid_event_count,
        "scope_rejected_count": scope_rejected_count,
        "source_counts": dict(Counter(item["source_type"] for item in events)),
        "events": visible_events,
    }
    view["view_digest"] = _sha256_digest(
        {
            "schema_version": view["schema_version"],
            "project_id": view["project_id"],
            "as_of": view["as_of"],
            "event_count": view["event_count"],
            "returned_count": view["returned_count"],
            "invalid_event_count": view["invalid_event_count"],
            "scope_rejected_count": view["scope_rejected_count"],
            "source_counts": view["source_counts"],
            "events": view["events"],
        }
    )
    return view


def _event(
    *,
    source_type: str,
    source_id: Any,
    project_id: str,
    event_time: Any,
    event_time_semantics: str,
    title: Any,
    summary: Any,
    status: str,
    workflow_id: Any,
) -> dict[str, Any]:
    source_id = str(source_id or "").strip()
    if not source_id:
        raise ValueError(f"{source_type} source_id is required")
    parsed = _parse_timestamp(event_time)
    if parsed is None:
        return {
            "_invalid": True,
            "event_id": f"{source_type}:{source_id}",
        }
    return {
        "event_id": f"{source_type}:{source_id}",
        "source_type": source_type,
        "source_id": source_id,
        "source_ref": f"{source_type}:{source_id}",
        "project_id": project_id,
        "workflow_id": str(workflow_id or "").strip() or None,
        "event_time": str(event_time).strip(),
        "event_time_semantics": event_time_semantics,
        "title": _compact(title, 160),
        "summary": _compact(summary, 240),
        "status": status,
        "_event_epoch": parsed.timestamp(),
    }


def _append_if_visible(
    events: list[dict[str, Any]],
    event: dict[str, Any],
    cutoff: datetime | None,
) -> int:
    if event.get("_invalid"):
        return 1
    if cutoff is not None and float(event["_event_epoch"]) > cutoff.timestamp():
        return 0
    events.append(event)
    return 0


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact(value: Any, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3].rstrip() + "..."


def _sha256_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
