import math
import re
from dataclasses import dataclass
from typing import Any


SENSITIVE_MEMORY_PATTERN = re.compile(
    r"api[_ -]?key|secret[_ -]?key|password|credential|access[_ -]?token|"
    r"private[_ -]?key|system prompt|developer message|"
    r"口令|密码|密钥|访问令牌|系统提示词|开发者消息|银行卡|身份证号",
    flags=re.IGNORECASE,
)
UNTRUSTED_MEMORY_INSTRUCTION_PATTERN = re.compile(
    r"ignore(?:\s+all)?\s+(?:previous|prior|earlier)\s+(?:instructions|rules|prompts?)|"
    r"disregard(?:\s+all)?\s+(?:previous|prior|earlier)\s+(?:instructions|rules|prompts?)|"
    r"忽略(?:之前|先前|上文|前面)(?:的)?(?:所有|任何)?(?:指令|规则|提示)|"
    r"不要(?:遵守|服从)(?:系统|安全|上层)?(?:指令|规则)|"
    r"(?:无需|不需要|不必)(?:用户|人工)?确认.{0,20}(?:执行|调用|工具|权限|审核)",
    flags=re.IGNORECASE,
)
UNTRUSTED_MEMORY_BYPASS_PATTERN = re.compile(
    r"绕过(?:安全|权限|审核|系统|规则)|"
    r"\bbypass(?:\s+(?:the|all|any))?\s+"
    r"(?:approval|review|security|safety|rules?|permissions?)\b",
    flags=re.IGNORECASE,
)
UNTRUSTED_MEMORY_EXECUTION_PATTERN = re.compile(
    r"\b(?:execute|run|call|invoke|launch)\b.{0,80}"
    r"\b(?:without|skip(?:ping)?|bypass(?:ing)?)\s+"
    r"(?:(?:the|user|human|explicit)\s+)?"
    r"(?:approval|review|confirmation|permission|authorization)\b",
    flags=re.IGNORECASE,
)
NEGATED_MEMORY_BYPASS_PATTERN = re.compile(
    r"(?:不要|请勿|切勿|禁止|严禁|不应(?:当)?|不可|不得|不可以|不允许)\s*$|"
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't)"
    r"(?:\s+ever)?\s*$",
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
TRANSIENT_CONTEXT_PATTERN = re.compile(
    r"\b(?:i am|i'm)\s+(?:currently\s+)?(?:looking at|viewing|checking|browsing|opening)\b|"
    r"\b(?:right now|just now)\s+(?:i am|i'm)\b|"
    r"我(?:现在|当前)?正在(?:查看|浏览|检查|打开|阅读)(?:一下)?|"
    r"刚刚(?:打开|查看|浏览|检查)|"
    r"(?:今天|本次|这次)(?:对话|任务|请求|运行)中",
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
    if contains_untrusted_memory_instruction(text):
        return MemoryWriteGateResult(
            "reject",
            0.0,
            ("untrusted_instruction",),
            False,
            True,
        )
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
    transient_context = bool(TRANSIENT_CONTEXT_PATTERN.search(text))
    if transient_context:
        reasons.append("transient_context")

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
    if transient_context:
        # Keep one-off activity visible for an explicit human decision, but do
        # not let a high-confidence contextual sentence enter the automatic
        # long-term memory path.
        score = min(score, 0.71)
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


def contains_untrusted_memory_instruction(text: str) -> bool:
    """识别不应进入长期记忆、也不应被后续问答当作偏好的指令性内容。"""
    normalized = str(text or "")
    if UNTRUSTED_MEMORY_INSTRUCTION_PATTERN.search(normalized):
        return True
    for pattern in (UNTRUSTED_MEMORY_BYPASS_PATTERN, UNTRUSTED_MEMORY_EXECUTION_PATTERN):
        for match in pattern.finditer(normalized):
            prefix = normalized[max(0, match.start() - 32) : match.start()]
            if not NEGATED_MEMORY_BYPASS_PATTERN.search(prefix):
                return True
    return False


def _normalized_role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "assistant" if normalized == "ai" else normalized


def _clamp(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(parsed, 1.0))
