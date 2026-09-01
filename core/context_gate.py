import re
from dataclasses import dataclass
from typing import Any

from core.answer_quality import content_terms, evaluate_evidence_gate
from core.query_expansion import query_requires_source_diversity, split_comparison_query


STRICT_NON_RETRIEVAL_PATTERN = re.compile(
    r"^(?:你好|您好|嗨|hello|hi|谢谢|感谢|辛苦了|再见|拜拜)[！!。,.，\s]*$",
    flags=re.IGNORECASE,
)
COMPOUND_QUERY_PATTERN = re.compile(
    r"对比|比较|异同|分别|同时|以及|并且|优缺点|风险与|目标与|方案与|"
    r"compare|versus|\bvs\b",
    flags=re.IGNORECASE,
)
UNTRUSTED_INSTRUCTION_PATTERN = re.compile(
    r"ignore (?:all |any )?(?:previous|prior) instructions|"
    r"reveal (?:the )?(?:system prompt|developer message)|"
    r"忽略(?:之前|以上|系统)指令|泄露(?:系统提示词|密钥|口令)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class RetrievalPlan:
    requires_retrieval: bool
    action: str
    query_kind: str
    reasons: tuple[str, ...]
    subqueries: tuple[str, ...]
    max_passes: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "requires_retrieval": self.requires_retrieval,
            "action": self.action,
            "query_kind": self.query_kind,
            "reasons": list(self.reasons),
            "subqueries": list(self.subqueries),
            "max_passes": self.max_passes,
        }


@dataclass(frozen=True)
class ContextGateResult:
    documents: tuple[str, ...]
    metadatas: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]
    decision: str
    reason: str
    budget_chars: int
    accepted_chars: int
    query_term_coverage: float
    source_count: int
    untrusted_instruction_count: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "context_admission_gate_v1",
            "decision": self.decision,
            "reason": self.reason,
            "accepted_count": len(self.evidence),
            "rejected_count": len(self.rejected),
            "budget_chars": self.budget_chars,
            "accepted_chars": self.accepted_chars,
            "budget_utilization": round(
                self.accepted_chars / self.budget_chars,
                6,
            ) if self.budget_chars else 0.0,
            "query_term_coverage": self.query_term_coverage,
            "source_count": self.source_count,
            "untrusted_instruction_count": self.untrusted_instruction_count,
            "rejected": list(self.rejected),
        }


def plan_retrieval(query: str) -> RetrievalPlan:
    normalized = " ".join(str(query or "").split())
    if not normalized:
        return RetrievalPlan(False, "skip", "empty", ("empty_query",), (), 0)
    if len(normalized) <= 24 and STRICT_NON_RETRIEVAL_PATTERN.fullmatch(normalized):
        return RetrievalPlan(False, "skip", "conversation", ("strict_social_turn",), (), 0)

    diverse = query_requires_source_diversity(normalized)
    subqueries = tuple(split_comparison_query(normalized))
    compound = diverse or bool(COMPOUND_QUERY_PATTERN.search(normalized))
    if compound:
        return RetrievalPlan(
            True,
            "decompose",
            "multi_part_document_query",
            ("multiple_evidence_targets", "corrective_pass_enabled"),
            subqueries,
            2,
        )
    return RetrievalPlan(
        True,
        "retrieve",
        "document_query",
        ("knowledge_evidence_required",),
        (),
        2,
    )


def non_retrieval_response(query: str) -> str:
    normalized = str(query or "").strip().lower()
    if any(marker in normalized for marker in ("谢谢", "感谢", "辛苦", "thank")):
        return "不客气。"
    if any(marker in normalized for marker in ("再见", "拜拜", "bye")):
        return "再见。"
    return "你好，请告诉我需要检索或分析的文档问题。"


def admit_context(
    query: str,
    documents: list[str],
    metadatas: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    budget_chars: int = 7600,
    max_evidence: int = 6,
    max_chars_per_evidence: int = 1800,
    minimum_score: float = 0.015,
) -> ContextGateResult:
    budget_chars = max(800, int(budget_chars))
    max_evidence = max(1, int(max_evidence))
    query_terms = content_terms(query)
    accepted_docs: list[str] = []
    accepted_metas: list[dict[str, Any]] = []
    accepted_evidence: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_terms: set[str] = set()
    fingerprints: list[set[str]] = []
    accepted_chars = 0
    untrusted_instruction_count = 0
    anchored_sources = _exact_section_sources(query, evidence, metadatas)
    scope_to_anchor = bool(anchored_sources) and not query_requires_source_diversity(query)

    item_count = max(len(documents), len(evidence))
    for index in range(item_count):
        item = dict(evidence[index]) if index < len(evidence) else {}
        metadata = dict(metadatas[index]) if index < len(metadatas) else {}
        document = str(documents[index] if index < len(documents) else "").strip()
        if not document:
            document = str(item.get("context") or item.get("snippet") or "").strip()
            if document:
                item["context_recovered_from_evidence"] = True
        score = _safe_float(item.get("score"))
        source = str(item.get("source_filename") or metadata.get("source_filename") or "未知文件")
        candidate_id = str(item.get("candidate_id") or metadata.get("child_id") or f"candidate-{index + 1}")

        if scope_to_anchor and source not in anchored_sources:
            rejected.append(_rejection(candidate_id, source, score, "exact_section_scope"))
            continue

        if not document:
            rejected.append(_rejection(candidate_id, source, score, "empty_context"))
            continue
        ocr_confidence = metadata.get("ocr_mean_confidence")
        if str(metadata.get("modality") or "").lower() == "image" and _safe_float(
            1.0 if ocr_confidence is None else ocr_confidence
        ) < 0.60:
            rejected.append(_rejection(candidate_id, source, score, "low_ocr_confidence"))
            continue
        if score < minimum_score:
            rejected.append(_rejection(candidate_id, source, score, "score_below_floor"))
            continue

        item_terms = content_terms(
            " ".join(
                [
                    source,
                    str(item.get("section") or ""),
                    str(item.get("snippet") or ""),
                    document[:1000],
                ]
            )
        )
        overlap = query_terms & item_terms
        query_coverage = len(overlap) / max(1, len(query_terms)) if query_terms else 1.0
        if index > 0 and score < 0.08 and query_terms and query_coverage <= 0.0:
            rejected.append(_rejection(candidate_id, source, score, "no_query_signal"))
            continue

        fingerprint = content_terms(document[:2400])
        if any(_jaccard(fingerprint, existing) >= 0.88 for existing in fingerprints):
            rejected.append(_rejection(candidate_id, source, score, "near_duplicate"))
            continue
        if len(accepted_evidence) >= max_evidence:
            rejected.append(_rejection(candidate_id, source, score, "evidence_limit"))
            continue

        remaining = budget_chars - accepted_chars
        if remaining < 240:
            rejected.append(_rejection(candidate_id, source, score, "context_budget_exhausted"))
            continue
        admitted = _compact_context(document, min(max_chars_per_evidence, remaining))
        if len(admitted) < 8:
            rejected.append(_rejection(candidate_id, source, score, "context_too_short"))
            continue

        if UNTRUSTED_INSTRUCTION_PATTERN.search(admitted):
            untrusted_instruction_count += 1
            metadata["contains_untrusted_instruction"] = True

        accepted_docs.append(admitted)
        accepted_metas.append(metadata)
        item["context"] = admitted
        item["id"] = f"E{len(accepted_evidence) + 1}"
        accepted_evidence.append(item)
        accepted_terms.update(item_terms)
        fingerprints.append(fingerprint)
        accepted_chars += len(admitted)

    accepted_sources = {
        str(item.get("source_filename") or "").strip()
        for item in accepted_evidence
        if str(item.get("source_filename") or "").strip()
    }
    term_coverage = (
        len(query_terms & accepted_terms) / len(query_terms)
        if query_terms
        else 1.0
    )
    evidence_gate = evaluate_evidence_gate(accepted_evidence, query=query)
    decision = evidence_gate.decision
    reason = evidence_gate.reason
    if decision == "accept" and query_requires_source_diversity(query) and len(accepted_sources) < 2:
        decision = "retry"
        reason = "multi-source query lacks source diversity"

    return ContextGateResult(
        documents=tuple(accepted_docs),
        metadatas=tuple(accepted_metas),
        evidence=tuple(accepted_evidence),
        rejected=tuple(rejected),
        decision=decision,
        reason=reason,
        budget_chars=budget_chars,
        accepted_chars=accepted_chars,
        query_term_coverage=round(term_coverage, 6),
        source_count=len(accepted_sources),
        untrusted_instruction_count=untrusted_instruction_count,
    )


def _rejection(candidate_id: str, source: str, score: float, reason: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "source_filename": source,
        "score": round(score, 6),
        "reason": reason,
    }


def _exact_section_sources(
    query: str,
    evidence: list[dict[str, Any]],
    metadatas: list[dict[str, Any]],
) -> set[str]:
    topics = [
        topic.strip()
        for topic in re.findall(r"[《\"“]([^》\"”]{2,80})[》\"”]", str(query or ""))
        if topic.strip()
    ]
    if not topics:
        return set()

    sources: set[str] = set()
    for index, item in enumerate(evidence):
        metadata = metadatas[index] if index < len(metadatas) else {}
        section = " ".join(
            [
                str(item.get("document_title") or ""),
                str(item.get("document_number") or ""),
                str(item.get("section") or ""),
                str(item.get("snippet") or ""),
                str(metadata.get("section") or ""),
                str(metadata.get("section_path") or ""),
                str(metadata.get("child_excerpt") or ""),
            ]
        )
        source = str(item.get("source_filename") or metadata.get("source_filename") or "").strip()
        normalized_section = re.sub(r"\s+", "", section)
        if source and _safe_float(item.get("score")) >= 0.12 and any(
            normalized_topic in normalized_section
            or (len(normalized_section) >= 5 and normalized_section in normalized_topic)
            for normalized_topic in (re.sub(r"\s+", "", topic) for topic in topics)
        ):
            sources.add(source)
    return sources


def _compact_context(text: str, max_chars: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    marker = " ... [context gated] ... "
    if max_chars <= len(marker) + 120:
        return normalized[:max_chars]
    available = max_chars - len(marker)
    head = int(available * 0.72)
    return f"{normalized[:head]}{marker}{normalized[-(available - head):]}"


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
