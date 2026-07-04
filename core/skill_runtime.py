import re
from typing import Any


DEFAULT_OUTPUT_CONTRACT = "Return a concise result that follows the skill steps and calls out any missing inputs."
DEFAULT_SUCCESS_CRITERIA = ["The task follows the approved steps.", "The result satisfies the user's current request."]


def normalize_skill_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    title = str(normalized.get("title") or "Reusable skill").strip()
    content = str(normalized.get("content") or "").strip()
    steps = _string_list(normalized.get("steps")) or _extract_steps(content)
    if not steps and content:
        steps = [content]
    if not steps:
        steps = ["Clarify the task, execute the reusable process, and verify the result."]

    normalized.update(
        {
            "title": title,
            "content": content,
            "intent": str(normalized.get("intent") or title).strip(),
            "triggers": _dedupe(_string_list(normalized.get("triggers")) + _trigger_terms(title, content)),
            "required_inputs": _string_list(normalized.get("required_inputs")),
            "applicability": str(normalized.get("applicability") or content or title).strip(),
            "steps": steps,
            "tool_permissions": _string_list(normalized.get("tool_permissions")),
            "output_contract": str(normalized.get("output_contract") or DEFAULT_OUTPUT_CONTRACT).strip(),
            "success_criteria": _string_list(normalized.get("success_criteria")) or list(DEFAULT_SUCCESS_CRITERIA),
            "failure_modes": _string_list(normalized.get("failure_modes")),
            "eval_cases": list(normalized.get("eval_cases") or []),
            "tags": _string_list(normalized.get("tags")),
        }
    )
    return normalized


def match_skills(query: str, skills: list[dict[str, Any]], top_k: int = 3, min_score: float = 0.25) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    if not query_terms:
        return []

    matches = []
    for skill in skills or []:
        normalized_skill = normalize_skill_payload(skill)
        skill_terms = _terms(_skill_text(normalized_skill))
        trigger_terms = _terms(" ".join(normalized_skill.get("triggers") or []))
        if not skill_terms:
            continue
        overlap = len(query_terms & skill_terms) / max(1, len(query_terms))
        trigger_overlap = len(query_terms & trigger_terms) / max(1, len(query_terms))
        shared_term_boost = min(0.5, len(query_terms & skill_terms) * 0.08)
        score = min(1.0, overlap + trigger_overlap * 0.5 + shared_term_boost)
        if score < min_score:
            continue
        matches.append(
            {
                "skill_id": normalized_skill.get("skill_id") or normalized_skill.get("asset_id") or "",
                "title": normalized_skill.get("title") or "",
                "score": round(score, 4),
                "reason": _match_reason(query_terms, normalized_skill),
                "skill": normalized_skill,
            }
        )

    matches.sort(key=lambda item: (-item["score"], item["title"]))
    return matches[:top_k]


def create_skill_execution_plan(
    skill: dict[str, Any],
    query: str,
    *,
    matched_score: float = 0.0,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_skill = normalize_skill_payload(skill)
    return {
        "skill_id": normalized_skill.get("skill_id") or normalized_skill.get("asset_id") or "",
        "skill_title": normalized_skill.get("title") or "",
        "status": "planned",
        "query": str(query or ""),
        "matched_score": matched_score,
        "required_inputs": list(normalized_skill.get("required_inputs") or []),
        "inputs": dict(inputs or {}),
        "steps": [
            {
                "index": index + 1,
                "instruction": step,
                "expected_output": "Step result or verification note.",
            }
            for index, step in enumerate(normalized_skill.get("steps") or [])
        ],
        "tool_permissions": list(normalized_skill.get("tool_permissions") or []),
        "output_contract": normalized_skill.get("output_contract") or DEFAULT_OUTPUT_CONTRACT,
        "success_criteria": list(normalized_skill.get("success_criteria") or DEFAULT_SUCCESS_CRITERIA),
        "failure_modes": list(normalized_skill.get("failure_modes") or []),
    }


def _extract_steps(content: str) -> list[str]:
    normalized = str(content or "").strip()
    if not normalized:
        return []
    lines = []
    for raw_line in normalized.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line).strip()
        if line:
            lines.append(line)
    if len(lines) > 1:
        return lines
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    return sentences if len(sentences) > 1 else lines


def _trigger_terms(title: str, content: str) -> list[str]:
    text = f"{title} {content}".lower()
    triggers = []
    for marker in ("tdd", "test", "implement", "workflow", "troubleshoot", "template", "review"):
        if marker in text:
            triggers.append(marker)
    return triggers


def _match_reason(query_terms: set[str], skill: dict[str, Any]) -> str:
    matched = sorted(query_terms & _terms(_skill_text(skill)))
    if not matched:
        return "matched by approved skill metadata"
    return f"matched terms: {', '.join(matched[:5])}"


def _skill_text(skill: dict[str, Any]) -> str:
    parts = [
        skill.get("title"),
        skill.get("content"),
        skill.get("intent"),
        skill.get("applicability"),
        " ".join(skill.get("triggers") or []),
        " ".join(skill.get("steps") or []),
        " ".join(skill.get("tags") or []),
    ]
    return " ".join(str(part or "") for part in parts)


def _string_list(values: Any) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        marker = value.lower()
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _terms(value: Any) -> set[str]:
    stopwords = {"the", "and", "with", "this", "that", "does", "what", "about", "please"}
    text = str(value or "").lower()
    terms = {term for term in re.findall(r"[a-z0-9_]{3,}", text) if term not in stopwords}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        max_size = min(4, len(chunk))
        for size in range(2, max_size + 1):
            for index in range(0, len(chunk) - size + 1):
                terms.add(chunk[index : index + size])
    return terms
