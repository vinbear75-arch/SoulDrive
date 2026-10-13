from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from core import mcp_server
from core.enterprise_security import SlidingWindowRateLimiter
from core.experiment_ledger import build_experiment_evidence, compare_experiment_runs
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace


@pytest.fixture
def run_pair(tmp_path):
    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    catalog = ProjectCatalog(workspace)
    try:
        catalog.create_project("合成实验项目", project_id="project")
        catalog.create_project("另一个项目", project_id="other")
        experiment_id = catalog.create_experiment("project", "运行身份检查")
        metric_id = catalog.create_metric_definition("synthetic_score", "points", "higher", "v1")
        for run_id, performed_at in (
            ("baseline", "2026-09-01T10:00:00+08:00"),
            ("current", "2026-10-01T10:00:00+08:00"),
        ):
            catalog.create_experiment_run(
                experiment_id,
                performed_at,
                run_id=run_id,
                dataset_version="synthetic-v1",
                config_ref="synthetic-config",
                evaluation_script_ref="synthetic-eval-v1",
                environment_ref="synthetic-environment",
            )
            catalog.record_measurement(
                run_id, metric_id, 1.0, "synthetic", source_locator=f"synthetic:{run_id}"
            )
        yield workspace, catalog
    finally:
        catalog.close()


def test_same_run_is_not_a_two_run_comparison_or_measurement_evidence(run_pair):
    _workspace, catalog = run_pair
    result = compare_experiment_runs(catalog, "baseline", "baseline", project_id="project")

    assert result["status"] == "not_comparable"
    assert "不同" in result["reason"]
    assert result["changes"] == []
    assert result["compatibility"]["change_computation"] == "blocked"
    assert build_experiment_evidence(result) == []


def test_distinct_runs_with_equal_values_remain_comparable(run_pair):
    _workspace, catalog = run_pair
    result = compare_experiment_runs(catalog, "baseline", "current", project_id="project")

    assert result["status"] == "comparable"
    assert result["changes"][0]["absolute_delta"] == 0.0
    assert result["changes"][0]["assessment"] == "unchanged"
    evidence = build_experiment_evidence(result)
    assert len(evidence) == 1
    assert evidence[0]["baseline_source"]["run_id"] == "baseline"
    assert evidence[0]["current_source"]["run_id"] == "current"


def test_project_scope_is_checked_before_same_run_identity(run_pair):
    _workspace, catalog = run_pair
    result = compare_experiment_runs(catalog, "baseline", "baseline", project_id="other")

    assert result["status"] == "rejected"
    assert result["reason"] == "cross_project"
    assert build_experiment_evidence(result) == []


@pytest.mark.parametrize("entrypoint", ["compare", "plan", "chat"])
def test_real_run_identity_gate_reaches_each_research_entrypoint(run_pair, monkeypatch, entrypoint):
    workspace, _catalog = run_pair
    generate = Mock(return_value=iter(["unexpected generation"]))
    load_runtime = Mock()
    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(
        mcp_server, "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(
        mcp_server, "current_audit_logger", lambda: SimpleNamespace(append_event=lambda *a, **k: None)
    )
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server, "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", load_runtime)
    monkeypatch.setattr(mcp_server, "rag", SimpleNamespace(generate_response_stream=generate))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "synthetic-identity-token")
    headers = {"X-SoulDrive-Token": "synthetic-identity-token"}
    selection = {"baseline_run_id": "baseline", "current_run_id": "baseline"}

    with TestClient(mcp_server.app) as client:
        if entrypoint == "compare":
            response = client.post("/projects/project/experiments/compare", headers=headers, json=selection)
            assert response.status_code == 200
            assert response.json()["status"] == "not_comparable"
            assert "不同" in response.json()["reason"]
        elif entrypoint == "plan":
            response = client.post(
                "/multimodal/workflows/plan", headers=headers,
                json={"workflow_id": "experiment_compare", "project_id": "project", **selection},
            )
            assert response.status_code == 200
            assert response.json()["ready"] is False
            assert "不同" in response.json()["next_action"]
        else:
            response = client.post(
                "/chat", headers=headers,
                json={
                    "query": "比较这两次实验",
                    "workflow_id": "experiment_compare",
                    "active_context": {"project_id": "project", **selection},
                },
            )
            assert response.status_code == 422
            assert response.json()["experiment_comparison"]["status"] == "not_comparable"
            assert "不同" in response.json()["experiment_comparison"]["reason"]
    load_runtime.assert_not_called()
    generate.assert_not_called()
