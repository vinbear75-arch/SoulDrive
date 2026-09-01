import re
from dataclasses import dataclass
from typing import Any


SENSITIVE_MEMORY_PATTERN = re.compile(
    r"api[_ -]?key|secret[_ -]?key|password|credential|access[_ -]?token|"
    r"private[_ -]?key|system prompt|developer message|"
    r"口令|密码|密钥|访问令牌|系统提示词|开发者消息|银行卡|身份证号",
    flags=re.IGNORECASE,
)
EXPLICIT_MEMORY_PATTERN = re.compile(
    r"remember|keep in mind|i prefer|i want|always|never|"
    r"记住|请记|偏好|希望|不要|以后|始终|每次|默认",
    flags=re.IGNORECASE,
)
DURABLE_MEMORY_PATTERN = re.compile(
    r"project|workflow|process|policy|preference|default|before|after|"
    r"项目|流程|规范|偏好|默认|长期|关闭前|每次|固定",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryWriteGateResult:
    decision: str
    score: float
    reasons: tuple[str, ...]
    requires_human_review: bool
    hard_block: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "memory_write_gate_v1",
            "decision": self.decision,
            "score": round(self.score, 6),
            "reasons": list(self.reasons),
            "requires_human_review": self.requires_human_review,
            "hard_block": self.hard_block,
        }


def evaluate_memory_write_gate(
    candidate: dict[str, Any],
    messages: list[dict[str, str]],
) -> MemoryWriteGateResult:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "content", "rationale")
    )
    text += " " + " ".join(str(item) for item in candidate.get("evidence") or [])
    if contains_sensitive_memory(text):
        return MemoryWriteGateResult(
            "reject",
            0.0,
            ("sensitive_or_privileged_content",),
            False,
            True,
        )

    source_indexes = [
        int(index)
        for index in candidate.get("source_message_indexes") or []
        if str(index).isdigit() and 0 <= int(index) < len(messages)
    ]
    source_roles = {
        _normalized_role(messages[index].get("role"))
        for index in source_indexes
    }
    confidence = _clamp(candidate.get("confidence"))
    score = confidence * 0.25
    reasons: list[str] = []

    if "user" in source_roles:
        score += 0.20
        reasons.append("user_grounded")
    elif candidate.get("asset_type") == "skill" and "assistant" in source_roles:
        score += 0.12
        reasons.append("assistant_skill_grounded")

    if EXPLICIT_MEMORY_PATTERN.search(text):
        score += 0.20
        reasons.append("explicit_intent")
    if DURABLE_MEMORY_PATTERN.search(text):
        score += 0.15
        reasons.append("durable_value")

    content = str(candidate.get("content") or "").strip()
    if 12 <= len(content) <= 2000:
        score += 0.10
        reasons.append("specific_content")
    if candidate.get("evidence") and source_indexes:
        score += 0.10
        reasons.append("evidence_bound")
    if candidate.get("duplicate_of"):
        score -= 0.30
        reasons.append("duplicate_candidate")
    if candidate.get("conflicts_with"):
        score -= 0.20
        reasons.append("conflicting_candidate")

    score = max(0.0, min(score, 1.0))
    decision = "review" if score >= 0.72 else "hold"
    if decision == "hold":
        reasons.append("insufficient_durable_signal")
    return MemoryWriteGateResult(
        decision,
        score,
        tuple(reasons),
        True,
        False,
    )


def contains_sensitive_memory(text: str) -> bool:
    return bool(SENSITIVE_MEMORY_PATTERN.search(str(text or "")))


def _normalized_role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "assistant" if normalized == "ai" else normalized


def _clamp(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))
