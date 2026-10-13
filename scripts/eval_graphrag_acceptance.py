"""Run a reproducible local GraphRAG B0/B1/Candidate acceptance check."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.eval_rag import loaded_chat_model_name

REFUSAL_MARKERS = (
    "未找到足够可靠",
    "无法确定",
    "无法回答",
    "evidence does not answer query",
)
GLOBAL_SOURCE_GATE_FALLBACK_MARKER = "自动综合未通过来源多样性校验"


def configure_local_ollama() -> None:
    os.environ.setdefault("SOULDRIVE_CHAT_PROVIDER", "ollama")
    os.environ.setdefault("SOULDRIVE_OLLAMA_MODEL", "qwen3.5:9b")
    os.environ.setdefault("SOULDRIVE_OLLAMA_URL", "http://127.0.0.1:11434")


def _evidence_source_parts(item: dict[str, Any]) -> tuple[str, str, str]:
    """Return a scope-aware identity, display label, and raw source name."""
    raw_source = str(
        item.get("source_filename")
        or item.get("document_title")
        or item.get("document_number")
        or ""
    ).strip()
    if not raw_source:
        return "", "", ""
    scope = str(item.get("project_id") or "").strip() or "global"
    identity = next(
        (
            str(item.get(field) or "").strip()
            for field in ("asset_version_id", "document_id", "asset_id")
            if str(item.get(field) or "").strip()
        ),
        raw_source,
    )
    source_key = f"{scope}\0{identity}"
    display = raw_source if scope == "global" else f"{scope}/{raw_source}"
    return source_key, display, raw_source


def unique_sources(evidence: list[dict[str, Any]]) -> list[str]:
    sources = []
    seen_keys = set()
    seen_labels = set()
    for item in evidence:
        source_key, display, _raw_source = _evidence_source_parts(item)
        if not source_key or source_key in seen_keys:
            continue
        if display in seen_labels:
            _scope, identity = source_key.split("\0", 1)
            display = f"{display}#{identity}"
        sources.append(display)
        seen_keys.add(source_key)
        seen_labels.add(display)
    return sources


def evidence_summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    sources = unique_sources(evidence)
    return {
        "count": len(evidence),
        "sources": sources,
        "source_count": len(sources),
        "raw_sources_only": all(
            not _evidence_source_parts(item)[2].startswith("GraphRAG社区报告/")
            for item in evidence
        ),
        "citation_ids": [str(item.get("id")) for item in evidence if item.get("id")],
    }


def citations_are_bound(answer: str, evidence_ids: list[str]) -> bool:
    """Require every answer citation to refer to this run's admitted evidence."""
    citation_ids = {
        f"{kind}{number}"
        for kind, number in re.findall(r"\[([EVAM])(\d+)\]", str(answer or ""))
    }
    allowed_ids = {
        str(item or "").strip()
        for item in evidence_ids or []
        if str(item or "").strip()
    }
    return bool(citation_ids) and bool(allowed_ids) and citation_ids.issubset(allowed_ids)


def gate_summary(gate: Any, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **gate.public_dict(),
        "evidence": evidence_summary(evidence),
    }


def recent_event(audit_logger: Any, event_type: str, trace_id: str) -> dict[str, Any]:
    for event in reversed(audit_logger.read_recent(limit=200)):
        if event.get("event_type") == event_type and event.get("trace_id") == trace_id:
            return event
    return {}


def answer_execution(audit_logger: Any, trace_id: str) -> dict[str, Any]:
    """Report the observed answer path without changing acceptance scores."""
    paths = {
        "rag.completed": "model_generated",
        "rag.refused_invalid_answer": "validation_fallback",
        "rag.answer_quality_fallback": "quality_fallback",
        "rag.extractive_lookup": "extractive_lookup",
    }
    for event in reversed(audit_logger.read_recent(limit=200)):
        event_type = event.get("event_type")
        if event.get("trace_id") != trace_id or event_type not in paths:
            continue
        payload = event.get("payload") or {}
        return {
            "path": paths[event_type],
            "event_type": event_type,
            "answer_attempts": payload.get("answer_attempts"),
            "validation_reason": (payload.get("validation") or {}).get("reason"),
        }
    return {"path": "unknown", "event_type": None, "answer_attempts": None, "validation_reason": None}


def final_retrieval_evidence_ids(audit_logger: Any, trace_id: str) -> list[str]:
    """Read evidence IDs from the pass that actually supplied the answer."""
    initial_event = recent_event(audit_logger, "rag.retrieved", trace_id)
    initial_payload = (initial_event.get("payload") if initial_event else {}) or {}
    retry_event = recent_event(audit_logger, "rag.retrieval_retry", trace_id)
    retry_payload = (retry_event.get("payload") if retry_event else {}) or {}
    if retry_payload.get("used"):
        return [
            str(item).strip()
            for item in retry_payload.get("evidence_ids") or []
            if str(item).strip()
        ]
    return [
        str(item).strip()
        for item in initial_payload.get("evidence_ids") or []
        if str(item).strip()
    ]


def graph_index_diagnostics(rag: Any, project_id: str | None = None) -> dict[str, Any]:
    """Use the runtime GraphRAG object so scope freshness can be revalidated."""
    runtime_graph = getattr(rag, "graph_rag", None)
    diagnostics = getattr(runtime_graph, "diagnostics", None)
    if callable(diagnostics):
        return diagnostics(project_id=project_id)

    # Keep a narrow fallback for older test doubles or runtimes that expose
    # only the graph store.  The production RAGEngine takes the provider path
    # above, which is required for scope-aware indexes.
    from core.graph_rag import GraphRAGIndex

    return GraphRAGIndex(rag.graph_db).diagnostics(project_id=project_id)


def acceptance_passes(acceptance: dict[str, Any]) -> bool:
    """Evaluate required gates without treating scope choice as a failure."""
    return all(
        bool(value)
        for key, value in acceptance.items()
        if key != "project_scope_requested"
    )


def run_query(*, rag, kb, query: str, index_diagnostics: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    from core.answer_quality import evaluate_evidence_gate
    from core.graph_rag import route_graph_query
    from core.rag_engine import gate_retrieval_context

    route = route_graph_query(query)
    b0_raw = kb.search_with_evidence(
        query=query,
        graph_db=None,
        top_k=6,
        project_id=project_id,
        global_only=not bool(project_id),
    )
    _, b0_gate = gate_retrieval_context(query, b0_raw, top_k=6)

    b1_raw = rag._apply_graph_rag_route(b0_raw, route, query, project_id=project_id)
    _, b1_gate = gate_retrieval_context(query, b1_raw, top_k=6)
    b1_evidence_gate = evaluate_evidence_gate(b1_raw.get("evidence") or [], query=query)
    b1_graph = (b1_raw.get("observability") or {}).get("graph_rag") or {}

    trace_id = f"graphrag-acceptance-{int(time.time() * 1000)}"
    started = time.perf_counter()
    answer = ""
    error = None
    try:
        interaction_context = {"active_context": {"project_id": project_id}} if project_id else None
        answer = "".join(rag.generate_response_stream(
            query,
            top_k=6,
            trace_id=trace_id,
            interaction_context=interaction_context,
        ))
    except Exception as exc:  # The artifact must preserve a failed runtime check.
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    answer_body = answer.split(chr(96) * 3 + "souldrive-evidence", 1)[0].strip()
    retrieved_event = recent_event(rag.audit_logger, "rag.retrieved", trace_id)
    retrieved_payload = (retrieved_event.get("payload") if retrieved_event else {}) or {}
    candidate_graph = (retrieved_payload.get("retrieval_observability") or {}).get("graph_rag") or {}
    retrieved_evidence_ids = final_retrieval_evidence_ids(rag.audit_logger, trace_id)
    citation_binding = citations_are_bound(answer_body, retrieved_evidence_ids)
    citation_ids = sorted({
        f"{kind}{number}"
        for kind, number in re.findall(r"\[([EVAM])(\d+)\]", answer_body)
    })
    validation_event = recent_event(rag.audit_logger, "rag.completed", trace_id)
    if not validation_event:
        validation_event = recent_event(rag.audit_logger, "rag.refused_invalid_answer", trace_id)
    validation_payload = (validation_event.get("payload") if validation_event else {}) or {}
    citation_validation = validation_payload.get("citation_validation") or {}
    if not citation_validation:
        citation_validation = validation_payload.get("validation") or {}
    required_source_count = int(
        citation_validation.get("required_source_count")
        or (
            2
            if (
                route.mode == "global"
                and candidate_graph.get("enabled")
                and int(candidate_graph.get("source_count") or 0) >= 2
            )
            else 0
        )
    )
    fallback_citation_count = len({item for item in citation_ids if item.startswith("E")})
    source_gate_fallback = GLOBAL_SOURCE_GATE_FALLBACK_MARKER in answer_body
    validated_source_count = int(
        citation_validation.get("cited_source_count")
        or validation_payload.get("fallback_cited_source_count")
        or 0
    )
    global_source_gate_satisfied = (
        required_source_count == 0
        or validated_source_count >= required_source_count
        or (
            source_gate_fallback
            and citation_binding
            and fallback_citation_count >= required_source_count
        )
    )

    return {
        "query": query,
        "project_id": project_id,
        "route": route.public_dict(),
        "index_snapshot": {
            "document_count": index_diagnostics.get("document_count"),
            "community_count": index_diagnostics.get("community_count"),
            "algorithm": index_diagnostics.get("algorithm"),
            "project_id": index_diagnostics.get("project_id"),
            "index_name": index_diagnostics.get("index_name"),
        },
        "b0_current": {
            "graph_enabled": False,
            "gate": gate_summary(b0_gate, b0_raw.get("evidence") or []),
        },
        "b1_simple_graph": {
            "graph_enabled": bool(b1_graph.get("enabled")),
            "graph": b1_graph,
            "context_gate": gate_summary(b1_gate, b1_raw.get("evidence") or []),
            "evidence_gate": b1_evidence_gate.public_dict(),
        },
        "candidate_live_answer": {
            "status": "pass" if answer and not error else "error",
            "error": error,
            "answer_execution": answer_execution(rag.audit_logger, trace_id),
            "elapsed_ms": elapsed_ms,
            "answer_chars": len(answer_body),
            "refusal": any(marker in answer_body for marker in REFUSAL_MARKERS),
            "has_citation": citation_binding,
            "citation_ids": citation_ids,
            "evidence_ids": retrieved_evidence_ids,
            "citation_binding": citation_binding,
            "global_source_gate": {
                "required_source_count": required_source_count,
                "validated_source_count": validated_source_count,
                "fallback_citation_count": fallback_citation_count,
                "controlled_fallback": source_gate_fallback,
                "satisfied": global_source_gate_satisfied,
            },
            "answer": answer_body,
            "retrieved_sources": retrieved_payload.get("sources") or [],
            "graph": candidate_graph,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("SOULDRIVE_WORKSPACE"))
    parser.add_argument("--passphrase", default=os.environ.get("SOULDRIVE_WORKSPACE_PASSPHRASE"))
    parser.add_argument("--project-id", default=os.environ.get("SOULDRIVE_RAG_PROJECT_ID"))
    parser.add_argument(
        "--output",
        default="eval/results/graphrag_acceptance_current.json",
    )
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace is required")

    configure_local_ollama()
    os.environ["SOULDRIVE_WORKSPACE"] = str(Path(args.workspace).resolve())

    from core.audit_log import AuditLogger
    from core.knowledge_base import LocalKnowledgeBase, _OllamaEmbeddingModel
    from core.rag_engine import RAGEngine
    from core.security_context import set_workspace_keys
    from core.workspace import SoulDriveWorkspace
    from core.workspace_crypto import unlock_keystore

    workspace = SoulDriveWorkspace(str(Path(args.workspace).resolve())).ensure()
    if (Path(workspace.root_path) / "index" / "secure_vectors.sqlite").exists():
        if not args.passphrase:
            parser.error("--passphrase is required for an encrypted local workspace")
        set_workspace_keys(workspace.root_path, unlock_keystore(workspace, args.passphrase))
    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
    rag = RAGEngine(
        kb=kb,
        graph_db_path=workspace.graph_db_path,
        workspace_path=workspace.root_path,
        audit_logger=AuditLogger.for_workspace(workspace),
    )
    try:
        index_diagnostics = graph_index_diagnostics(rag, project_id=args.project_id)
        queries = [
            "RAG 的核心流程包括哪些阶段？",
            "请从这些中文资料总结 RAG 检索、重排与生成的共同优化思路。",
            "GraphRAG 中实体、关系、社区报告之间是什么关系？",
        ]
        rows = [
            run_query(
                rag=rag,
                kb=kb,
                query=query,
                index_diagnostics=index_diagnostics,
                project_id=args.project_id,
            )
            for query in queries
        ]
        embedding_name = getattr(kb, "embedding_model_name", None)
        models = {
            "chat": loaded_chat_model_name(getattr(rag, "llm", None)),
            "embedding_provider": (
                "ollama"
                if isinstance(getattr(kb, "embedding_model", None), _OllamaEmbeddingModel)
                else "sentence-transformers" if getattr(kb, "_embedding_model_path", None) else None
            ),
            "embedding": embedding_name.strip() if isinstance(embedding_name, str) and embedding_name.strip() else None,
        }
    finally:
        rag.close()
        kb.close()

    candidate_rows = [row["candidate_live_answer"] for row in rows]
    payload = {
        "schema_version": "souldrive_graphrag_acceptance_v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "purpose": "同一工作区、同一查询、同一模型条件下的 GraphRAG 路由与证据闭环验收，不是竞赛成绩",
        "workspace": str(workspace.root_path),
        "project_id": args.project_id,
        "models": models,
        "index": index_diagnostics,
        "runs": rows,
        "acceptance": {
            "index_ready": bool(index_diagnostics.get("ready")),
            "community_target_ok": bool(index_diagnostics.get("target_range_ok")),
            "candidate_all_completed": all(item["status"] == "pass" for item in candidate_rows),
            "candidate_no_refusal": all(not item["refusal"] for item in candidate_rows),
            "candidate_all_cited": all(item["has_citation"] for item in candidate_rows),
            "global_source_diversity_satisfied": all(
                row["route"].get("mode") != "global"
                or row["candidate_live_answer"].get("global_source_gate", {}).get("satisfied")
                for row in rows
            ),
            "global_route_enabled": any(
                row["route"].get("mode") == "global"
                and row["candidate_live_answer"].get("graph", {}).get("enabled")
                for row in rows
            ),
            "local_route_enabled": any(
                row["route"].get("mode") == "local"
                and row["candidate_live_answer"].get("graph", {}).get("enabled")
                for row in rows
            ),
            "ordinary_route_keeps_hybrid": any(
                row["route"].get("mode") == "hybrid"
                and not row["route"].get("use_graph")
                and not row["candidate_live_answer"].get("graph", {}).get("enabled")
                for row in rows
            ),
            "project_scope_requested": bool(args.project_id),
            "project_index_ready": (
                not bool(args.project_id)
                or (
                    bool(index_diagnostics.get("ready"))
                    and index_diagnostics.get("project_id") == args.project_id
                )
            ),
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["acceptance"], ensure_ascii=True, indent=2))
    return 0 if acceptance_passes(payload["acceptance"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
