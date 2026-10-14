import re
import math
from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Decimal, DecimalException, Inexact, localcontext
from typing import Any

from core.retrieval import classify_query_intent, source_diversity_key


CITATION_PATTERN = re.compile(r"\[((?:E|V|A|M)\d+)\]")
INVALID_ANSWER_FALLBACK_NOTICE = "自动回答未通过证据或当前模式的输出校验，本轮已改为证据核对。"
INCOMPLETE_ANSWER_FALLBACK_NOTICE = "自动回答未满足完整性或证据要求，本轮已改为证据核对。"
CLAIM_BULLET_PATTERN = re.compile(r"^(?:[-*+] |\d+[.)、．]?\s+)")
DEFAULT_REJECT_SCORE = 0.015
DEFAULT_RETRY_SCORE = 0.08
DEFAULT_VISUAL_REJECT_CONFIDENCE = 0.60
CITATION_SUPPORT_MIN_OVERLAP = 2
CITATION_SUPPORT_MIN_COVERAGE = 0.12
_FACT_UNIT_PATTERN = (
    r"(?:%|％|毫秒|秒钟|秒|分钟|小时|天|字节|"
    r"KiB|MiB|GiB|TiB|"
    r"msec|milliseconds?|ms|seconds?|sec|s|minutes?|min|hours?|hrs?|hr|h|"
    r"bytes?|b|[A-Za-z]+)"
)
_FACT_NUMBER_PATTERN = (
    # A range/version hyphen is not a unary sign; explicit signs and
    # scientific exponents remain part of the numeric fact.
    r"(?:(?<![A-Za-z0-9_.])[+\-−])?(?:\d+(?:[.,]\d+)?|\.\d+)(?:[eE][+\-−]?\d+)?"
)
FACT_TOKEN_PATTERN = re.compile(
    rf"{_FACT_NUMBER_PATTERN}(?:\s*{_FACT_UNIT_PATTERN})?",
    flags=re.IGNORECASE,
)
_FACT_TIME_UNITS = {
    "毫秒": 1.0,
    "msec": 1.0,
    "millisecond": 1.0,
    "milliseconds": 1.0,
    "ms": 1.0,
    "秒": 1000.0,
    "秒钟": 1000.0,
    "s": 1000.0,
    "sec": 1000.0,
    "second": 1000.0,
    "seconds": 1000.0,
    "分钟": 60_000.0,
    "min": 60_000.0,
    "minute": 60_000.0,
    "minutes": 60_000.0,
    "小时": 3_600_000.0,
    "h": 3_600_000.0,
    "hr": 3_600_000.0,
    "hrs": 3_600_000.0,
    "hour": 3_600_000.0,
    "hours": 3_600_000.0,
    "天": 86_400_000.0,
}
_FACT_STORAGE_UNITS = {
    "b": 1.0,
    "byte": 1.0,
    "bytes": 1.0,
    "字节": 1.0,
    "kib": 1024.0,
    "mib": 1024.0**2,
    "gib": 1024.0**3,
    "tib": 1024.0**4,
}
EVIDENCE_QUERY_CHECK_LIMIT = 6
_QUERY_GENERIC_TERMS = frozenset({
    "请",
    "根据",
    "当前",
    "本地",
    "资料",
    "文档",
    "文件",
    "报告",
    "给出",
    "列出",
    "找出",
    "说明",
    "概括",
    "总结",
    "主要",
    "哪些",
    "什么",
    "如何",
    "怎么",
    "是否",
    "这些",
    "相关",
    "里面",
    "其中",
    "中的",
    "建议",
})
_QUERY_HARD_TOKEN_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_.:/-]{1,}|\b\d+(?:[.,]\d+)?\b"
)
_QUERY_HARD_TOKEN_STOPWORDS = frozenset({
    "a",
    "an",
    "and",
    "are",
    "can",
    "does",
    "for",
    "from",
    "give",
    "how",
    "is",
    "list",
    "local",
    "please",
    "the",
    "these",
    "this",
    "those",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
})
_CITATION_BOILERPLATE_EXACT = {
    "answer",
    "答案",
    "回答引用正确",
    "回答引用错误",
    "引用正确",
    "引用错误",
    "根据证据",
    "依据证据",
}
_CITATION_BOILERPLATE_SUFFIXES = ("找到证据", "检索到证据")
_VISUAL_EVIDENCE_MODALITIES = frozenset({
    "image",
    "image_region",
    "ocr_region",
    "ui_selection",
    "ui_automation",
})
_VISUAL_CLAIM_MARKERS = (
    "根据本轮截图",
    "根据截图",
    "截图中",
    "截图里",
    "截图显示",
    "视觉识别",
    "当前画面",
    "当前屏幕",
    "屏幕上",
    "画面中",
    "当前授权区域",
    "当前选区",
    "图表中",
    "图表显示",
    "图片中",
    "图像中",
    "界面显示",
)


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
    if query and not answers_query:
        query_intent = classify_query_intent(query)
        if query_intent.prefers_visual_asset or query_intent.explicit_source:
            quoted_topics = [
                item.strip()
                for item in re.findall(r"[“\"]([^”\"]+)[”\"]", query)
                if item.strip()
            ]
            title_hints = quoted_topics[:1]
            detail_hints = quoted_topics[1:]
            for item in evidence:
                source_text = " ".join(
                    str(item.get(key) or "")
                    for key in ("source_filename", "document_title", "section_path")
                )
                content_text = " ".join(
                    str(item.get(key) or "")
                    for key in ("section", "snippet", "context")
                )
                title_match = any(hint in source_text for hint in title_hints)
                if detail_hints and title_match and _detail_topics_match_content(detail_hints, content_text):
                    answers_query = True
                    break
                if not detail_hints and title_match:
                    answers_query = True
                    break
    if query and not answers_query and max_score >= reject_score:
        return EvidenceGateResult(
            False,
            "evidence does not answer query",
            len(evidence),
            max_score,
            "retry",
        )
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
        if answers_query and not query_requires_corroboration(query):
            return EvidenceGateResult(
                True,
                "evidence accepted by direct answer",
                len(evidence),
                max_score,
                "accept",
            )
        return EvidenceGateResult(False, "evidence confidence requires retry", len(evidence), max_score, "retry")

    if query and not answers_query:
        return EvidenceGateResult(False, "evidence does not answer query", len(evidence), max_score, "retry")

    return EvidenceGateResult(True, "evidence accepted", len(evidence), max_score, "accept")


def evaluate_visual_evidence_gate(
    evidence: list[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_VISUAL_REJECT_CONFIDENCE,
) -> EvidenceGateResult:
    """Validate user-authorized visual perception without document semantics.

    OCR/VLM text is useful for describing a current screen, but it is not a
    project document and should not be forced through the document query-term
    matcher.  Confidence remains the hard boundary for this separate route.
    """
    visual = [
        item
        for item in evidence
        if str(item.get("modality") or "").lower() == "image"
        and str(item.get("context") or item.get("snippet") or "").strip()
    ]
    if not visual:
        return EvidenceGateResult(False, "no visual evidence supplied", 0, 0.0, "reject")
    threshold = max(0.0, min(float(min_confidence), 1.0))
    max_score = max((_safe_float(item.get("confidence", item.get("score"))) for item in visual), default=0.0)
    if max_score < threshold:
        return EvidenceGateResult(
            False,
            "visual evidence confidence below threshold",
            len(visual),
            max_score,
            "reject",
        )
    return EvidenceGateResult(True, "visual evidence accepted", len(visual), max_score, "accept")


def evaluate_audio_evidence_gate(
    evidence: list[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_VISUAL_REJECT_CONFIDENCE,
) -> EvidenceGateResult:
    """Validate an explicitly supplied transcript for the meeting workflow."""
    audio = [
        item
        for item in evidence
        if str(item.get("modality") or "").lower() == "audio"
        and str(item.get("context") or item.get("snippet") or "").strip()
        and item.get("explicit") is True
    ]
    if not audio:
        return EvidenceGateResult(False, "no explicitly confirmed audio transcript supplied", 0, 0.0, "reject")
    threshold = max(0.0, min(float(min_confidence), 1.0))
    max_score = max((_safe_float(item.get("confidence", item.get("score"))) for item in audio), default=0.0)
    if max_score < threshold:
        return EvidenceGateResult(
            False,
            "audio transcript confidence below threshold",
            len(audio),
            max_score,
            "reject",
        )
    return EvidenceGateResult(True, "audio transcript accepted", len(audio), max_score, "accept")


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
        source_diversity_key(item)
        for item in credible
        if source_diversity_key(item)
    }
    return len(sources) >= 2


def refusal_answer(query: str, gate: EvidenceGateResult) -> str:
    _ = query
    return (
        "根据本地存储的知识库，未找到足够可靠的相关证据，"
        f"因此不会生成可能误导的回答。原因：{gate.reason}。"
    )


def citation_coverage(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    cited_ids = set(CITATION_PATTERN.findall(answer or ""))
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


def _cited_source_keys_for_text(text: str, evidence: list[dict[str, Any]]) -> set[str]:
    """Return logical source identities behind citations in one text span.

    GraphRAG evidence may carry several original document identities on one
    derived record.  Count those original identities instead of counting the
    community report as a single source.  Ordinary evidence falls back to the
    same scoped source key used by retrieval diagnostics.
    """
    evidence_by_id = {
        str(item.get("id")): item
        for item in evidence
        if item.get("id")
    }
    source_keys: set[str] = set()
    for evidence_id in dict.fromkeys(CITATION_PATTERN.findall(text or "")):
        if not evidence_id.startswith("E"):
            continue
        item = evidence_by_id.get(evidence_id)
        if not item:
            continue
        raw_document_ids = item.get("source_document_ids")
        if isinstance(raw_document_ids, str):
            raw_document_ids = [raw_document_ids]
        document_ids = {
            str(value).strip()
            for value in (raw_document_ids or [])
            if str(value).strip()
        }
        if document_ids:
            source_keys.update(f"document:{value}" for value in document_ids)
            continue
        source_key = source_diversity_key(
            item,
            str(item.get("source_filename") or evidence_id),
        )
        if source_key:
            source_keys.add(f"source:{source_key}")
    return source_keys


def _cited_source_keys(answer: str, evidence: list[dict[str, Any]]) -> set[str]:
    return _cited_source_keys_for_text(answer, evidence)


def citation_source_count(answer: str, evidence: list[dict[str, Any]]) -> int:
    """Count distinct logical source documents cited by an answer."""
    return len(_cited_source_keys(answer, evidence))


def validate_answer_citations(
    answer: str,
    evidence: list[dict[str, Any]],
    *,
    semantic: bool = False,
    required_source_count: int = 0,
    require_source_count_per_claim: bool = False,
) -> dict[str, Any]:
    try:
        required_source_count = max(0, int(required_source_count))
    except (TypeError, ValueError):
        required_source_count = 0
    coverage = citation_coverage(answer, evidence)
    if not evidence:
        if required_source_count > 0:
            return {
                "valid": False,
                "decision": "retry",
                "reason": "answer citation source diversity insufficient",
                "cited_source_count": 0,
                "required_source_count": required_source_count,
                **coverage,
            }
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

    visual_namespace = _validate_visual_citation_namespaces(answer, evidence)
    if not visual_namespace["valid"]:
        return {
            "valid": False,
            "decision": "retry",
            "reason": "visual citation does not match visual evidence",
            "visual_namespace": visual_namespace,
            **coverage,
        }

    uncited_claims = [
        claim for claim in answer_claim_units(answer)
        if not CITATION_PATTERN.search(claim) and not _is_non_factual_citation_claim(claim)
    ]
    if uncited_claims:
        return {
            "valid": False,
            "decision": "retry",
            "reason": "answer claim missing evidence citation",
            "uncited_claim_count": len(uncited_claims),
            **coverage,
        }

    semantic_report = _validate_claim_support(answer, evidence) if semantic else None
    if semantic_report and not semantic_report["valid"]:
        return {
            "valid": False,
            "decision": "retry",
            "reason": "citation does not support answer claim",
            "semantic_support": semantic_report,
            **coverage,
        }

    cited_source_count = None
    if required_source_count > 0:
        cited_source_count = citation_source_count(answer, evidence)
        if cited_source_count < required_source_count:
            return {
                "valid": False,
                "decision": "retry",
                "reason": "answer citation source diversity insufficient",
                "cited_source_count": cited_source_count,
                "required_source_count": required_source_count,
                **coverage,
            }
        if require_source_count_per_claim:
            insufficient_claims = []
            for index, claim in enumerate(answer_claim_units(answer), start=1):
                if _is_non_factual_citation_claim(claim):
                    continue
                claim_source_count = len(_cited_source_keys_for_text(claim, evidence))
                if claim_source_count < required_source_count:
                    insufficient_claims.append({
                        "claim_index": index,
                        "cited_source_count": claim_source_count,
                        "required_source_count": required_source_count,
                    })
            if insufficient_claims:
                return {
                    "valid": False,
                    "decision": "retry",
                    "reason": "answer claim source diversity insufficient",
                    "cited_source_count": cited_source_count,
                    "required_source_count": required_source_count,
                    "insufficient_claim_count": len(insufficient_claims),
                    "insufficient_claims": insufficient_claims[:8],
                    **coverage,
                }

    return {
        "valid": True,
        "decision": "accept",
        "reason": "answer citations validated",
        "uncited_claim_count": 0,
        **({"semantic_support": semantic_report} if semantic_report else {}),
        **(
            {
                "cited_source_count": cited_source_count,
                "required_source_count": required_source_count,
            }
            if required_source_count > 0
            else {}
        ),
        **coverage,
    }


def _validate_visual_citation_namespaces(
    answer: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep visual claims and the ``V#`` namespace bound to visual records.

    A citation prefix is part of the evidence contract, not presentation
    decoration.  This guard prevents a generated phrase such as
    “根据本轮截图/视觉识别” from being backed only by an ``E#`` document
    record, and rejects ``V#`` records that lost their modality metadata.
    """
    evidence_by_id = {
        str(item.get("id")): item
        for item in evidence
        if item.get("id")
    }
    checked_claims: list[dict[str, Any]] = []
    invalid_claims: list[dict[str, Any]] = []
    for claim in answer_claim_units(answer):
        cited_ids = list(dict.fromkeys(CITATION_PATTERN.findall(claim)))
        if not cited_ids:
            continue
        claim_text = CITATION_PATTERN.sub("", claim).strip()
        visual_ids = [item_id for item_id in cited_ids if item_id.startswith("V")]
        invalid_visual_ids = [
            item_id
            for item_id in visual_ids
            if not _is_visual_evidence_item(evidence_by_id.get(item_id))
        ]
        issues: list[str] = []
        if _contains_visual_claim_marker(claim_text) and not visual_ids:
            issues.append("missing_visual_evidence")
        if invalid_visual_ids:
            issues.append("invalid_visual_evidence")
        record = {
            "claim": claim_text[:240],
            "cited_ids": cited_ids,
            "visual_citations": visual_ids,
            "invalid_visual_citations": invalid_visual_ids,
            "issues": issues,
            "status": "invalid" if issues else "valid",
        }
        checked_claims.append(record)
        if issues:
            invalid_claims.append(record)
    return {
        "valid": not invalid_claims,
        "checked_claim_count": len(checked_claims),
        "invalid_claim_count": len(invalid_claims),
        "invalid_claims": invalid_claims[:8],
    }


def _contains_visual_claim_marker(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    return any(re.sub(r"\s+", "", marker) in normalized for marker in _VISUAL_CLAIM_MARKERS)


def _is_visual_evidence_item(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    modality = str(item.get("modality") or "").strip().lower()
    return modality in _VISUAL_EVIDENCE_MODALITIES


def _validate_claim_support(
    answer: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run a bounded, explainable claim-to-citation support check.

    This is deliberately lexical rather than an entailment model.  It catches
    unsupported numbers and obviously unrelated cited claims without adding a
    second model call or pretending to establish truth beyond the local
    evidence corpus.
    """
    evidence_by_id = {
        str(item.get("id")): item
        for item in evidence
        if item.get("id")
    }
    claims: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for claim in answer_claim_units(answer):
        cited_ids = list(dict.fromkeys(CITATION_PATTERN.findall(claim)))
        if not cited_ids:
            continue
        claim_text = CITATION_PATTERN.sub("", claim).strip()
        claim_text = re.sub(
            r"^\s*(?:[-*+]\s+|\d+[.)、．]?\s+)",
            "",
            claim_text,
        ).strip()
        normalized_claim = re.sub(r"\s+", "", claim_text)
        if _is_non_factual_citation_claim(claim_text):
            claims.append({
                "claim": claim_text[:240],
                "cited_ids": cited_ids,
                "status": "locator_or_scaffold_exempt",
            })
            continue
        if (
            normalized_claim in _CITATION_BOILERPLATE_EXACT
            or any(normalized_claim.endswith(marker) for marker in _CITATION_BOILERPLATE_SUFFIXES)
        ):
            claims.append({
                "claim": claim_text[:240],
                "cited_ids": cited_ids,
                "status": "boilerplate_exempt",
            })
            continue

        cited_items = [evidence_by_id[item_id] for item_id in cited_ids if item_id in evidence_by_id]
        support_text = " ".join(
            str(item.get(key) or "")
            for item in cited_items
            for key in (
                "section",
                "section_path",
                "snippet",
                "context",
                "visual_asset_captions",
            )
        )
        claim_terms = content_terms(claim_text)
        support_terms = content_terms(support_text)
        overlap = claim_terms & support_terms
        coverage = len(overlap) / max(len(claim_terms), 1)
        claim_fact_tokens = _fact_tokens(_strip_location_tokens(claim_text))
        support_fact_tokens = _fact_tokens(support_text)
        missing_fact_tokens = sorted(claim_fact_tokens - support_fact_tokens)
        missing_scoped_topics = [
            topic
            for topic in _scoped_quoted_topics(claim_text)
            if _compact_text(topic) not in _compact_text(support_text)
        ]
        supported = bool(cited_items) and not missing_fact_tokens and (
            len(overlap) >= CITATION_SUPPORT_MIN_OVERLAP
            or coverage >= CITATION_SUPPORT_MIN_COVERAGE
        ) and not missing_scoped_topics
        claims.append({
            "claim": claim_text[:240],
            "cited_ids": cited_ids,
            "shared_terms": sorted(overlap)[:16],
            "claim_term_count": len(claim_terms),
            "coverage": round(coverage, 6),
            "missing_fact_tokens": missing_fact_tokens,
            "missing_scoped_topics": missing_scoped_topics,
            "status": "supported" if supported else "unsupported",
        })
        if not supported:
            unsupported.append(claim_text[:240])

    return {
        "valid": not unsupported,
        "checked_claim_count": len(claims),
        "unsupported_claim_count": len(unsupported),
        "unsupported_claims": unsupported[:8],
        "claims": claims[:16],
    }


def _scoped_quoted_topics(text: str) -> list[str]:
    """Return quoted topics explicitly scoped by a claim such as 关于“章节”。"""
    return list(dict.fromkeys(
        topic.strip()
        for topic in re.findall(
            r"(?:关于|围绕|针对|有关)\s*[“\"]([^”\"]+)[”\"]",
            text or "",
        )
        if len(_compact_text(topic)) >= 3
    ))


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _is_non_factual_citation_claim(text: str) -> bool:
    """Recognize location scaffolding and the exact missing-evidence marker."""
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized or normalized in {
        "证据", "证据：", "待确认",
        INVALID_ANSWER_FALLBACK_NOTICE, INCOMPLETE_ANSWER_FALLBACK_NOTICE,
    }:
        return True
    if normalized.startswith(("来源文档：", "证据章节：", "视觉证据范围：")):
        return True
    if normalized.startswith("根据本轮截图/视觉识别，证据位于") and any(
        marker in normalized for marker in ("文档标题", "章节", "切片", "页")
    ):
        return True
    return normalized.startswith("关于") and normalized.endswith(("直接证据表明：", "直接证据表明"))


def _strip_location_tokens(text: str) -> str:
    """Remove page/chunk/reference markers before numeric fact checking."""
    cleaned = str(text or "")
    cleaned = re.sub(r"第\s*\[\s*\d+\s*\]\s*(?:页|切片)", "", cleaned)
    cleaned = re.sub(r"第\s*\d+\s*(?:页|切片)", "", cleaned)
    cleaned = re.sub(r"\[\s*\d+\s*\](?=\s*(?:页|切片|私有文献详情|证据|原文))", "", cleaned)
    return re.sub(r"([“\"])\s*\d+[.)、．]\s*", r"\1", cleaned)


def _fact_tokens(text: str) -> set[str]:
    return {
        _normalize_fact_token(token)
        for token in FACT_TOKEN_PATTERN.findall(text or "")
        if token.strip()
    }


def _normalize_fact_token(token: str) -> str:
    """Normalize only unambiguous numeric and unit spellings for citation support."""
    normalized = str(token or "").replace(" ", "").replace("−", "-").lower()
    match = re.fullmatch(rf"({_FACT_NUMBER_PATTERN})(.*)", normalized)
    if not match:
        return normalized
    unit = match.group(2)
    if unit in {"%", "％"}:
        multiplier, prefix = Decimal("0.01"), ""
    elif unit in _FACT_TIME_UNITS:
        multiplier, prefix = Decimal(str(_FACT_TIME_UNITS[unit])), "time_ms:"
    elif unit in _FACT_STORAGE_UNITS:
        multiplier, prefix = Decimal(str(_FACT_STORAGE_UNITS[unit])), "bytes:"
    else:
        multiplier, prefix = Decimal(1), f"unit:{unit}:" if unit else ""
    try:
        with localcontext() as context:
            value = Decimal(match.group(1).replace(",", "."))
            if not value.is_finite():
                return normalized
            # Multiplying finite decimal coefficients needs at most the sum
            # of their digit counts; never round citation facts to display precision.
            context.prec = len(value.as_tuple().digits) + len(multiplier.as_tuple().digits)
            context.Emax, context.Emin, context.clamp = MAX_EMAX, MIN_EMIN, 0
            context.traps[Inexact] = True
            value = (value * multiplier).normalize()
            return f"{prefix}{'0' if value.is_zero() else value}"
    except DecimalException:
        # Keep unsupported exponent literals distinct, not float inf/zero.
        return normalized


def answer_claim_units(answer: str) -> list[str]:
    """将 Markdown 的每个要点或段落视作一条需被证据支持的结论。"""
    units: list[str] = []
    for block in re.split(r"\n\s*\n", answer or ""):
        prose_lines: list[str] = []
        bullet_lines: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("```"):
                continue
            if CLAIM_BULLET_PATTERN.match(line):
                if prose_lines:
                    units.append(" ".join(prose_lines))
                    prose_lines = []
                bullet_lines.append(line)
                continue
            if bullet_lines:
                bullet_lines[-1] = f"{bullet_lines[-1]} {line}"
            else:
                prose_lines.append(line)
        if prose_lines:
            units.append(" ".join(prose_lines))
        units.extend(bullet_lines)
    return [unit for unit in units if unit]


def evidence_answers_query(query: str, evidence: list[dict[str, Any]]) -> bool:
    # The retrieval/context contract is bounded to six final evidence items.
    # Inspect that whole bounded set: a project-first route may place the
    # shared document at E4-E6 even though it is already safely retrieved.
    checked_evidence = list(evidence[:EVIDENCE_QUERY_CHECK_LIMIT])
    quoted_topics = [
        item.strip()
        for item in re.findall(r"[“\"]([^”\"]+)[”\"]", query or "")
        if item.strip()
    ]
    source_topics = [
        match.group(1).strip()
        for match in re.finditer(
            r"[“\"]([^”\"]+)[”\"]\s*(?:里|中|中的|文档中|文件中|报告中|说明中)",
            query or "",
        )
        if match.group(1).strip()
    ]
    content_topics = [topic for topic in quoted_topics if topic not in source_topics]
    if quoted_topics:
        for item in checked_evidence:
            source_text = re.sub(
                r"\s+",
                "",
                " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "document_title",
                        "document_number",
                        "source_filename",
                        "section",
                        "section_path",
                    )
                ),
            )
            source_match = bool(source_topics) and any(
                re.sub(r"\s+", "", topic) in source_text
                for topic in source_topics
            )
            content_text = " ".join(
                str(item.get(key) or "")
                for key in ("section", "section_path", "snippet", "context")
            )

            if content_topics:
                if _detail_topics_match_content(content_topics, content_text) and (not source_topics or source_match):
                    return True

                topic_terms = content_terms(" ".join(content_topics))
                evidence_content_terms = content_terms(content_text)
                overlap = _detail_term_overlap(topic_terms, evidence_content_terms)
                coverage = len(overlap) / max(len(topic_terms), 1)
                if source_match and (len(overlap) >= 2 or coverage >= 0.34):
                    return True
            elif source_match:
                return True

        # A matching document title locates the source but does not prove that
        # the requested detail was present in its extracted content.
        if source_topics and content_topics:
            return False

    evidence_text = " ".join(
        " ".join(
            str(item.get(key) or "")
            for key in ("source_filename", "document_title", "section", "section_path", "snippet", "context")
        )
        for item in checked_evidence
    )
    missing_hard_tokens = [
        token
        for token in _query_hard_tokens(query)
        if _normalize_hard_token(token) not in _normalize_hard_token(evidence_text)
    ]
    if missing_hard_tokens:
        return False

    query_terms = content_terms(query)
    if not query_terms:
        return True

    evidence_terms = content_terms(evidence_text)

    if not evidence_terms:
        return False

    overlap = query_terms & evidence_terms
    signal_terms = _query_signal_terms(query)
    signal_overlap = signal_terms & evidence_terms
    coverage = len(overlap) / max(len(query_terms), 1)
    signal_coverage = len(signal_overlap) / max(len(signal_terms), 1)
    if (
        (is_definition_query(query) or is_synthesis_query(query))
        and any(is_strong_entity_term(term) for term in overlap)
    ):
        return True
    if signal_terms and not signal_overlap:
        return False
    return (
        len(overlap) >= 2
        and (not signal_terms or len(signal_overlap) >= 2 or signal_coverage >= 0.34)
    ) or coverage >= 0.34


def _query_hard_tokens(query: str) -> list[str]:
    tokens = []
    seen = set()
    for token in _QUERY_HARD_TOKEN_PATTERN.findall(str(query or "")):
        normalized = _normalize_hard_token(token)
        if (
            len(normalized) < 2
            or normalized in _QUERY_HARD_TOKEN_STOPWORDS
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        tokens.append(token)
    return tokens


def _normalize_hard_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _query_signal_terms(query: str) -> set[str]:
    terms = content_terms(query)
    return {
        term
        for term in terms
        if term not in _QUERY_GENERIC_TERMS
        and not any(generic in term for generic in _QUERY_GENERIC_TERMS)
    }


def content_terms(text: str) -> set[str]:
    normalized = (text or "").lower()
    terms = set()
    for raw_term in re.findall(r"[A-Za-z0-9_-]{2,}", normalized):
        # File names commonly encode method names as self_rag or
        # corrective-rag.  Keep the whole token for identifiers, but also
        # expose its components so cross-language evidence checks can match
        # the method named in the question.
        for term in (raw_term, *re.split(r"[-_]", raw_term)):
            if len(term) < 2 or is_stop_term(term):
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


def _detail_topics_match_content(detail_hints: list[str], content_text: str) -> bool:
    evidence_terms = content_terms(content_text)
    if not evidence_terms:
        return False
    for hint in detail_hints:
        hint_terms = content_terms(hint)
        if not hint_terms:
            continue
        if _detail_term_overlap(hint_terms, evidence_terms):
            return True
    return False


def _detail_term_overlap(query_terms: set[str], evidence_terms: set[str]) -> set[str]:
    overlap = query_terms & evidence_terms
    if overlap:
        return overlap
    fuzzy_overlap: set[str] = set()
    for query_term in query_terms:
        if not is_strong_entity_term(query_term):
            continue
        for evidence_term in evidence_terms:
            if query_term.startswith(evidence_term) or evidence_term.startswith(query_term):
                fuzzy_overlap.add(query_term)
                break
    return fuzzy_overlap


def is_definition_query(query: str) -> bool:
    return any(marker in (query or "") for marker in ("是什么", "定义", "机制", "原理", "概念"))


def is_synthesis_query(query: str) -> bool:
    return any(marker in (query or "") for marker in ("总结", "概述", "区别", "差异", "比较", "对比", "共同思路"))


def query_requires_corroboration(query: str | None) -> bool:
    """保留跨文档、因果和数值结论的多源证据要求。"""
    return any(
        marker in (query or "")
        for marker in (
            "比较",
            "对比",
            "区别",
            "差异",
            "原因",
            "为什么",
            "导致",
            "影响",
            "提升",
            "下降",
            "数值",
            "指标",
            "实验",
        )
    )


def is_strong_entity_term(term: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9_]", term)) and len(term) >= 3


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0
