import re
from dataclasses import dataclass
from typing import Any


CITATION_PATTERN = re.compile(r"\[E(\d+)\]")
DEFAULT_REJECT_SCORE = 0.015
DEFAULT_RETRY_SCORE = 0.08


@dataclass(frozen=True)
class EvidenceGateResult:
    allowed: bool
    reason: str
    evidence_count: int
    max_score: float
    decision: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "max_score": self.max_score,
            "decision": self.decision,
        }


def evaluate_evidence_gate(
    evidence: list[dict[str, Any]],
    query: str | None = None,
    min_evidence_count: int = 1,
    reject_score: float = DEFAULT_REJECT_SCORE,
    retry_score: float = DEFAULT_RETRY_SCORE,
) -> EvidenceGateResult:
    if len(evidence) < min_evidence_count:
        return EvidenceGateResult(False, "no sufficient evidence", len(evidence), 0.0, "reject")

    scores = [_safe_float(item.get("score")) for item in evidence]
    max_score = max(scores or [0.0])
    if max_score < reject_score:
        return EvidenceGateResult(False, "evidence confidence below threshold", len(evidence), max_score, "reject")

    answers_query = evidence_answers_query(query, evidence) if query else None
    if max_score < retry_score:
        if (
            answers_query
            and has_corroborating_evidence(
                evidence,
                minimum_score=max(reject_score, retry_score * 0.65),
            )
        ):
            return EvidenceGateResult(
                True,
                "evidence accepted by corroboration",
                len(evidence),
                max_score,
                "accept",
            )
        return EvidenceGateResult(False, "evidence confidence requires retry", len(evidence), max_score, "retry")

    if query and not answers_query:
        return EvidenceGateResult(False, "evidence does not answer query", len(evidence), max_score, "retry")

    return EvidenceGateResult(True, "evidence accepted", len(evidence), max_score, "accept")


def has_corroborating_evidence(
    evidence: list[dict[str, Any]],
    minimum_score: float,
) -> bool:
    credible = [
        item
        for item in evidence
        if _safe_float(item.get("score")) >= minimum_score
    ]
    if len(credible) < 2:
        return False
    sources = {
        str(item.get("source_filename") or "").strip()
        for item in credible
        if str(item.get("source_filename") or "").strip()
    }
    return len(sources) >= 2


def refusal_answer(query: str, gate: EvidenceGateResult) -> str:
    _ = query
    return (
        "根据本地存储的知识库，未找到足够可靠的相关证据，"
        f"因此不会生成可能误导的回答。原因：{gate.reason}。"
    )


def citation_coverage(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    cited_ids = {f"E{match}" for match in CITATION_PATTERN.findall(answer or "")}
    available_ids = {str(item.get("id")) for item in evidence if item.get("id")}
    used = sorted(cited_ids & available_ids)
    unsupported = sorted(cited_ids - available_ids)
    return {
        "available_count": len(available_ids),
        "cited_count": len(used),
        "coverage": round(len(used) / len(available_ids), 6) if available_ids else 0.0,
        "used": used,
        "unsupported": unsupported,
    }


def validate_answer_citations(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = citation_coverage(answer, evidence)
    if not evidence:
        return {
            "valid": True,
            "decision": "accept",
            "reason": "no evidence supplied",
            **coverage,
        }

    if coverage["unsupported"]:
        return {
            "valid": False,
            "decision": "retry",
            "reason": "answer cited unsupported evidence",
            **coverage,
        }

    if coverage["cited_count"] <= 0:
        return {
            "valid": False,
            "decision": "retry",
            "reason": "answer missing evidence citation",
            **coverage,
        }

    return {
        "valid": True,
        "decision": "accept",
        "reason": "answer citations validated",
        **coverage,
    }


def evidence_answers_query(query: str, evidence: list[dict[str, Any]]) -> bool:
    query_terms = content_terms(query)
    if not query_terms:
        return True

    evidence_terms: set[str] = set()
    for item in evidence[:3]:
        evidence_terms.update(
            content_terms(
                " ".join(
                    str(item.get(key) or "")
                    for key in ("source_filename", "section", "snippet")
                )
            )
        )

    if not evidence_terms:
        return False

    overlap = query_terms & evidence_terms
    coverage = len(overlap) / max(len(query_terms), 1)
    if is_definition_query(query) and any(is_strong_entity_term(term) for term in overlap):
        return True
    return len(overlap) >= 2 or coverage >= 0.34


def content_terms(text: str) -> set[str]:
    normalized = (text or "").lower()
    terms = set()
    for term in re.findall(r"[A-Za-z0-9_]{2,}", normalized):
        if is_stop_term(term):
            continue
        terms.add(term)

    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        max_size = min(4, len(chunk))
        for size in range(2, max_size + 1):
            for index in range(0, len(chunk) - size + 1):
                term = chunk[index : index + size]
                if not is_stop_term(term):
                    terms.add(term)
    return terms


def is_stop_term(term: str) -> bool:
    return term in {"哪些", "什么", "如何", "为什么", "根据", "资料", "文档", "主要", "系统", "企业"}


def is_definition_query(query: str) -> bool:
    return any(marker in (query or "") for marker in ("是什么", "定义", "机制", "原理", "概念"))


def is_strong_entity_term(term: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9_]", term)) and len(term) >= 3


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
