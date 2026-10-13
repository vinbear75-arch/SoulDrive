import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import mcp_server, runtime_state
from core.experiment_ledger import import_metric_source
from core.project_catalog import ProjectCatalog
from core.secure_document_store import SecureDocumentStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys


DEFAULTS = {
    "performed_at": "2026-09-12T10:00:00+08:00", "dataset_version": "dataset-v1",
    "config_ref": "config-a", "evaluation_script_ref": "eval-v1", "environment_ref": "env-a",
    "unit": "%", "direction": "higher", "definition_version": "metric-v1", "split": "test",
}


@pytest.fixture
def metric_sources(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    keys = derive_workspace_keys(b"m" * 32)
    set_workspace_keys(workspace.root_path, keys)
    store, catalog = SecureDocumentStore(workspace, keys), ProjectCatalog(workspace)
    ids, versions = {}, {}
    try:
        for project_id in ("project-a", "project-b"):
            catalog.create_project(project_id, project_id=project_id)
        for suffix, value, name in (("a", "0.81", "metrics.csv"), ("b", "9.0", "metrics.csv"), ("other", "0.86", "other.csv")):
            project_id = "project-b" if suffix == "b" else "project-a"
            payload = f"metric,value\naccuracy,{value}\n".encode("utf-8")
            binding = catalog.register_imported_document(project_id, name, hashlib.md5(payload).hexdigest(), name)
            store.import_document_bytes(name, payload)
            ids[suffix] = next(item["object_id"] for item in store.iter_documents() if store.read_document_bytes(item["object_id"]) == payload)
            versions[suffix] = binding["version_id"]
    finally:
        store.close()
        catalog.close()
    monkeypatch.setattr(runtime_state, "STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "metric-test-token")
    runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
    runtime_state.mark_software_unlocked()
    try:
        yield workspace, keys, ids, versions, TestClient(mcp_server.app)
    finally:
        clear_workspace_keys(workspace.root_path)


def test_metric_preview_scopes_project_before_same_name_resolution(metric_sources):
    workspace, _, ids, versions, client = metric_sources
    response = client.post("/projects/project-a/experiments/metrics", headers={"X-SoulDrive-Token": "metric-test-token"}, json={
        "source_name": "metrics.csv", "experiment_name": "alignment", **DEFAULTS,
    })
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "confirmation_required"
    assert response.json()["source_version_id"] == versions["a"]
    assert response.json()["source_document_id"] == ids["a"]
    assert response.json()["plan"]["rows"][0]["value"] == 0.81
    catalog = ProjectCatalog(workspace)
    try:
        assert catalog.list_experiment_runs("project-a") == []
    finally:
        catalog.close()


def test_explicit_metric_object_binds_matching_version_not_latest_name(metric_sources):
    workspace, keys, ids, versions, client = metric_sources
    catalog, store = ProjectCatalog(workspace), SecureDocumentStore(workspace, keys)
    try:
        payload = b"metric,value\naccuracy,0.82\n"
        store.import_document_bytes("metrics.csv", payload)
        later = catalog.register_imported_document("project-a", "metrics", hashlib.md5(payload).hexdigest(), "metrics.csv")
        assert later["version_id"] != versions["a"]
    finally:
        store.close()
        catalog.close()
    body = {"source_name": "metrics.csv", "source_document_id": ids["a"], "source_version_id": versions["a"],
            "experiment_name": "alignment", "confirmed": True, **DEFAULTS}
    first = client.post("/projects/project-a/experiments/metrics", headers={"X-SoulDrive-Token": "metric-test-token"}, json=body)
    assert first.status_code == 200, first.json()
    assert first.json()["source_version_id"] == versions["a"]
    replay = client.post("/projects/project-a/experiments/metrics", headers={"X-SoulDrive-Token": "metric-test-token"}, json=body)
    assert replay.status_code == 200 and replay.json()["run_id"] == first.json()["run_id"]
    catalog = ProjectCatalog(workspace)
    try:
        runs = catalog.list_experiment_runs("project-a")
        assert len(runs) == 1
        assert catalog.get_experiment_run(first.json()["run_id"])["measurements"][0]["value"] == 0.81
    finally:
        catalog.close()


@pytest.mark.parametrize("selection,status", [("foreign", 409), ("missing", 404), ("label", 409), ("version", 409)])
def test_metric_selection_mismatch_never_writes(metric_sources, selection, status):
    workspace, _, ids, versions, client = metric_sources
    body = {"source_name": "metrics.csv", "source_document_id": ids["a"], "source_version_id": versions["a"],
            "experiment_name": "alignment", "confirmed": True, **DEFAULTS}
    if selection == "foreign":
        body["source_document_id"] = ids["b"]
    elif selection == "missing":
        body["source_document_id"] = "missing-object"
    elif selection == "label":
        body["source_name"] = "other.csv"
    else:
        body["source_name"] = "other.csv"
        body["source_document_id"] = ids["other"]
        body["source_version_id"] = versions["a"]
    response = client.post("/projects/project-a/experiments/metrics", headers={"X-SoulDrive-Token": "metric-test-token"}, json=body)
    assert response.status_code == status, response.json()
    catalog = ProjectCatalog(workspace)
    try:
        assert catalog.list_experiment_runs("project-a") == []
    finally:
        catalog.close()


def test_metric_import_shared_service_rejects_wrong_source_bytes(metric_sources, tmp_path):
    workspace, _, _, versions, _ = metric_sources
    wrong = tmp_path / "wrong.csv"
    wrong.write_bytes(b"metric,value\naccuracy,0.86\n")
    catalog = ProjectCatalog(workspace)
    try:
        with pytest.raises(ValueError, match="source.*version|version.*source"):
            import_metric_source(catalog, project_id="project-a", evidence_version_id=versions["a"],
                                 path=wrong, experiment_name="alignment", confirmed=True, **DEFAULTS)
        assert catalog.list_experiment_runs("project-a") == []
    finally:
        catalog.close()


def test_metric_import_shared_service_accepts_sha256_version(metric_sources, tmp_path):
    workspace, _, _, _, _ = metric_sources
    source = tmp_path / "sha256.csv"
    source.write_bytes(b"metric,value\naccuracy,0.81\n")
    catalog = ProjectCatalog(workspace)
    try:
        binding = catalog.register_imported_document("project-a", "sha256", hashlib.sha256(source.read_bytes()).hexdigest(), "sha256.csv")
        result = import_metric_source(catalog, project_id="project-a", evidence_version_id=binding["version_id"],
                                      path=source, experiment_name="alignment", confirmed=True, **DEFAULTS)
        assert result["status"] == "imported"
        assert len(catalog.list_experiment_runs("project-a")) == 1
    finally:
        catalog.close()


def test_explicit_document_id_does_not_fall_back_without_keys(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    (Path(workspace.papers_path) / "metrics.csv").write_bytes(b"metric,value\naccuracy,0.81\n")
    monkeypatch.setattr(mcp_server, "get_workspace_keys", lambda _: None)
    payload, _, _, _ = mcp_server._document_payload_by_selector(workspace, name="metrics.csv", document_id="missing-object")
    assert payload is None


def test_document_selector_rejects_workspace_path_escape(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    outside = Path(workspace.papers_path).parent / "outside.csv"
    outside.write_bytes(b"private outside-workspace bytes")
    monkeypatch.setattr(mcp_server, "get_workspace_keys", lambda _: None)
    payload, error, _, _ = mcp_server._document_payload_by_selector(workspace, name="../outside.csv")
    assert payload is None and error
