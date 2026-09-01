import re
from typing import Any

from core.memory_gate import evaluate_memory_write_gate


VALID_ASSET_TYPES = {"memory", "skill"}
VALID_MEMORY_SUBTYPES = {"user_preference", "project_fact", "workflow_fact"}
VALID_SKILL_SUBTYPES = {"sop", "troubleshooting", "template", "pitfall"}
USER_BOUND_MEMORY_SUBTYPES = {"user_preference", "project_fact"}
SENSITIVE_PATTERNS = (
    "ignore previous instructions",
    "reveal the system prompt",
    "system prompt",
    "developer message",
    "api key",
    "secret key",
    "password",
    "credential",
)


def validate_learning_candidates(
    candidates: list[dict[str, Any]],
    messages: list[dict[str, str]],
    existing_assets: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    validated = []
    for candidate in candidates or []:
        normalized = _normalize_candidate(candidate, messages)
        if normalized is None:
            continue
        if _contains_sensitive_instruction(normalized):
            continue

        duplicate_id = _find_duplicate(normalized, existing_assets or {})
        if duplicate_id:
            normalized["duplicate_of"] = duplicate_id
            normalized["selected_by_default"] = False

        conflict_ids = _find_conflicts(normalized, existing_assets or {})
        if conflict_ids:
            normalized["conflicts_with"] = conflict_ids
            normalized["selected_by_default"] = False

        if normalized["confidence"] < 0.75:
            normalized["selected_by_default"] = False

        if normalized["asset_type"] == "memory":
            write_gate = evaluate_memory_write_gate(normalized, messages)
            normalized["write_gate"] = write_gate.public_dict()
            if write_gate.hard_block:
                continue
            if write_gate.decision != "review":
                normalized["selected_by_default"] = False

        validated.append(normalized)
    return validated


def _normalize_candidate(candidate: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None

    asset_type = str(candidate.get("asset_type") or "").strip()
    subtype = str(candidate.get("subtype") or "").strip()
    if asset_type not in VALID_ASSET_TYPES:
        return None
    if asset_type == "memory" and subtype not in VALID_MEMORY_SUBTYPES:
        return None
    if asset_type == "skill" and subtype not in VALID_SKILL_SUBTYPES:
        return None

    title = str(candidate.get("title") or "").strip()
    content = str(candidate.get("content") or "").strip()
    evidence = _string_list(candidate.get("evidence"))
    source_indexes = _source_indexes(candidate.get("source_message_indexes"), messages)
    if not title or not content or not evidence or not source_indexes:
        return None

    source_roles = {_role(messages[index].get("role")) for index in source_indexes}
    if asset_type == "memory" and subtype in USER_BOUND_MEMORY_SUBTYPES and "user" not in source_roles:
        return None

    if not _evidence_is_bound(evidence, source_indexes, messages):
        return None

    normalized = dict(candidate)
    normalized.update(
        {
            "asset_type": asset_type,
            "subtype": subtype,
            "title": title,
            "content": content,
            "evidence": evidence,
            "confidence": _confidence(candidate.get("confidence")),
            "selected_by_default": bool(candidate.get("selected_by_default")),
            "source_message_indexes": source_indexes,
            "tags": _string_list(candidate.get("tags")),
        }
    )
    return normalized


def _evidence_is_bound(evidence: list[str], source_indexes: list[int], messages: list[dict[str, str]]) -> bool:
    source_text = "\n".join(str(messages[index].get("content") or "") for index in source_indexes)
    normalized_source = _normalize_text(source_text)
    for item in evidence:
        normalized_evidence = _normalize_text(item)
        if not normalized_evidence:
            return False
        if normalized_evidence in normalized_source:
            continue
        evidence_terms = _terms(normalized_evidence)
        source_terms = _terms(normalized_source)
        if not evidence_terms or len(evidence_terms & source_terms) / len(evidence_terms) < 0.8:
            return False
    return True


def _find_duplicate(candidate: dict[str, Any], existing_assets: dict[str, list[dict[str, Any]]]) -> str | None:
    asset_key = "memories" if candidate["asset_type"] == "memory" else "skills"
    candidate_text = _asset_text(candidate)
    candidate_terms = _terms(candidate_text)
    for asset in existing_assets.get(asset_key) or []:
        asset_terms = _terms(_asset_text(asset))
        if not candidate_terms or not asset_terms:
            continue
        overlap = len(candidate_terms & asset_terms) / max(1, len(candidate_terms | asset_terms))
        if overlap >= 0.45 or _normalize_text(candidate.get("title")) == _normalize_text(asset.get("title")):
            return str(asset.get("memory_id") or asset.get("skill_id") or asset.get("asset_id") or "")
    return None


def _find_conflicts(candidate: dict[str, Any], existing_assets: dict[str, list[dict[str, Any]]]) -> list[str]:
    asset_key = "memories" if candidate["asset_type"] == "memory" else "skills"
    candidate_text = _normalize_text(_asset_text(candidate))
    conflicts = []
    for asset in existing_assets.get(asset_key) or []:
        asset_text = _normalize_text(_asset_text(asset))
        if _looks_conflicting(candidate_text, asset_text):
            conflicts.append(str(asset.get("memory_id") or asset.get("skill_id") or asset.get("asset_id") or ""))
    return [item for item in conflicts if item]


def _looks_conflicting(left: str, right: str) -> bool:
    if not left or not right:
        return False
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
    return any((a in left and b in right) or (b in left and a in right) for a, b in opposite_pairs)


def _contains_sensitive_instruction(candidate: dict[str, Any]) -> bool:
    text = _normalize_text(" ".join(str(candidate.get(key) or "") for key in ("title", "content", "rationale")))
    text += " " + _normalize_text(" ".join(candidate.get("evidence") or []))
    return any(pattern in text for pattern in SENSITIVE_PATTERNS)


def _asset_text(asset: dict[str, Any]) -> str:
    return " ".join(str(asset.get(key) or "") for key in ("title", "content", "intent", "tags"))


def _string_list(values: Any) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _source_indexes(values: Any, messages: list[dict[str, str]]) -> list[int]:
    indexes = []
    for value in values or []:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(messages) and index not in indexes:
            indexes.append(index)
    return indexes


def _role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "assistant" if normalized == "ai" else normalized


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _terms(value: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9_]{3,}", _normalize_text(value)) if term not in {"the", "and", "that"}}
