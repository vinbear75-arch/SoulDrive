from unittest.mock import Mock

from core.audit_log import AuditLogger
from core.workspace import SoulDriveWorkspace
from scripts import eval_graphrag_acceptance, eval_rag


def _audit_fixture(tmp_path, monkeypatch):
    global_audit = AuditLogger(tmp_path / "shared" / "audit.jsonl", tmp_path / "shared" / "state.json")
    global_audit.append_event("existing.application.event", {"fixture": True})
    before_log = (tmp_path / "shared" / "audit.jsonl").read_bytes()
    before_state = (tmp_path / "shared" / "state.json").read_bytes()

    class Engine:
        def __init__(self, *, audit_logger=None, **_kwargs):
            self.audit_logger = audit_logger or global_audit
            self.audit_logger.append_event("evaluation.fixture", {"fixture": True})

        def close(self):
            pass

    monkeypatch.setattr("core.rag_engine.RAGEngine", Engine)
    monkeypatch.setattr("core.knowledge_base.LocalKnowledgeBase", Mock())
    workspace = SoulDriveWorkspace(str(tmp_path / "evaluation")).ensure()
    return workspace, before_log, before_state


def _assert_isolated(workspace, tmp_path, before_log, before_state):
    assert (tmp_path / "shared" / "audit.jsonl").read_bytes() == before_log
    assert (tmp_path / "shared" / "state.json").read_bytes() == before_state
    audit = AuditLogger.for_workspace(workspace)
    assert [event["event_type"] for event in audit.read_recent()] == ["evaluation.fixture"]
    assert audit.verify_chain()["ready"] is True


def test_answer_evaluation_binds_audit_to_the_explicit_workspace(tmp_path, monkeypatch):
    workspace, before_log, before_state = _audit_fixture(tmp_path, monkeypatch)
    assert eval_rag.run_answer_generation([], workspace.root_path, 6) == []
    _assert_isolated(workspace, tmp_path, before_log, before_state)


def test_graph_acceptance_binds_audit_to_the_explicit_workspace(tmp_path, monkeypatch):
    workspace, before_log, before_state = _audit_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(eval_graphrag_acceptance, "configure_local_ollama", lambda: None)
    monkeypatch.setenv("SOULDRIVE_WORKSPACE", "original-fixture-workspace")
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
    assert eval_graphrag_acceptance.main([
        "--workspace", workspace.root_path, "--output", str(tmp_path / "result.json"),
    ]) == 0
    _assert_isolated(workspace, tmp_path, before_log, before_state)
