import hashlib
import json
import math
import time
from typing import Any

from core.answer_quality import content_terms
from core.memory_gate import contains_sensitive_memory, contains_untrusted_memory_instruction
from core.memory_roles import classify_memory_family, is_context_admissible_memory_family
from core.skill_runtime import SUPPORTED_WORKFLOW_IDS


MEMORY_PRIORITY = {
    "project_constraint": 0,
    "user_preference": 1,
    "project_fact": 2,
    "workflow_fact": 3,
}

MEMORY_DECAY_AFTER_SECONDS = 30 * 24 * 60 * 60


def _memory_freshness_weight(memory: dict[str, Any], *, now: float | None = None) -> float:
    """Lower stale memories without allowing age to erase a relevant fact."""
    reference_time = float(now if now is not None else time.time())
    anchors: list[float] = []
    for field in ("last_used_at", "confirmed_at", "created_at"):
        try:
            value = float(memory.get(field))
        except (TypeError, ValueError):
            continue
        # A read at a historical point must not become fresh because the
        # memory happened to be injected later in the real timeline.
        if value <= reference_time:
            anchors.append(value)
    if not anchors:
        return 1.0
    anchor_value = max(anchors)
    if anchor_value <= 0:
        return 1.0
    age = max(0.0, reference_time - anchor_value)
    if age <= MEMORY_DECAY_AFTER_SECONDS:
        return 1.0
    if age <= 90 * 24 * 60 * 60:
        return 0.85
    if age <= 180 * 24 * 60 * 60:
        return 0.65
    return 0.45


def build_memory_capsule(memories: list[dict[str, Any]], max_chars: int = 900) -> dict[str, Any]:
    ordered = sorted(
        memories or [],
        key=lambda item: (
            MEMORY_PRIORITY.get(str(item.get("subtype") or ""), 9),
            -float(item.get("updated_at") or 0),
            str(item.get("title") or ""),
            str(item.get("memory_id") or item.get("asset_id") or ""),
        ),
    )
    items = []
    lines = []
    used = 0
    for memory in ordered:
        title = _compact(memory.get("title") or "Memory", 80)
        content = _compact(memory.get("content") or "", 240)
        if not content:
            continue
        provenance = _memory_provenance_suffix(memory)
        prefix = f"- {title}: "
        line = f"{prefix}{content}{provenance}"
        projected = used + len(line) + (1 if lines else 0)
        if projected > max_chars:
            remaining = max_chars - used - (1 if lines else 0)
            if remaining < 24:
                break
            content_budget = remaining - len(prefix) - len(provenance)
            if content_budget > 0:
                line = f"{prefix}{_compact(content, content_budget)}{provenance}"
            else:
                # Preserve the leading memory_id in the provenance suffix when
                # the remaining budget cannot fit a readable content prefix.
                line = _compact(provenance, remaining)
        lines.append(line)
        used += len(line) + (1 if len(lines) > 1 else 0)
        items.append(memory)
        if used >= max_chars:
            break

    return {"items": items, "text": "\n".join(lines)}


def memory_prompt_binding_digest(
    memories: list[dict[str, Any]] | None,
    capsule_text: str,
    *,
    project_id: str | None,
    include_global: bool,
    workflow_id: str | None,
) -> str:
    payload = {
        "schema_version": "memory_prompt_binding_v1",
        "project_id": project_id,
        "include_global": bool(include_global),
        "workflow_id": workflow_id,
        "memories": [item for item in memories or [] if isinstance(item, dict)],
        "capsule_text": str(capsule_text or ""),
    }
    return _sha256_digest(payload)


def gate_memory_context(
    query: str,
    memories: list[dict[str, Any]],
    *,
    max_chars: int = 900,
    max_items: int = 3,
    reference_timestamp: float | None = None,
    project_id: str | None = None,
    include_global: bool = True,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    query_terms = content_terms(query)
    requested_project_id = str(project_id or "").strip() or None
    requested_workflow_id = str(workflow_id or "").strip() or None
    reference_time = (
        float(reference_timestamp)
        if reference_timestamp is not None
        else time.time()
    )
    accepted = []
    rejected = []
    for memory in memories or []:
        memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "")
        validity_reason = _memory_validity_reason(memory, reference_time)
        if validity_reason:
            rejected.append({"memory_id": memory_id, "reason": validity_reason})
            continue
        derived_family = classify_memory_family("memory", memory.get("subtype"))
        declared_family = str(memory.get("memory_family") or "").strip().casefold()
        if declared_family and declared_family != derived_family:
            rejected.append({"memory_id": memory_id, "reason": "memory_family_mismatch"})
            continue
        if not is_context_admissible_memory_family(derived_family):
            rejected.append({"memory_id": memory_id, "reason": "unclassified_memory_family"})
            continue
        memory = {**memory, "memory_family": derived_family}
        text = " ".join(
            str(memory.get(key) or "")
            for key in ("title", "content", "subtype")
        )
        if contains_untrusted_memory_instruction(text):
            rejected.append({"memory_id": memory_id, "reason": "untrusted_instruction"})
            continue
        if contains_sensitive_memory(text):
            rejected.append({"memory_id": memory_id, "reason": "sensitive_content"})
            continue

        memory_scope = str(memory.get("scope") or "global").strip().casefold() or "global"
        memory_project_id = str(memory.get("project_id") or "").strip() or None
        if memory_scope not in {"global", "project"}:
            rejected.append({"memory_id": memory_id, "reason": "invalid_memory_scope"})
            continue
        if memory_scope == "project":
            if requested_project_id is None:
                rejected.append(
                    {"memory_id": memory_id, "reason": "project_memory_scope_required"}
                )
                continue
            if memory_project_id != requested_project_id:
                rejected.append(
                    {"memory_id": memory_id, "reason": "project_memory_scope_mismatch"}
                )
                continue
        elif not include_global:
            rejected.append(
                {"memory_id": memory_id, "reason": "global_memory_scope_disabled"}
            )
            continue

        if str(memory.get("subtype") or "") == "workflow_fact":
            stored_workflow_id = str(memory.get("workflow_id") or "").strip() or None
            if requested_workflow_id is None:
                rejected.append(
                    {
                        "memory_id": memory_id,
                        "reason": "workflow_memory_namespace_required",
                    }
                )
                continue
            if requested_workflow_id not in SUPPORTED_WORKFLOW_IDS:
                rejected.append(
                    {
                        "memory_id": memory_id,
                        "reason": "workflow_memory_namespace_unknown",
                    }
                )
                continue
            if stored_workflow_id not in SUPPORTED_WORKFLOW_IDS:
                rejected.append(
                    {
                        "memory_id": memory_id,
                        "reason": "workflow_memory_namespace_unknown",
                    }
                )
                continue
            if stored_workflow_id != requested_workflow_id:
                rejected.append(
                    {
                        "memory_id": memory_id,
                        "reason": "workflow_memory_namespace_mismatch",
                    }
                )
                continue

        if str(memory.get("subtype") or "") == "project_constraint":
            if (
                str(memory.get("scope") or "") != "project"
                or not str(memory.get("project_id") or "").strip()
            ):
                rejected.append({"memory_id": memory_id, "reason": "project_constraint_unbound"})
                continue

        memory_terms = content_terms(text)
        overlap = query_terms & memory_terms
        lexical_relevance = len(overlap) / max(1, len(query_terms)) if query_terms else 0.0
        search_score = _safe_float(memory.get("score"))
        raw_relevance = max(search_score, lexical_relevance)
        freshness_weight = _memory_freshness_weight(
            memory,
            now=reference_time,
        )
        relevance = raw_relevance * freshness_weight
        global_preference = (
            str(memory.get("subtype") or "") == "user_preference"
            and _is_global_control_preference(text)
        )
        project_constraint = (
            str(memory.get("subtype") or "") == "project_constraint"
            and str(memory.get("scope") or "") == "project"
            and bool(str(memory.get("project_id") or "").strip())
        )
        if relevance < 0.08 and not global_preference and not project_constraint:
            rejected.append({"memory_id": memory_id, "reason": "query_irrelevant"})
            continue

        accepted.append(
            {
                **memory,
                "context_relevance": round(
                    max(relevance, 0.08 if (global_preference or project_constraint) else 0.0),
                    6,
                ),
                "freshness_weight": freshness_weight,
                "context_gate_reason": (
                    "project_constraint"
                    if project_constraint and relevance < 0.08
                    else "global_control_preference"
                    if global_preference and relevance < 0.08
                    else "query_relevant"
                ),
            }
        )

    accepted.sort(
        key=lambda item: (
            -_safe_float(item.get("context_relevance")),
            MEMORY_PRIORITY.get(str(item.get("subtype") or ""), 9),
            -_safe_float(item.get("updated_at")),
            str(item.get("memory_id") or item.get("asset_id") or ""),
        )
    )
    conflict_free = []
    for memory in accepted:
        conflict = next(
            (
                previous
                for previous in conflict_free
                if _looks_like_conflicting_preference(memory, previous)
                or _looks_like_conflicting_topic(memory, previous)
            ),
            None,
        )
        if conflict is None:
            conflict_free.append(memory)
            continue
        winner, loser = max(
            (memory, conflict),
            key=_preference_authority,
        ), min(
            (memory, conflict),
            key=_preference_authority,
        )
        if winner is memory:
            conflict_free.remove(conflict)
            conflict_free.append(memory)
        rejected.append(
            {
                "memory_id": str(loser.get("memory_id") or loser.get("asset_id") or ""),
                "reason": (
                    "conflicting_memory_topic"
                    if _looks_like_conflicting_topic(memory, conflict)
                    else "conflicting_preference"
                ),
                "conflict_with": str(winner.get("memory_id") or winner.get("asset_id") or ""),
            }
        )
    accepted = conflict_free
    deduplicated: list[dict[str, Any]] = []
    deduplicated_positions: dict[str, int] = {}
    for memory in accepted:
        memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "").strip()
        if not memory_id:
            deduplicated.append(memory)
            continue
        previous_position = deduplicated_positions.get(memory_id)
        if previous_position is None:
            deduplicated_positions[memory_id] = len(deduplicated)
            deduplicated.append(memory)
            continue
        previous = deduplicated[previous_position]
        winner = max(
            (memory, previous),
            key=lambda item: (
                1 if str(item.get("scope") or "") == "project" else 0,
                1 if str(item.get("confirmation_status") or "") == "human_reviewed" else 0,
                _safe_float(item.get("record_version")),
                _safe_float(item.get("freshness_weight")),
                _safe_float(item.get("context_relevance")),
                _safe_float(item.get("updated_at")),
            ),
        )
        loser = previous if winner is memory else memory
        if winner is memory:
            deduplicated[previous_position] = memory
        rejected.append(
            {
                "memory_id": str(loser.get("memory_id") or loser.get("asset_id") or ""),
                "reason": "duplicate_memory_id",
                "conflict_with": str(winner.get("memory_id") or winner.get("asset_id") or ""),
            }
        )
    accepted = deduplicated
    capsule = build_memory_capsule(accepted[:max_items], max_chars=max_chars)
    admitted_ids = {
        str(item.get("memory_id") or item.get("asset_id") or "")
        for item in capsule["items"]
    }
    admitted_memory_ids = [
        str(item.get("memory_id") or item.get("asset_id") or "")
        for item in capsule["items"]
        if str(item.get("memory_id") or item.get("asset_id") or "")
    ]
    budget_rejected = [
        {
            "memory_id": str(item.get("memory_id") or item.get("asset_id") or ""),
            "reason": "memory_context_budget",
        }
        for item in accepted
        if str(item.get("memory_id") or item.get("asset_id") or "") not in admitted_ids
    ]
    if capsule["items"]:
        decision = "accept"
        decision_reason = "memory_context_admitted"
    elif accepted:
        decision = "skip"
        decision_reason = "memory_context_budget"
    elif memories:
        decision = "skip"
        decision_reason = "no_admissible_memory"
    else:
        decision = "skip"
        decision_reason = "no_memory_candidates"
    gate = {
        "schema_version": "memory_read_gate_v1",
        "decision": decision,
        "decision_reason": decision_reason,
        "accepted_count": len(capsule["items"]),
        "accepted_memory_ids": admitted_memory_ids,
        "rejected_count": len(rejected) + len(budget_rejected),
        "budget_chars": max_chars,
        "used_chars": len(capsule["text"]),
        "rejected": [*rejected, *budget_rejected],
    }
    gate["prompt_binding_digest"] = memory_prompt_binding_digest(
        capsule["items"],
        capsule["text"],
        project_id=requested_project_id,
        include_global=include_global,
        workflow_id=requested_workflow_id,
    )
    gate["decision_digest"] = _memory_gate_decision_digest(
        query=query,
        gate=gate,
        capsule_text=capsule["text"],
        max_items=max_items,
        project_id=requested_project_id,
        include_global=include_global,
        workflow_id=requested_workflow_id,
    )
    return {
        "memories": capsule["items"],
        "memory_capsule": capsule,
        "gate": gate,
    }


def _memory_gate_decision_digest(
    *,
    query: str,
    gate: dict[str, Any],
    capsule_text: str,
    max_items: int,
    project_id: str | None,
    include_global: bool,
    workflow_id: str | None,
) -> str:
    """Commit to a memory read decision without returning its raw context."""
    rejected = []
    for item in gate.get("rejected") or []:
        rejected.append(
            {
                "memory_id": str(item.get("memory_id") or ""),
                "reason": str(item.get("reason") or ""),
                "conflict_with": str(item.get("conflict_with") or ""),
            }
        )
    rejected.sort(
        key=lambda item: (
            item["memory_id"],
            item["reason"],
            item["conflict_with"],
        )
    )
    payload = {
        "schema_version": gate["schema_version"],
        "query_digest": _sha256_digest(" ".join(str(query or "").split())),
        "project_id": project_id,
        "include_global": bool(include_global),
        "workflow_id": workflow_id,
        "decision": gate["decision"],
        "decision_reason": gate.get("decision_reason"),
        "accepted_memory_ids": list(gate.get("accepted_memory_ids") or []),
        "rejected": rejected,
        "budget_chars": gate["budget_chars"],
        "max_items": max_items,
        "used_chars": gate["used_chars"],
        "capsule_text_digest": _sha256_digest(capsule_text),
    }
    return _sha256_digest(payload)


def _sha256_digest(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact(value: Any, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3].rstrip() + "..."


def _memory_provenance_suffix(memory: dict[str, Any]) -> str:
    """Keep trace metadata while excluding the original conversation excerpt."""
    fields = []
    memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "").strip()
    if memory_id:
        fields.append(f"memory_id={memory_id}")

    scope = str(memory.get("scope") or "global").strip() or "global"
    project_id = str(memory.get("project_id") or "").strip()
    fields.append(
        f"scope=project:{project_id}" if scope == "project" and project_id else f"scope={scope}"
    )
    trace = memory.get("source_trace") or {}
    workflow_id = str(memory.get("workflow_id") or "").strip()
    if workflow_id:
        fields.append(f"workflow={workflow_id}")
    source_workflow_id = str(memory.get("source_workflow_id") or trace.get("workflow_id") or "").strip()
    if source_workflow_id and source_workflow_id != workflow_id:
        fields.append(f"source_workflow={source_workflow_id}")

    for field in ("valid_from", "invalid_at"):
        if memory.get(field) is not None:
            value = memory.get(field)
            try:
                numeric = float(value)
                value = int(numeric) if numeric.is_integer() else numeric
            except (TypeError, ValueError):
                value = str(value).strip()
            if value != "":
                fields.append(f"{field}={value}")

    source_session_id = str(memory.get("source_session_id") or trace.get("session_id") or "").strip()
    if source_session_id:
        fields.append(f"source_session={source_session_id}")
    message_ids = memory.get("source_message_ids") or trace.get("message_ids") or []
    normalized_ids = [str(item).strip() for item in message_ids if str(item).strip()]
    if normalized_ids:
        fields.append(f"source_message_ids={','.join(normalized_ids[:4])}")
    message_sequences = memory.get("source_message_sequences") or trace.get("message_sequences") or []
    normalized_sequences = [str(item).strip() for item in message_sequences if str(item).strip()]
    if normalized_sequences:
        fields.append(f"source_sequences={','.join(normalized_sequences[:8])}")
    message_indexes = memory.get("source_message_indexes") or trace.get("message_indexes") or []
    normalized_indexes = [str(item).strip() for item in message_indexes if str(item).strip()]
    if normalized_indexes:
        fields.append(f"source_messages={','.join(normalized_indexes[:8])}")
    return f"（{'；'.join(fields)}）" if fields else ""


def _is_global_control_preference(text: str) -> bool:
    normalized = str(text or "").lower()
    markers = (
        "response language",
        "output format",
        "human review",
        "memory write",
        "long-term memory",
        "回复语言",
        "输出格式",
        "人工审核",
        "记忆写入",
        "长期记忆",
    )
    return any(marker in normalized for marker in markers)


def _looks_like_conflicting_preference(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left.get("subtype") or "") != "user_preference" or str(right.get("subtype") or "") != "user_preference":
        return False
    left_text = " ".join(str(left.get(key) or "").lower() for key in ("title", "content"))
    right_text = " ".join(str(right.get(key) or "").lower() for key in ("title", "content"))
    opposite_pairs = (
        ("automatic", "manual"),
        ("always", "never"),
        ("every answer", "before closing"),
        ("without review", "review before"),
        ("自动", "手动"),
        ("每次回答", "关闭前"),
        ("每次提问", "关闭前"),
        ("无需审核", "需要审核"),
        ("不需要审核", "需要审核"),
        ("中文", "英文"),
    )
    return any(
        (left_marker in left_text and right_marker in right_text)
        or (right_marker in left_text and left_marker in right_text)
        for left_marker, right_marker in opposite_pairs
    )


def _looks_like_conflicting_topic(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Treat same-titled, differently worded facts as one read-time topic."""
    if str(left.get("subtype") or "") != str(right.get("subtype") or ""):
        return False
    left_title = " ".join(str(left.get("title") or "").lower().split())
    right_title = " ".join(str(right.get("title") or "").lower().split())
    if not left_title or left_title != right_title:
        return False
    left_content = " ".join(str(left.get("content") or "").lower().split())
    right_content = " ".join(str(right.get("content") or "").lower().split())
    return bool(left_content and right_content and left_content != right_content)


def _preference_authority(memory: dict[str, Any]) -> tuple[int, int, float, float, float]:
    return (
        1 if str(memory.get("scope") or "") == "project" else 0,
        1 if str(memory.get("confirmation_status") or "") == "human_reviewed" else 0,
        _safe_float(memory.get("freshness_weight")),
        _safe_float(memory.get("context_relevance")),
        _safe_float(memory.get("updated_at")),
    )


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _memory_validity_reason(memory: dict[str, Any], reference_time: float) -> str | None:
    """Fail closed if a candidate is outside its declared point-in-time interval."""
    valid_from, valid_from_error = _optional_finite_timestamp(memory.get("valid_from"))
    invalid_at, invalid_at_error = _optional_finite_timestamp(memory.get("invalid_at"))
    if valid_from_error or invalid_at_error:
        return "invalid_memory_validity"
    if valid_from is not None and invalid_at is not None and invalid_at <= valid_from:
        return "invalid_memory_validity"
    if valid_from is not None and valid_from > reference_time:
        return "memory_not_yet_valid"
    if invalid_at is not None and invalid_at <= reference_time:
        return "memory_expired"
    return None


def _optional_finite_timestamp(value: Any) -> tuple[float | None, bool]:
    if value is None or str(value).strip() == "":
        return None, False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(parsed):
        return None, True
    return parsed, False
