import re
from typing import Any, Callable


DEFAULT_COMPACTION_THRESHOLD = 16
DEFAULT_RECENT_WINDOW = 8


def build_recoverable_summary(
    records: list[dict[str, Any]],
    *,
    max_chars: int = 2400,
    summary_builder: Callable[[list[dict[str, Any]], int], str] | None = None,
) -> dict[str, Any] | None:
    if not records:
        return None
    ordered = sorted(records, key=lambda item: int(item.get("sequence") or 0))
    recent_window = max(1, min(DEFAULT_RECENT_WINDOW, len(ordered)))
    covered = ordered[:-recent_window]
    if not covered:
        return None
    input_sequence_start = int(covered[0].get("sequence") or 0)
    input_sequence_end = int(covered[-1].get("sequence") or 0)
    if summary_builder:
        text = summary_builder(covered, max_chars).strip()
        excerpt_message_ids = None
        excerpt_truncated = None
        excerpt_status = "unknown"
    else:
        excerpted, excerpt_truncated = _select_excerpt_records(covered, max_chars)
        text = _extractive_summary(excerpted, max_chars)
        excerpt_message_ids = [
            str(item.get("message_id") or "")
            for item in excerpted
            if str(item.get("message_id") or "")
        ]
        excerpt_status = "known"
    if not text:
        return None
    return {
        "session_id": str(ordered[0].get("session_id") or ""),
        "version": 1,
        "summary": text,
        # The legacy covered range describes the full input window.  It is
        # intentionally not narrowed to the messages that fit the excerpt.
        "covered_sequence_start": input_sequence_start,
        "covered_sequence_end": input_sequence_end,
        "input_sequence_start": input_sequence_start,
        "input_sequence_end": input_sequence_end,
        "input_message_count": len(covered),
        "source_message_ids": [str(item.get("message_id") or "") for item in covered],
        "excerpt_message_ids": excerpt_message_ids,
        "excerpt_truncated": excerpt_truncated,
        "excerpt_status": excerpt_status,
        "compression": "extractive_recoverable_v1",
    }


def next_summary_version(current: dict[str, Any] | None) -> int:
    return int((current or {}).get("version") or 0) + 1


def _extractive_summary(records: list[dict[str, Any]], max_chars: int) -> str:
    header = "该摘要只压缩上下文，完整消息仍保存在本地加密会话文件中；需要时可按序号回源。"
    budget = max(1, int(max_chars))
    if len(header) > budget:
        return _compact(header, budget)
    lines = [header]
    used = len(lines[0])
    for record in records:
        content = _normalize(str(record.get("content") or ""))
        if not content:
            continue
        excerpt = _compact(content, 280)
        line = f"[{record.get('sequence')}] {record.get('role')}: {excerpt}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _select_excerpt_records(
    records: list[dict[str, Any]],
    max_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Choose newer records first, then restore chronological presentation."""
    candidates = [
        record
        for record in records
        if _normalize(str(record.get("content") or ""))
    ]
    selected: list[dict[str, Any]] = []
    used = len("该摘要只压缩上下文，完整消息仍保存在本地加密会话文件中；需要时可按序号回源。")
    budget = max(1, int(max_chars))
    for record in reversed(candidates):
        content = _normalize(str(record.get("content") or ""))
        line = f"[{record.get('sequence')}] {record.get('role')}: {_compact(content, 280)}"
        if used + len(line) + 1 > budget:
            break
        selected.append(record)
        used += len(line) + 1
    selected.reverse()
    return selected, len(selected) < len(candidates)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()


def _compact(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."
