import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import eval_graphrag_acceptance


class _FakeGraphRag:
    def diagnostics(self, *, project_id=None):
        return {
            "ready": True,
            "project_id": project_id,
            "scope_freshness": "verified",
        }


class _FakeRag:
    graph_rag = _FakeGraphRag()


def test_acceptance_uses_runtime_graph_rag_scope_provider():
    diagnostics = eval_graphrag_acceptance.graph_index_diagnostics(
        _FakeRag(),
        project_id="project-a",
    )

    assert diagnostics["ready"] is True
    assert diagnostics["project_id"] == "project-a"
    assert diagnostics["scope_freshness"] == "verified"


def test_unscoped_acceptance_does_not_require_project_scope_request():
    assert eval_graphrag_acceptance.acceptance_passes(
        {
            "index_ready": True,
            "community_target_ok": True,
            "candidate_all_completed": True,
            "candidate_no_refusal": True,
            "candidate_all_cited": True,
            "global_route_enabled": True,
            "local_route_enabled": True,
            "ordinary_route_keeps_hybrid": True,
            "project_scope_requested": False,
            "project_index_ready": True,
        }
    )


def test_evidence_summary_keeps_same_named_sources_separate_by_scope():
    summary = eval_graphrag_acceptance.evidence_summary(
        [
            {
                "id": "chunk-a",
                "source_filename": "report.pdf",
                "project_id": "project-a",
                "document_id": "doc-a",
            },
            {
                "id": "chunk-b",
                "source_filename": "report.pdf",
                "project_id": "project-b",
                "document_id": "doc-b",
            },
        ]
    )

    assert summary["source_count"] == 2
    assert summary["sources"] == ["project-a/report.pdf", "project-b/report.pdf"]


def test_evidence_summary_detects_derived_graph_report_as_non_raw_source():
    summary = eval_graphrag_acceptance.evidence_summary(
        [{"id": "graph-c1", "source_filename": "GraphRAG社区报告/C1"}]
    )

    assert summary["raw_sources_only"] is False


def test_citation_binding_rejects_unknown_evidence_ids():
    assert eval_graphrag_acceptance.citations_are_bound(
        "结论 [E1]",
        ["E1", "E2"],
    )
    assert not eval_graphrag_acceptance.citations_are_bound(
        "结论 [E9]",
        ["E1", "E2"],
    )
    assert not eval_graphrag_acceptance.citations_are_bound(
        "没有引用的结论",
        ["E1", "E2"],
    )


def test_final_retrieval_evidence_ids_follow_a_used_retry():
    class _Audit:
        def read_recent(self, *, limit):
            assert limit == 200
            return [
                {
                    "event_type": "rag.retrieved",
                    "trace_id": "trace-1",
                    "payload": {"evidence_ids": ["E1"]},
                },
                {
                    "event_type": "rag.retrieval_retry",
                    "trace_id": "trace-1",
                    "payload": {"used": True, "evidence_ids": ["E2"]},
                },
            ]

    assert eval_graphrag_acceptance.final_retrieval_evidence_ids(
        _Audit(),
        "trace-1",
    ) == ["E2"]


@pytest.mark.parametrize("event_type,path", [
    ("rag.completed", "model_generated"),
    ("rag.refused_invalid_answer", "validation_fallback"),
    ("rag.answer_quality_fallback", "quality_fallback"),
    ("rag.extractive_lookup", "extractive_lookup"),
])
def test_answer_execution_records_the_actual_terminal_path(event_type, path):
    class Audit:
        def read_recent(self, *, limit):
            assert limit == 200
            return [{
                "event_type": event_type, "trace_id": "trace-current",
                "payload": {"answer_attempts": 2, "validation": {"reason": "missing citation"}},
            }]

    execution = eval_graphrag_acceptance.answer_execution(Audit(), "trace-current")
    assert execution["path"] == path
    assert execution["event_type"] == event_type
    assert execution["answer_attempts"] == 2
    assert execution["validation_reason"] == "missing citation"


def test_answer_execution_does_not_adopt_another_trace_or_retrieval_success():
    class Audit:
        def read_recent(self, *, limit):
            return [
                {"event_type": "rag.retrieved", "trace_id": "trace-current", "payload": {}},
                {"event_type": "rag.completed", "trace_id": "trace-other", "payload": {}},
            ]

    assert eval_graphrag_acceptance.answer_execution(Audit(), "trace-current") == {
        "path": "unknown", "event_type": None, "answer_attempts": None, "validation_reason": None,
    }


def test_answer_execution_uses_the_latest_terminal_event_for_its_trace():
    class Audit:
        def read_recent(self, *, limit):
            return [
                {"event_type": "rag.refused_invalid_answer", "trace_id": "trace-current", "payload": {}},
                {"event_type": "rag.completed", "trace_id": "trace-current", "payload": {}},
            ]

    assert eval_graphrag_acceptance.answer_execution(Audit(), "trace-current")["path"] == "model_generated"


@pytest.mark.parametrize("llm,expected_chat,embedding_provider", [
    (SimpleNamespace(model="actual-ollama:9b"), "actual-ollama:9b", "sentence-transformers"),
    (SimpleNamespace(model_name="actual-compatible"), "actual-compatible", "sentence-transformers"),
    (SimpleNamespace(model_path=Path("local-models/actually-loaded.gguf")), "actually-loaded.gguf", "sentence-transformers"),
    (SimpleNamespace(), None, "sentence-transformers"),
    (SimpleNamespace(model="actual-ollama:9b"), "actual-ollama:9b", "ollama"),
    (SimpleNamespace(), None, None),
])
def test_main_records_resolved_models_before_clients_are_closed(tmp_path, monkeypatch, llm, expected_chat, embedding_provider):
    kb = SimpleNamespace(close=Mock())
    if embedding_provider:
        kb.embedding_model_name = "actual-local-embedding"
    if embedding_provider == "sentence-transformers":
        kb._embedding_model_path = Path("local-models/actual-local-embedding")
    elif embedding_provider == "ollama":
        from core.knowledge_base import _OllamaEmbeddingModel

        kb.embedding_model = _OllamaEmbeddingModel("actual-local-embedding", "http://fixture.invalid")
    rag = SimpleNamespace(llm=llm)
    rag.close = Mock(side_effect=lambda: setattr(rag, "llm", None))
    monkeypatch.setattr("core.knowledge_base.LocalKnowledgeBase", Mock(return_value=kb))
    monkeypatch.setattr("core.rag_engine.RAGEngine", Mock(return_value=rag))
    monkeypatch.setenv("SOULDRIVE_WORKSPACE", "original-fixture-workspace")
    monkeypatch.setenv("SOULDRIVE_OLLAMA_MODEL", "configured-but-not-loaded:9b")
    monkeypatch.setenv("SOULDRIVE_OLLAMA_EMBEDDING_MODEL", "configured-but-not-loaded-embedding")
    monkeypatch.setenv("SOULDRIVE_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(eval_graphrag_acceptance, "configure_local_ollama", lambda: None)
    monkeypatch.setattr(
        eval_graphrag_acceptance, "graph_index_diagnostics",
        lambda *args, **kwargs: {"ready": True, "target_range_ok": True},
    )
    modes = iter(("hybrid", "global", "local"))

    def run_query(**_kwargs):
        mode = next(modes)
        return {
            "route": {"mode": mode, "use_graph": mode != "hybrid"},
            "candidate_live_answer": {
                "status": "pass", "refusal": False, "has_citation": True,
                "global_source_gate": {"satisfied": True}, "graph": {"enabled": mode != "hybrid"},
            },
        }

    monkeypatch.setattr(eval_graphrag_acceptance, "run_query", run_query)
    output = tmp_path / "result.json"
    assert eval_graphrag_acceptance.main([
        "--workspace", str(tmp_path / "evaluation"), "--output", str(output),
    ]) == 0
    models = json.loads(output.read_text(encoding="utf-8"))["models"]
    assert models["chat"] == expected_chat
    assert models["embedding"] == ("actual-local-embedding" if embedding_provider else None)
    assert models["embedding_provider"] == embedding_provider
    assert "local-models" not in json.dumps(models)
    rag.close.assert_called_once()
    kb.close.assert_called_once()
