from typing import Any


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


def _compact(value: Any, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3].rstrip() + "..."
