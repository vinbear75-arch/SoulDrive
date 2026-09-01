"""Evaluate the locally captured real SoulDrive pilot with one warm runtime."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.topic5_runtime import Topic5Runtime
from core.soulalign import decide as soulalign_decide
from core.audit_log import AuditLogger
from core.answer_quality import validate_answer_citations
from core.knowledge_base import LocalKnowledgeBase
from core.rag_engine import RAGEngine


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _select_rows(rows: list[dict[str, Any]], episode_ids: list[str] | None) -> list[dict[str, Any]]:
    requested = {str(value).strip() for value in (episode_ids or []) if str(value).strip()}
    if not requested:
        return rows
    selected = [row for row in rows if str(row.get("episode_id") or "") in requested]
    selected_ids = {str(row.get("episode_id") or "") for row in selected}
    missing = sorted(requested - selected_ids)
    if missing:
        raise ValueError(f"requested episode_id not found: {', '.join(missing)}")
    return selected


def _events(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in row["events"] if event.get("modality") not in {"speech_text", "audio", "screen"}]


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int(len(values) * q + 0.999999) - 1))]


PERCEPTION_STAGE_SOURCES = {
    "asr": "asr",
    "ocr": "ocr",
    "vlm": "vlm",
    "alignment": "soulalign",
}
FULL_CHAIN_STAGE_NAMES = (*PERCEPTION_STAGE_SOURCES, "retrieval", "first_token", "complete_answer")


def _latency_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number >= 0 else None


def _stage_measurement(
    stages: dict[str, Any],
    source_name: str,
    *,
    not_integrated_reason: str | None = None,
) -> dict[str, Any]:
    if not_integrated_reason:
        return {"status": "not_integrated", "latency_ms": None, "reason": not_integrated_reason}
    raw = stages.get(source_name)
    if not isinstance(raw, dict):
        return {"status": "not_measured", "latency_ms": None, "reason": f"runtime stage missing: {source_name}"}
    status = str(raw.get("status") or "unknown")
    measurement = {
        "status": status,
        # Runtime uses 0 for skipped stages.  Keep that state explicit rather
        # than treating a non-executed stage as a zero-latency measurement.
        "latency_ms": None if status == "skipped" else _latency_value(raw.get("latency_ms")),
    }
    reason = str(raw.get("reason") or raw.get("code") or "").strip()
    if reason:
        measurement["reason"] = reason[:240]
    return measurement


def build_stage_ledger(
    episode_id: str,
    runtime_result: dict[str, Any],
    wall_clock_latency_ms: float,
    rag_measurement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize per-stage evidence without turning absent stages into zeros."""
    stages = runtime_result.get("stages") if isinstance(runtime_result.get("stages"), dict) else {}
    stage_rows = {
        name: _stage_measurement(stages, source_name)
        for name, source_name in PERCEPTION_STAGE_SOURCES.items()
    }
    if rag_measurement is None:
        not_integrated_reason = "pilot evaluator currently measures perception/alignment only"
        stage_rows.update({
            name: _stage_measurement({}, name, not_integrated_reason=not_integrated_reason)
            for name in ("retrieval", "first_token", "complete_answer")
        })
        measurement_scope = "perception_and_alignment_only"
        full_chain_complete = False
    else:
        stage_rows.update({
            name: dict((rag_measurement.get("stages") or {}).get(name) or {
                "status": "not_measured",
                "latency_ms": None,
            })
            for name in ("retrieval", "first_token", "complete_answer")
        })
        measurement_scope = "perception_alignment_and_rag"
        full_chain_complete = bool(rag_measurement.get("full_chain_complete"))
    return {
        "schema_version": "souldrive_mmi_stage_ledger_v0.2" if rag_measurement else "souldrive_mmi_stage_ledger_v0.1",
        "episode_id": episode_id,
        "measurement_scope": measurement_scope,
        "stages": stage_rows,
        "wall_clock_latency_ms": _latency_value(wall_clock_latency_ms),
        "full_chain_complete": full_chain_complete,
    }


def _captured_active_context(row: dict[str, Any]) -> dict[str, Any]:
    """Build RAG context from captured metadata, never from answer labels."""
    active: dict[str, Any] = {}
    project_id = str(row.get("project_id") or "").strip()
    if project_id:
        active["project_id"] = project_id[:128]
    for event in row.get("events") or []:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        modality = str(event.get("modality") or "").strip().lower()
        if modality in {"ui_selection", "ui_automation"}:
            # ``text`` is often only a target label (for example, a section
            # name).  It is not safe to use it as the selected document text.
            # Only an explicit selected_text field may enter the RAG context.
            selected_text = str(payload.get("selected_text") or event.get("selected_text") or "").strip()
            if selected_text:
                active["selected_text"] = selected_text[:4000]
        if modality == "screen":
            window_title = str(payload.get("window_title") or event.get("window_title") or "").strip()
            if window_title:
                active["window_title"] = window_title[:260]
                document_name = window_title.split(" - ", 1)[0].strip()
                if document_name:
                    active["document_name"] = document_name[:260]
    return active


def _not_applicable_rag_measurement(reason: str) -> dict[str, Any]:
    stages = {
        name: {"status": "not_applicable", "latency_ms": None, "reason": reason}
        for name in ("retrieval", "first_token", "complete_answer")
    }
    return {
        "status": "not_applicable",
        "eligible": False,
        "full_chain_complete": False,
        "stages": stages,
        "response_text": "",
        "response_has_citation": False,
        "evidence_backed_response": False,
        "evidence_count": 0,
    }


def _run_rag_episode(
    rag: RAGEngine,
    row: dict[str, Any],
    perception: dict[str, Any],
    *,
    top_k: int,
) -> dict[str, Any]:
    """Run the real ASR/visual context through the existing RAG engine."""
    query = str(perception.get("speech_text") or "").strip()[:6000]
    decision = perception.get("decision") if isinstance(perception.get("decision"), dict) else {}
    if not query:
        return _not_applicable_rag_measurement("ASR did not produce a query")
    if bool(decision.get("clarification_required")):
        return _not_applicable_rag_measurement("SoulAlign requires clarification before retrieval")

    retrieval: dict[str, Any] = {}
    first_token_ms: float | None = None
    started = time.perf_counter()

    def on_retrieval_completed(value: dict[str, Any]) -> None:
        retrieval.update(value or {})

    def on_generation_chunk(value: str) -> None:
        nonlocal first_token_ms
        if first_token_ms is None and str(value or "").strip():
            first_token_ms = (time.perf_counter() - started) * 1000

    try:
        # Episode timestamps are relative to capture start. Omitting a wall-clock
        # query timestamp keeps those observed events in the interaction window.
        chunks = list(rag.generate_response_stream(
            query=query,
            top_k=max(1, min(int(top_k), 20)),
            interaction_context={
                "active_context": _captured_active_context(row),
                "events": list(perception.get("perception_events") or []),
            },
            on_retrieval_completed=on_retrieval_completed,
            on_generation_chunk=on_generation_chunk,
        ))
    except Exception as exc:
        error_type = type(exc).__name__
        return {
            "status": "failed",
            "eligible": True,
            "full_chain_complete": False,
            "error": error_type,
            "stages": {
                "retrieval": {
                    "status": "failed",
                    "latency_ms": _latency_value(retrieval.get("latency_ms")),
                    "reason": error_type,
                },
                "first_token": {"status": "failed", "latency_ms": _latency_value(first_token_ms), "reason": error_type},
                "complete_answer": {"status": "failed", "latency_ms": None, "reason": error_type},
            },
            "response_text": "",
            "response_has_citation": False,
            "evidence_backed_response": False,
            "evidence_count": int(retrieval.get("evidence_count") or 0),
        }

    response_text = "".join(str(chunk or "") for chunk in chunks)
    from scripts.eval_rag import split_answer_and_evidence

    answer_text, parsed_evidence = split_answer_and_evidence(response_text)
    citation_count = len(re.findall(r"\[(?:E|V|A|M)\d+\]", answer_text))
    evidence_count = int(retrieval.get("evidence_count") or 0)
    retrieval_route = str(retrieval.get("route") or "document_retrieval").strip()
    visual_observation_only = retrieval_route == "visual_observation_only"
    response_has_citation = citation_count > 0
    citation_validation = validate_answer_citations(
        answer_text,
        parsed_evidence,
        semantic=True,
    )
    evidence_block_valid = bool(parsed_evidence) if (evidence_count or visual_observation_only) else False
    citation_support_applicable = bool(evidence_count or visual_observation_only)
    citation_support_valid = bool(
        citation_support_applicable
        and evidence_block_valid
        and response_has_citation
        and citation_validation.get("valid")
    )
    evidence_backed_response = citation_support_valid
    retrieval_status = (
        "not_applicable"
        if visual_observation_only
        else ("pass" if evidence_count > 0 else "partial")
    )
    complete_status = "pass" if response_text.strip() and evidence_backed_response else (
        "partial" if response_text.strip() else "failed"
    )
    first_token_status = "pass" if first_token_ms is not None else "not_measured"
    full_chain_complete = retrieval_status == "pass" and first_token_status == "pass" and complete_status == "pass"
    route_complete = visual_observation_only and complete_status == "pass"
    return {
        "status": "pass" if full_chain_complete or route_complete else ("partial" if response_text.strip() else "failed"),
        "eligible": True,
        "full_chain_complete": full_chain_complete,
        "query": query,
        "project_id": str(row.get("project_id") or "").strip() or None,
        "response_text": response_text,
        "response_chars": len(response_text),
        "response_has_citation": response_has_citation,
        "citation_count_in_answer": citation_count,
        "evidence_block_count": len(parsed_evidence),
        "evidence_block_valid": evidence_block_valid,
        "citation_validation": citation_validation,
        "citation_support_valid": citation_support_valid,
        "evidence_backed_response": evidence_backed_response,
        "evidence_count": evidence_count,
        "retrieval_route": retrieval_route,
        "citation_support_scope": "visual_observation" if visual_observation_only else "document_retrieval",
        "stages": {
            "retrieval": {
                "status": retrieval_status,
                "latency_ms": _latency_value(retrieval.get("latency_ms")),
                "evidence_count": evidence_count,
            },
            "first_token": {"status": first_token_status, "latency_ms": _latency_value(first_token_ms)},
            "complete_answer": {
                "status": complete_status,
                "latency_ms": _latency_value((time.perf_counter() - started) * 1000),
                "response_chars": len(response_text),
                "citation_count_in_answer": citation_count,
                "evidence_block_count": len(parsed_evidence),
                "citation_support_valid": citation_support_valid,
                "citation_support_reason": citation_validation.get("reason"),
            },
        },
    }


def _error_types(
    gold: dict[str, Any],
    prediction: dict[str, Any],
    runtime_result: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if prediction.get("intent") != gold.get("intent"):
        errors.append("intent_mismatch")
    if prediction.get("target_id") != gold.get("target_id"):
        errors.append("target_binding_mismatch")
    if prediction.get("action") != gold.get("expected_action"):
        errors.append("action_mismatch")
    expected_clarification = gold.get("expected_action") == "clarify"
    if prediction.get("clarification_required") != expected_clarification:
        errors.append("clarification_mismatch")
    if runtime_result.get("status") not in {"pass", "partial"}:
        errors.append("runtime_failure")
    for name, stage in (runtime_result.get("stages") or {}).items():
        if isinstance(stage, dict) and stage.get("status") in {"failed", "blocked"}:
            errors.append(f"{name}_{stage['status']}")
    return list(dict.fromkeys(errors))


def summarize_stage_coverage(ledgers: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for stage_name in FULL_CHAIN_STAGE_NAMES:
        counts = Counter(
            str((ledger.get("stages") or {}).get(stage_name, {}).get("status") or "missing")
            for ledger in ledgers
        )
        measured_count = sum(
            count
            for status, count in counts.items()
            if status in {"pass", "failed", "blocked", "partial"}
        )
        latency_values = [
            float((ledger.get("stages") or {}).get(stage_name, {}).get("latency_ms"))
            for ledger in ledgers
            if isinstance((ledger.get("stages") or {}).get(stage_name), dict)
            and (ledger.get("stages") or {}).get(stage_name, {}).get("latency_ms") is not None
        ]
        summary[stage_name] = {
            "status_counts": dict(sorted(counts.items())),
            "measured_count": measured_count,
            "latency_count": len(latency_values),
            "latency_ms": {
                "p50": statistics.median(latency_values) if latency_values else None,
                "p95": _pct(latency_values, 0.95) if latency_values else None,
                "max": max(latency_values) if latency_values else None,
            },
        }
    return summary


def evaluate(
    collection: Path,
    output: Path,
    *,
    episode_ids: list[str] | None = None,
    warmup_perception: bool = False,
    run_vlm: bool = False,
    rag_workspace: Path | None = None,
    rag_top_k: int = 6,
    rag_rerank_candidate_limit: int | None = None,
) -> dict[str, Any]:
    rows = _select_rows(_rows(collection / "episodes.jsonl"), episode_ids)
    output.mkdir(parents=True, exist_ok=True)
    runtime = Topic5Runtime()
    kb = None
    rag = None
    if rag_workspace is not None and rows:
        rag_workspace = rag_workspace.resolve()
        if not rag_workspace.is_dir():
            raise ValueError(f"RAG evaluation workspace does not exist: {rag_workspace}")
        index_root = rag_workspace / "index"
        if not (index_root / "keyword_index.sqlite").is_file() or not (index_root / "parent_docs.sqlite").is_file():
            raise ValueError("RAG evaluation workspace must contain an indexed keyword and parent-document store")
        kb = LocalKnowledgeBase(
            workspace_path=str(rag_workspace),
            enable_usage_ledger=False,
            rerank_candidate_limit=rag_rerank_candidate_limit,
        )
        rag = RAGEngine(
            kb=kb,
            graph_db_path=str(output / "rag_graph.sqlite"),
            workspace_path=str(rag_workspace),
            audit_logger=AuditLogger(
                output / "rag_audit.jsonl",
                output / "rag_audit_state.json",
            ),
        )
    warmup_started = time.perf_counter()
    warmup_result = runtime.warmup_perception() if warmup_perception else None
    warmup_latency_ms = round((time.perf_counter() - warmup_started) * 1000, 3) if warmup_perception else None
    records: list[dict[str, Any]] = []
    stage_ledgers: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(rows):
            audio = next(event["payload_ref"] for event in row["events"] if event.get("modality") == "audio")
            image = next(event["payload_ref"] for event in row["events"] if event.get("modality") == "screen")
            truth = row["ground_truth"]
            started = time.perf_counter()
            if run_vlm and rag is not None:
                release_generation_model = getattr(rag, "release_generation_model", None)
                if callable(release_generation_model):
                    release_generation_model()
            result = runtime.infer(
                workspace_root=collection,
                audio_ref=audio,
                image_ref=image,
                events=_events(row),
                # Do not inject the gold target into the runtime.  The real
                # selection event above is the only target context available to
                # this re-test; the gold value is used only after inference.
                target_id=None,
                # The active project is represented by the captured event stream;
                # do not inject the episode's gold project as a second shortcut.
                project_id=None,
                run_ocr=True,
                run_vlm=run_vlm,
                visual_mode="slow" if run_vlm else "fast",
                max_new_tokens=48 if run_vlm else 24,
            )
            if run_vlm:
                release_vlm = getattr(runtime, "release_vlm", None)
                if callable(release_vlm):
                    release_vlm()
            decision = result.get("decision") or {}
            prediction = {
                "intent": decision.get("intent"),
                "target_id": decision.get("target_id"),
                "conflict_type": decision.get("conflict_type"),
                "action": decision.get("action"),
                "clarification_required": bool(decision.get("clarification_required")),
            }
            text_only_decision = soulalign_decide(str(result.get("speech_text") or ""), [], confidence_threshold=0.7)
            text_only_prediction = {
                "intent": text_only_decision.get("intent"),
                "target_id": text_only_decision.get("target_id"),
                "conflict_type": text_only_decision.get("conflict_type"),
                "action": text_only_decision.get("action"),
                "clarification_required": bool(text_only_decision.get("clarification_required")),
            }
            rag_measurement = _run_rag_episode(rag, row, result, top_k=rag_top_k) if rag is not None else None
            wall_clock_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            stage_ledger = build_stage_ledger(row["episode_id"], result, wall_clock_latency_ms, rag_measurement)
            stage_ledgers.append(stage_ledger)
            error_types = _error_types(truth, prediction, result)
            records.append({
                "episode_id": row["episode_id"],
                "gold": truth,
                "prediction": prediction,
                "text_only_prediction": text_only_prediction,
                "speech_text": result.get("speech_text"),
                "runtime": result,
                "rag": rag_measurement,
                "wall_clock_latency_ms": wall_clock_latency_ms,
                "stage_ledger": stage_ledger,
                "error_types": error_types,
                "clarification": {
                    "expected": truth.get("expected_action") == "clarify",
                    "predicted": prediction.get("clarification_required"),
                    "correct": "clarification_mismatch" not in error_types,
                },
                "run_order": index,
            })
    finally:
        if rag is not None:
            rag.close()
        if kb is not None:
            kb.close()
        runtime.close()
    truths = [record["gold"] for record in records]
    preds = [record["prediction"] for record in records]
    text_only_preds = [record["text_only_prediction"] for record in records]
    target_rows = [(gold, pred) for gold, pred in zip(truths, preds) if gold.get("target_id")]
    text_only_target_rows = [(gold, pred) for gold, pred in zip(truths, text_only_preds) if gold.get("target_id")]
    latencies = [float(record["wall_clock_latency_ms"]) for record in records]
    warm_latencies = latencies[1:]
    rag_records = [
        record.get("rag") or {}
        for record in records
        if isinstance(record.get("rag"), dict) and record["rag"].get("eligible")
    ]
    citation_support_records = [
        record
        for record in rag_records
        if "citation_support_valid" in record
    ]
    citation_support_invalid_count = sum(
        record.get("citation_support_valid") is False
        for record in citation_support_records
    )
    citation_support_unsupported_claim_count = sum(
        int(
            ((record.get("citation_validation") or {}).get("semantic_support") or {}).get(
                "unsupported_claim_count"
            )
            or 0
        )
        for record in citation_support_records
    )
    failures = [
        {
            "episode_id": record["episode_id"],
            "gold": record["gold"],
            "prediction": record["prediction"],
            "speech_text": record.get("speech_text"),
            "error_types": record.get("error_types") or [],
            "clarification": record.get("clarification") or {},
        }
        for record in records
        if record.get("error_types")
    ]
    metrics = {
        "sample_count": len(records),
        "intent_accuracy": sum(pred.get("intent") == gold.get("intent") for gold, pred in zip(truths, preds)) / len(records) if records else 0.0,
        "target_binding_accuracy_on_targeted": sum(pred.get("target_id") == gold.get("target_id") for gold, pred in target_rows) / len(target_rows) if target_rows else None,
        "action_accuracy": sum(pred.get("action") == gold.get("expected_action") for gold, pred in zip(truths, preds)) / len(records) if records else 0.0,
        "clarification_accuracy": sum(pred.get("clarification_required") == (gold.get("expected_action") == "clarify") for gold, pred in zip(truths, preds)) / len(records) if records else 0.0,
        "latency_ms": {"p50": statistics.median(latencies) if latencies else 0.0, "p95": _pct(latencies, 0.95), "max": max(latencies) if latencies else 0.0},
        "cold_start_ms": latencies[0] if latencies else 0.0,
        "warm_latency_ms": {"count": len(warm_latencies), "p50": statistics.median(warm_latencies) if warm_latencies else 0.0, "p95": _pct(warm_latencies, 0.95), "max": max(warm_latencies) if warm_latencies else 0.0},
        "failure_count": len(failures),
        "stage_coverage": summarize_stage_coverage(stage_ledgers),
        "full_chain_coverage": bool(
            rag is not None and any(ledger.get("full_chain_complete") for ledger in stage_ledgers)
            and all(
                ledger.get("full_chain_complete")
                for ledger in stage_ledgers
                if (ledger.get("measurement_scope") == "perception_alignment_and_rag"
                    and (ledger.get("stages") or {}).get("retrieval", {}).get("status") != "not_applicable")
            )
        ),
        "full_chain_coverage_detail": {
            "enabled": rag_workspace is not None,
            "run_vlm": run_vlm,
            "rerank_candidate_limit": rag_rerank_candidate_limit,
            "eligible_count": sum(bool((record.get("rag") or {}).get("eligible")) for record in records),
            "complete_count": sum(bool((record.get("rag") or {}).get("full_chain_complete")) for record in records),
            "citation_support_applicable_count": len(citation_support_records),
            "citation_support_valid_count": sum(
                record.get("citation_support_valid") is True
                for record in citation_support_records
            ),
            "citation_support_invalid_count": citation_support_invalid_count,
            "citation_support_unsupported_claim_count": citation_support_unsupported_claim_count,
        },
        "perception_warmup": {
            "requested": warmup_perception,
            "status": warmup_result.get("status") if isinstance(warmup_result, dict) else "not_requested",
            "wall_clock_latency_ms": warmup_latency_ms,
        },
        "visual_mode": "slow" if run_vlm else "fast",
        "text_only_baseline": {
            "intent_accuracy": sum(pred.get("intent") == gold.get("intent") for gold, pred in zip(truths, text_only_preds)) / len(records) if records else 0.0,
            "target_binding_accuracy_on_targeted": sum(pred.get("target_id") == gold.get("target_id") for gold, pred in text_only_target_rows) / len(text_only_target_rows) if text_only_target_rows else None,
            "action_accuracy": sum(pred.get("action") == gold.get("expected_action") for gold, pred in zip(truths, text_only_preds)) / len(records) if records else 0.0,
            "clarification_accuracy": sum(pred.get("clarification_required") == (gold.get("expected_action") == "clarify") for gold, pred in zip(truths, text_only_preds)) / len(records) if records else 0.0,
        },
        "limitations": [
            "真实用户授权 WAV/截图，但仅 10 条 pilot；不是公开集或华为隐藏集成绩。",
            ("本轮显式执行 VLM 慢路径；是否通过和耗时以逐阶段记录为准，不能外推为图标/图表准确率。"
             if run_vlm else "本轮使用快路径，VLM 慢路径仍未纳入；PILOT-11/12 未采集，不补造样本。"),
            *(["RAG 全链路只在显式提供的本地测试工作区运行；查询来自 ASR，未注入答案标签；澄清样本在检索前记为 not_applicable。", "该模式测量检索、首模型 token、完整回答，以及复用生产校验器的引用内容支持；支持检查是确定性词法代理，不等于答案事实准确率或竞赛成绩。"] if rag_workspace is not None else ["完整 RAG、首 token 和完整回答尚未接入该默认 pilot evaluator。"]),
        ],
    }
    (output / "predictions.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (output / "failures.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures), encoding="utf-8")
    (output / "stage_ledger.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in stage_ledgers),
        encoding="utf-8",
    )
    (output / "perception_warmup.json").write_text(
        json.dumps(
            {
                "schema_version": "souldrive_real_mmi_perception_warmup_eval_v0.1",
                "requested": warmup_perception,
                "wall_clock_latency_ms": warmup_latency_ms,
                "result": warmup_result,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "metrics.json").write_text(json.dumps({"schema_version": "souldrive_real_mmi_pilot_metrics_v0.2" if rag_workspace is not None else "souldrive_real_mmi_pilot_metrics_v0.1", "dataset_sha256": hashlib.sha256((collection / "episodes.jsonl").read_bytes()).hexdigest(), "metrics": metrics}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    base = metrics["text_only_baseline"]
    warmup_summary = (
        f"- 显式感知预热：`{metrics['perception_warmup']['status']}`，"
        f"`{metrics['perception_warmup']['wall_clock_latency_ms']:.1f} ms`"
        if metrics["perception_warmup"]["requested"]
        else "- 显式感知预热：`not_requested`"
    )
    full_chain = metrics["full_chain_coverage_detail"]
    full_chain_summary = (
        f"- RAG 全链路：`{full_chain['complete_count']}/{full_chain['eligible_count']}` 个可检索样本完整通过"
        if full_chain["enabled"]
        else "- RAG 全链路：`not_integrated`（默认 pilot 仅测感知/对齐）"
    )
    (output / "summary.md").write_text("# SoulDrive real MMI pilot\n\n" + "\n".join([f"- 样本数：`{metrics['sample_count']}`", f"- Intent accuracy（B1/Candidate）：`{base['intent_accuracy']:.4f}/{metrics['intent_accuracy']:.4f}`", f"- Target binding（B1/Candidate）：`{base['target_binding_accuracy_on_targeted']}/{metrics['target_binding_accuracy_on_targeted']}`", f"- Action accuracy（B1/Candidate）：`{base['action_accuracy']:.4f}/{metrics['action_accuracy']:.4f}`", f"- Clarification accuracy（B1/Candidate）：`{base['clarification_accuracy']:.4f}/{metrics['clarification_accuracy']:.4f}`", full_chain_summary, warmup_summary, f"- 冷启动：`{metrics['cold_start_ms']:.1f} ms`", f"- 热运行 P50/P95/max：`{metrics['warm_latency_ms']['p50']:.1f}/{metrics['warm_latency_ms']['p95']:.1f}/{metrics['warm_latency_ms']['max']:.1f} ms`", f"- 失败样本：`{metrics['failure_count']}`", "", "## 限制", "", *[f"- {item}" for item in metrics["limitations"]]]) + "\n", encoding="utf-8")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--episode-id",
        action="append",
        default=None,
        help="只评测指定 episode；可重复传入，指定不存在的 ID 会直接失败",
    )
    parser.add_argument(
        "--warmup-perception",
        action="store_true",
        help="在第一条 episode 前显式加载本地 ASR/OCR，并单独记录预热成本",
    )
    parser.add_argument(
        "--run-vlm",
        action="store_true",
        help="显式执行用户授权裁剪区域的 VLM 慢路径，并将其纳入阶段账本",
    )
    parser.add_argument(
        "--rag-workspace",
        type=Path,
        default=None,
        help="显式启用真实 ASR/视觉上下文到 RAG 的全链路评测；必须是已有本地测试索引",
    )
    parser.add_argument("--rag-top-k", type=int, default=6)
    parser.add_argument("--rag-rerank-candidate-limit", type=int, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(
                args.collection_root.resolve(),
                args.output_dir.resolve(),
                episode_ids=args.episode_id,
                warmup_perception=args.warmup_perception,
                run_vlm=args.run_vlm,
                rag_workspace=args.rag_workspace.resolve() if args.rag_workspace else None,
                rag_top_k=args.rag_top_k,
                rag_rerank_candidate_limit=args.rag_rerank_candidate_limit,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
