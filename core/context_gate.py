import math
import re
from dataclasses import dataclass
from typing import Any

from core.answer_quality import content_terms, evaluate_evidence_gate
from core.query_expansion import query_requires_source_diversity, split_comparison_query
from core.retrieval import source_diversity_key


STRICT_NON_RETRIEVAL_PATTERN = re.compile(
    r"^(?:你好|您好|嗨|hello|hi|谢谢|感谢|辛苦了|再见|拜拜)[！!。,.，\s]*$",
    flags=re.IGNORECASE,
)
COMPOUND_QUERY_PATTERN = re.compile(
    r"分别|同时|以及|并且|优缺点|风险与|目标与|方案与",
    flags=re.IGNORECASE,
)
UNTRUSTED_INSTRUCTION_PATTERN = re.compile(
    r"ignore (?:all |any )?(?:previous|prior) instructions|"
    r"reveal (?:the )?(?:system prompt|developer message)|"
    r"忽略(?:之前|以上|系统)指令|泄露(?:系统提示词|密钥|口令)",
    flags=re.IGNORECASE,
)
LOW_SCORE_QUERY_COVERAGE_FLOOR = 0.12
SUPPORT_SCORE_MIN_FLOOR = 0.06
SUPPORT_SCORE_RELATIVE_FLOOR = 0.20
_SUBQUERY_GENERIC_TERMS = frozenset({
    "核心",
    "证据",
    "关键",
    "比较",
    "对比",
    "分别",
    "各自",
    "方案",
    "项目",
    "文档",
    "资料",
})


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
            "task_supported_evidence": [
                {"id": item["id"], "source_filename": item.get("source_filename"),
                 "task_support": item["task_support"]}
                for item in self.evidence if item.get("admission_basis") == "workflow_task_focus"
            ],
            "rejected": list(self.rejected),
        }


def plan_retrieval(
    query: str,
    *,
    require_source_diversity: bool = False,
) -> RetrievalPlan:
    normalized = " ".join(str(query or "").split())
    if not normalized:
        return RetrievalPlan(False, "skip", "empty", ("empty_query",), (), 0)
    if len(normalized) <= 24 and STRICT_NON_RETRIEVAL_PATTERN.fullmatch(normalized):
        return RetrievalPlan(False, "skip", "conversation", ("strict_social_turn",), (), 0)

    diverse = bool(require_source_diversity) or query_requires_source_diversity(normalized)
    subqueries = tuple(split_comparison_query(normalized))
    compound = diverse or bool(COMPOUND_QUERY_PATTERN.search(normalized))
    if compound:
        reasons = ["multiple_evidence_targets", "corrective_pass_enabled"]
        if require_source_diversity:
            reasons.insert(0, "workflow_source_diversity")
        if not subqueries:
            reasons.append("subquery_generation_unavailable")
            return RetrievalPlan(
                True,
                "retrieve",
                "multi_source_query",
                tuple(reasons),
                (),
                2,
            )
        return RetrievalPlan(
            True,
            "decompose",
            "multi_part_document_query",
            tuple(reasons),
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


def evaluate_subquery_coverage(
    query: str,
    subqueries: tuple[str, ...] | list[str],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check that every planned comparison branch has relevant evidence.

    Source diversity alone is not decomposition coverage: two different files
    can still both describe only the first side of a comparison.  This gate is
    intentionally deterministic and bounded.  A branch needs a source/content
    identity match plus at least one non-generic focus term from the original
    query; when the query has no focus term, the identity match is sufficient.
    """
    normalized_subqueries = tuple(
        " ".join(str(item or "").split())
        for item in subqueries
        if " ".join(str(item or "").split())
    )
    if not normalized_subqueries:
        return {
            "required": [],
            "covered": [],
            "missing": [],
            "complete": True,
            "reason": "no_subqueries",
        }

    subjects = tuple(_subquery_subject(item) for item in normalized_subqueries)
    subject_terms = set()
    for subject in subjects:
        subject_terms.update(content_terms(subject))
    focus_query = str(query or "")
    for subject in subjects:
        if subject:
            focus_query = re.sub(re.escape(subject), " ", focus_query, flags=re.IGNORECASE)
    focus_query = re.sub(
        r"比较|对比|分别|各自|与|和|及|的|之间|方面|上|中|请|说明|概括|总结",
        " ",
        focus_query,
        flags=re.IGNORECASE,
    )
    focus_terms = {
        term
        for term in content_terms(focus_query)
        if term not in subject_terms
        and term not in _SUBQUERY_GENERIC_TERMS
        and not any(generic in term for generic in _SUBQUERY_GENERIC_TERMS)
    }

    covered: list[dict[str, Any]] = []
    missing: list[str] = []
    for subquery, subject in zip(normalized_subqueries, subjects):
        matches: list[str] = []
        for index, item in enumerate(evidence):
            evidence_item = dict(item or {})
            identity_text = " ".join(
                str(evidence_item.get(key) or "")
                for key in (
                    "source_filename",
                    "document_title",
                    "document_number",
                    "section",
                    "section_path",
                )
            )
            content_text = " ".join(
                str(evidence_item.get(key) or "")
                for key in (
                    "section",
                    "section_path",
                    "snippet",
                    "context",
                    "child_excerpt",
                )
            )
            if not _subquery_identity_match(subject, identity_text, content_text):
                continue
            content_focus = content_terms(content_text)
            if focus_terms and not (focus_terms & content_focus):
                continue
            evidence_id = str(
                evidence_item.get("id")
                or evidence_item.get("candidate_id")
                or f"candidate-{index + 1}"
            )
            matches.append(evidence_id)
        if matches:
            covered.append({"subquery": subquery, "evidence_ids": list(dict.fromkeys(matches))})
        else:
            missing.append(subquery)

    return {
        "required": list(normalized_subqueries),
        "covered": covered,
        "missing": missing,
        "complete": not missing,
        "reason": "all_subqueries_covered" if not missing else "subquery_evidence_missing",
    }


def _subquery_subject(subquery: str) -> str:
    subject = re.sub(
        r"\s*(?:核心证据|关键证据|直接证据|核心内容|关键内容|证据)\s*$",
        "",
        str(subquery or "").strip(),
        flags=re.IGNORECASE,
    ).strip(" ：:，,；;")
    return subject or str(subquery or "").strip()


def _subquery_identity_match(subject: str, identity_text: str, content_text: str) -> bool:
    compact_subject = re.sub(r"[\s\u3000]", "", str(subject or "")).casefold()
    if not compact_subject:
        return False
    compact_identity = re.sub(r"[\s\u3000]", "", str(identity_text or "")).casefold()
    compact_content = re.sub(r"[\s\u3000]", "", str(content_text or "")).casefold()
    if compact_subject in compact_identity or compact_subject in compact_content:
        return True
    subject_terms = {
        term
        for term in content_terms(subject)
        if term not in _SUBQUERY_GENERIC_TERMS
        and len(term) >= 2
    }
    if not subject_terms:
        return False
    return bool(subject_terms & content_terms(identity_text + " " + content_text))


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
    task_focuses: list[str] | tuple[str, ...] = (),
) -> ContextGateResult:
    budget_chars = max(800, int(budget_chars))
    max_evidence = max(1, int(max_evidence))
    query_terms = content_terms(query)
    accepted_docs: list[str] = []
    accepted_metas: list[dict[str, Any]] = []
    accepted_evidence: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_terms: set[str] = set()
    fingerprints: dict[str, list[tuple[set[str], str]]] = {}
    accepted_chars = 0
    untrusted_instruction_count = 0
    support_scores = [
        _optional_score(item.get("support_score"))
        for item in evidence
    ]
    lead_support_score = max(
        (score for score in support_scores if score is not None),
        default=None,
    )
    support_floor = (
        max(SUPPORT_SCORE_MIN_FLOOR, lead_support_score * SUPPORT_SCORE_RELATIVE_FLOOR)
        if lead_support_score is not None
        else None
    )
    anchored_sources = _exact_section_sources(query, evidence, metadatas)
    scope_to_anchor = bool(anchored_sources) and not query_requires_source_diversity(query)

    item_count = max(len(documents), len(evidence))
    for index in range(item_count):
        item = dict(evidence[index]) if index < len(evidence) else {}
        item.pop("admission_basis", None)
        item.pop("task_support", None)
        metadata = dict(metadatas[index]) if index < len(metadatas) else {}
        document = str(documents[index] if index < len(documents) else "").strip("\r\n")
        if not document.strip():
            document = str(item.get("context") or item.get("snippet") or "").strip("\r\n")
            if document.strip():
                item["context_recovered_from_evidence"] = True
        score = _safe_float(item.get("score"))
        source = str(item.get("source_filename") or metadata.get("source_filename") or "未知文件")
        candidate_id = str(item.get("candidate_id") or metadata.get("child_id") or f"candidate-{index + 1}")
        source_metadata = _merge_source_metadata(metadata, item)
        source_scope_key = source_diversity_key(source_metadata, candidate_id)

        derived_marker = item.get("derived_index")
        if derived_marker is True or str(derived_marker or "").strip().lower() in {"1", "true", "yes"}:
            rejected.append(_rejection(candidate_id, source, score, "derived_index_not_raw_evidence"))
            continue

        if scope_to_anchor and source_scope_key not in anchored_sources:
            rejected.append(_rejection(candidate_id, source, score, "exact_section_scope"))
            continue

        if not document.strip():
            rejected.append(_rejection(candidate_id, source, score, "empty_context"))
            continue
        ocr_confidence = metadata.get("ocr_mean_confidence")
        if str(metadata.get("modality") or "").lower() == "image" and _safe_float(
            1.0 if ocr_confidence is None else ocr_confidence
        ) < 0.60:
            rejected.append(_rejection(candidate_id, source, score, "low_ocr_confidence"))
            continue
        # Only the bound workflow supplies these focuses, never memory or
        # model-generated retrieval hints. This admits task conditions, not
        # proof of a conclusion; the original-query evidence gate is unchanged.
        remaining = budget_chars - accepted_chars
        structured = is_structured_source(source_metadata)
        admitted = compact_source_context(document, min(max_chars_per_evidence, remaining), structured=structured)
        task_support = _task_support(admitted, task_focuses) if score > 0 else {}
        task_score = float(task_support.get("score") or 0.0)
        if max(score, task_score) < minimum_score:
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
        if (
            index > 0
            and score < 0.08
            and query_terms
            and query_coverage < LOW_SCORE_QUERY_COVERAGE_FLOOR
            and task_score < LOW_SCORE_QUERY_COVERAGE_FLOOR
        ):
            rejected.append(_rejection(candidate_id, source, score, "no_query_signal"))
            continue

        support_score = _optional_score(item.get("support_score"))
        if (
            index > 0
            and support_floor is not None
            and support_score is not None
            and max(support_score, task_score) < support_floor
        ):
            rejected.append(_rejection(candidate_id, source, score, "weak_query_support"))
            continue

        fingerprint = content_terms(document[:2400])
        normalized_document = " ".join(document[:2400].split())
        if any(
            _jaccard(fingerprint, existing) >= 0.88
            and (not structured or normalized_document == existing_text)
            for existing, existing_text in fingerprints.get(source_scope_key, [])
        ):
            rejected.append(_rejection(candidate_id, source, score, "near_duplicate"))
            continue
        if len(accepted_evidence) >= max_evidence:
            rejected.append(_rejection(candidate_id, source, score, "evidence_limit"))
            continue

        if remaining < 240:
            rejected.append(_rejection(candidate_id, source, score, "context_budget_exhausted"))
            continue
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
        if task_support:
            item["admission_basis"] = "workflow_task_focus"
            item["task_support"] = task_support
        accepted_evidence.append(item)
        accepted_terms.update(item_terms)
        fingerprints.setdefault(source_scope_key, []).append((fingerprint, normalized_document))
        accepted_chars += len(admitted)

    accepted_sources = set()
    for item, metadata in zip(accepted_evidence, accepted_metas):
        source_metadata = _merge_source_metadata(metadata, item)
        source_key_value = source_diversity_key(source_metadata)
        if source_key_value:
            accepted_sources.add(source_key_value)
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


def _task_support(document: str, focuses: list[str] | tuple[str, ...]) -> dict[str, Any]:
    body_terms = content_terms(document[:1800])
    best: dict[str, Any] = {}
    for focus in focuses[:3]:
        terms = content_terms(str(focus)[:320])
        matched = sorted(terms & body_terms)
        if len(matched) < 2:
            continue
        score = len(matched) / max(1, len(terms))
        if score >= LOW_SCORE_QUERY_COVERAGE_FLOOR and score > float(best.get("score") or 0):
            best = {"focus": str(focus)[:320], "score": round(score, 6), "matched_terms": matched}
    return best


def _rejection(candidate_id: str, source: str, score: float, reason: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "source_filename": source,
        "score": round(score, 6),
        "reason": reason,
    }


def _merge_source_metadata(
    metadata: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(metadata or {})
    merged.update({
        key: value
        for key, value in (evidence or {}).items()
        if value is not None and str(value).strip()
    })
    return merged


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
        source_metadata = _merge_source_metadata(metadata, item)
        source_scope_key = source_diversity_key(source_metadata)
        normalized_section = re.sub(r"\s+", "", section)
        if source_scope_key and _safe_float(item.get("score")) >= 0.12 and any(
            normalized_topic in normalized_section
            or (len(normalized_section) >= 5 and normalized_section in normalized_topic)
            for normalized_topic in (re.sub(r"\s+", "", topic) for topic in topics)
        ):
            sources.add(source_scope_key)
    return sources


def is_structured_source(metadata: dict[str, Any]) -> bool:
    return (
        str(metadata.get("content_kind") or "").lower() in {"table", "code", "config"}
        or str(metadata.get("source_filename") or "").lower().endswith((".yaml", ".yml", ".json"))
    )


def compact_source_context(text: str, max_chars: int, *, structured: bool = False) -> str:
    """Keep source syntax; a truncated structured excerpt contains complete prefix lines only."""
    normalized = str(text or "").strip("\r\n")
    if len(normalized) <= max_chars:
        return normalized
    if structured:
        marker = "\n…（原文截断，请查看来源）"
        prefix, boundary, _ = normalized[:max(0, max_chars - len(marker))].rpartition("\n")
        return prefix.rstrip("\r\n") + marker if boundary else ""
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
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))
