from typing import Any

from core.answer_quality import content_terms
from core.memory_gate import contains_sensitive_memory


MEMORY_PRIORITY = {
    "user_preference": 0,
    "project_fact": 1,
    "workflow_fact": 2,
}


def build_memory_capsule(memories: list[dict[str, Any]], max_chars: int = 900) -> dict[str, Any]:
    ordered = sorted(
        memories or [],
        key=lambda item: (
            MEMORY_PRIORITY.get(str(item.get("subtype") or ""), 9),
            -float(item.get("updated_at") or 0),
            str(item.get("title") or ""),
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
        line = f"- {title}: {content}"
        projected = used + len(line) + (1 if lines else 0)
        if projected > max_chars:
            remaining = max_chars - used - (1 if lines else 0)
            if remaining < 24:
                break
            line = _compact(line, remaining)
        lines.append(line)
        used += len(line) + (1 if len(lines) > 1 else 0)
        items.append(memory)
        if used >= max_chars:
            break

    return {"items": items, "text": "\n".join(lines)}


def gate_memory_context(
    query: str,
    memories: list[dict[str, Any]],
    *,
    max_chars: int = 900,
    max_items: int = 3,
) -> dict[str, Any]:
    query_terms = content_terms(query)
    accepted = []
    rejected = []
    for memory in memories or []:
        text = " ".join(
            str(memory.get(key) or "")
            for key in ("title", "content", "subtype")
        )
        memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "")
        if contains_sensitive_memory(text):
            rejected.append({"memory_id": memory_id, "reason": "sensitive_content"})
            continue

        memory_terms = content_terms(text)
        overlap = query_terms & memory_terms
        lexical_relevance = len(overlap) / max(1, len(query_terms)) if query_terms else 0.0
        search_score = _safe_float(memory.get("score"))
        relevance = max(search_score, lexical_relevance)
        global_preference = (
            str(memory.get("subtype") or "") == "user_preference"
            and _is_global_control_preference(text)
        )
        if relevance < 0.08 and not global_preference:
            rejected.append({"memory_id": memory_id, "reason": "query_irrelevant"})
            continue

        accepted.append(
            {
                **memory,
                "context_relevance": round(max(relevance, 0.08 if global_preference else 0.0), 6),
                "context_gate_reason": "global_control_preference" if global_preference and relevance < 0.08 else "query_relevant",
            }
        )

    accepted.sort(
        key=lambda item: (
            -_safe_float(item.get("context_relevance")),
            MEMORY_PRIORITY.get(str(item.get("subtype") or ""), 9),
            -_safe_float(item.get("updated_at")),
        )
    )
    capsule = build_memory_capsule(accepted[:max_items], max_chars=max_chars)
    admitted_ids = {
        str(item.get("memory_id") or item.get("asset_id") or "")
        for item in capsule["items"]
    }
    budget_rejected = [
        {
            "memory_id": str(item.get("memory_id") or item.get("asset_id") or ""),
            "reason": "memory_context_budget",
        }
        for item in accepted
        if str(item.get("memory_id") or item.get("asset_id") or "") not in admitted_ids
    ]
    return {
        "memories": capsule["items"],
        "memory_capsule": capsule,
        "gate": {
            "schema_version": "memory_read_gate_v1",
            "decision": "accept" if capsule["items"] else "skip",
            "accepted_count": len(capsule["items"]),
            "rejected_count": len(rejected) + len(budget_rejected),
            "budget_chars": max_chars,
            "used_chars": len(capsule["text"]),
            "rejected": [*rejected, *budget_rejected],
        },
    }


def _compact(value: Any, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3].rstrip() + "..."


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


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
