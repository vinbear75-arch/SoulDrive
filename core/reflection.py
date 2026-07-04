import json
import re
from typing import Any


VALID_ASSET_TYPES = {"memory", "skill"}
VALID_MEMORY_SUBTYPES = {"user_preference", "project_fact", "workflow_fact"}
VALID_SKILL_SUBTYPES = {"sop", "troubleshooting", "template", "pitfall"}


def build_reflection_candidates(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, message in enumerate(messages or []):
        role = _normalized_role(message.get("role"))
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        candidate = None
        if role == "user":
            candidate = _build_memory_candidate(content, index)
        elif role == "assistant":
            candidate = _build_skill_candidate(content, index)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def parse_reflection_response(raw: str, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    parsed = _parse_json_payload(raw)
    if parsed is None:
        return []
    items = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return []
    return [
        candidate
        for candidate in (_normalize_candidate(item, messages) for item in items)
        if candidate is not None
    ]


def build_reflection_prompt(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    transcript = "\n".join(
        f"[{index}] { _normalized_role(message.get('role')) }: {message.get('content')}"
        for index, message in enumerate(messages or [])
    )
    system = (
        "你是 SoulDrive 的任务后反思器。"
        "只允许从给定 transcript 中提取可长期复用的候选 memory 或 skill。"
        "不要推断 transcript 中没有明确支持的信息。"
        "用户偏好和项目事实必须来自 user 消息。"
        "skill 可以来自完整对话，但必须带上来源消息索引。"
        "输出 JSON，格式为 {\"candidates\": [...]}。"
    )
    user = (
        "请提取候选长期资产。字段包括 asset_type, subtype, title, content, evidence, rationale, "
        "confidence, selected_by_default, source_message_indexes, tags。\n\n"
        f"<transcript>\n{transcript}\n</transcript>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_memory_candidate(content: str, index: int) -> dict[str, Any] | None:
    subtype = _detect_memory_subtype(content)
    if subtype is None:
        return None
    title = _memory_title(subtype, content)
    return {
        "asset_type": "memory",
        "subtype": subtype,
        "title": title,
        "content": _compact(content),
        "evidence": [_compact(content, 160)],
        "rationale": "用户在对话中明确表达了可长期复用的偏好或事实，适合沉淀为 memory。",
        "confidence": 0.86,
        "selected_by_default": True,
        "source_message_indexes": [index],
        "tags": ["memory", subtype],
    }


def _build_skill_candidate(content: str, index: int) -> dict[str, Any] | None:
    subtype = _detect_skill_subtype(content)
    if subtype is None:
        return None
    title = _skill_title(subtype, content)
    return {
        "asset_type": "skill",
        "subtype": subtype,
        "title": title,
        "content": _compact(content),
        "evidence": [_compact(content, 160)],
        "rationale": "对话中出现了可复用的操作步骤、模板或排障套路，适合沉淀为 skill。",
        "confidence": 0.78,
        "selected_by_default": False,
        "source_message_indexes": [index],
        "tags": ["skill", subtype],
    }


def _normalize_candidate(item: Any, messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    asset_type = str(item.get("asset_type") or "").strip()
    subtype = str(item.get("subtype") or "").strip()
    if asset_type not in VALID_ASSET_TYPES:
        return None
    if asset_type == "memory" and subtype not in VALID_MEMORY_SUBTYPES:
        return None
    if asset_type == "skill" and subtype not in VALID_SKILL_SUBTYPES:
        return None

    evidence = _clean_string_list(item.get("evidence"))
    source_indexes = _normalize_source_indexes(item.get("source_message_indexes"), messages)
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    if not title or not content or not evidence or not source_indexes:
        return None
    if asset_type == "memory" and subtype in {"user_preference", "project_fact"}:
        if not any(_normalized_role(messages[index].get("role")) == "user" for index in source_indexes):
            return None

    confidence = _clamp_confidence(item.get("confidence"))
    tags = _clean_string_list(item.get("tags"))
    return {
        "asset_type": asset_type,
        "subtype": subtype,
        "title": title,
        "content": content,
        "evidence": evidence,
        "rationale": str(item.get("rationale") or "").strip(),
        "confidence": confidence,
        "selected_by_default": bool(item.get("selected_by_default")),
        "source_message_indexes": source_indexes,
        "tags": tags,
    }


def _parse_json_payload(raw: str) -> Any | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = _strip_fenced_json(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _strip_fenced_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if match is not None:
        return match.group(1).strip()
    return text.strip("` \n")


def _normalized_role(role: Any) -> str:
    value = str(role or "").strip().lower()
    if value in {"ai", "assistant"}:
        return "assistant"
    if value == "user":
        return "user"
    return value


def _detect_memory_subtype(content: str) -> str | None:
    preference_markers = ("我希望", "我不希望", "不要", "偏好", "以后", "关闭前", "集中审核", "记忆", "保存", "审核", "弹窗")
    project_markers = ("项目", "工作区", "分支", "仓库", "workspace", "main 分支", "学习闭环", "RAG")
    workflow_markers = ("流程", "步骤", "习惯", "做法", "每次", "通常")

    if any(marker in content for marker in preference_markers):
        return "user_preference"
    if any(marker in content for marker in project_markers):
        return "project_fact"
    if any(marker in content for marker in workflow_markers):
        return "workflow_fact"
    return None


def _detect_skill_subtype(content: str) -> str | None:
    if any(marker in content for marker in ("排障", "报错", "失败原因", "恢复", "修复")):
        return "troubleshooting"
    if any(marker in content for marker in ("模板", "格式", "示例", "JSON", "schema")):
        return "template"
    if any(marker in content for marker in ("坑", "注意", "不要", "避免", "风险")):
        return "pitfall"
    if any(marker in content for marker in ("步骤", "先", "然后", "最后", "流程", "SOP")):
        return "sop"
    return None


def _memory_title(subtype: str, content: str) -> str:
    if subtype == "user_preference":
        if "关闭前" in content and "审核" in content:
            return "关闭前集中审核记忆"
        return "用户长期偏好"
    if subtype == "project_fact":
        return "项目事实"
    return "工作流事实"


def _skill_title(subtype: str, content: str) -> str:
    if subtype == "troubleshooting":
        return "排障流程"
    if subtype == "template":
        return "输出模板"
    if subtype == "pitfall":
        return "常见坑点"
    return "可复用流程"


def _compact(content: str, limit: int = 220) -> str:
    normalized = " ".join(str(content or "").split())
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip()


def _clean_string_list(values: Any) -> list[str]:
    cleaned: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def _normalize_source_indexes(values: Any, messages: list[dict[str, str]]) -> list[int]:
    indexes: list[int] = []
    for value in values or []:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(messages):
            if index not in indexes:
                indexes.append(index)
    return indexes


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(confidence, 1.0))
