from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import mcp_server
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace
import core.runtime_state as runtime_state


def _measurement_catalog(tmp_path):
    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    catalog = ProjectCatalog(workspace)
    catalog.create_project("实验项目", project_id="project")
    experiment_id = catalog.create_experiment("project", "alignment", experiment_id="experiment")
    run_id = catalog.create_experiment_run(
        experiment_id,
        "2026-08-30T00:00:00+00:00",
        run_id="run",
        dataset_version="dataset-v1",
        evaluation_script_ref="eval-v1",
    )
    metric_id = catalog.create_metric_definition(
        "accuracy", "%", "higher", "accuracy-v1", metric_id="metric"
    )
    return workspace, catalog, run_id, metric_id


def test_measurement_replay_is_idempotent_but_conflicting_write_cannot_rewrite_run(tmp_path):
    _workspace, catalog, run_id, metric_id = _measurement_catalog(tmp_path)
    try:
        catalog.record_measurement(run_id, metric_id, 0.81, "test", source_locator="row:2")
        catalog.record_measurement(run_id, metric_id, 0.81, "test", source_locator="row:2")

        with pytest.raises(ValueError, match="measurement is immutable"):
            catalog.record_measurement(run_id, metric_id, 0.92, "test", source_locator="row:3")

        current = catalog.get_experiment_run(run_id)
        revisions = catalog.list_measurement_revisions(run_id, metric_id, split="test")
        assert current["measurements"] == [{
            "metric_id": metric_id,
            "name": "accuracy",
            "unit": "%",
            "direction": "higher",
            "definition_version": "accuracy-v1",
            "value": 0.81,
            "split": "test",
            "sample_count": None,
            "variance": None,
            "source_locator": "row:2",
        }]
        assert len(revisions) == 1
        assert revisions[0]["revision"] == 1
        assert revisions[0]["value"] == 0.81
        assert revisions[0]["source_locator"] == "row:2"
    finally:
        catalog.close()


def test_legacy_measurement_is_backfilled_as_one_revision_on_catalog_reopen(tmp_path):
    workspace, catalog, run_id, metric_id = _measurement_catalog(tmp_path)
    catalog.conn.execute(
        "INSERT INTO measurements(run_id, metric_id, value, split, source_locator) VALUES (?, ?, ?, ?, ?)",
        (run_id, metric_id, 0.81, "test", "legacy:row:2"),
    )
    catalog.conn.commit()
    catalog.close()

    reopened = ProjectCatalog(workspace)
    try:
        revisions = reopened.list_measurement_revisions(run_id, metric_id)
        assert len(revisions) == 1
        assert revisions[0]["revision"] == 1
        assert revisions[0]["source_locator"] == "legacy:row:2"
    finally:
        reopened.close()


def test_measurement_versions_endpoint_is_project_scoped(tmp_path):
    workspace, catalog, run_id, metric_id = _measurement_catalog(tmp_path)
    catalog.record_measurement(run_id, metric_id, 0.81, "test", source_locator="row:2")
    catalog.close()

    state_path = str(Path(tmp_path) / "runtime_state.json")
    headers = {"X-SoulDrive-Token": "test-token"}
    with patch.object(runtime_state, "STATE_PATH", state_path), patch(
        "core.mcp_server.current_workspace", return_value=workspace
    ), patch.dict(
        "os.environ",
        {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
        clear=False,
    ):
        runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
        runtime_state.mark_software_unlocked()
        client = TestClient(mcp_server.app)
        response = client.get(
            f"/projects/project/experiments/runs/{run_id}/measurements/{metric_id}/versions",
            headers=headers,
        )
        foreign = client.get(
            f"/projects/other/experiments/runs/{run_id}/measurements/{metric_id}/versions",
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "souldrive_measurement_versions_v0.1"
    assert payload["immutable"] is True
    assert payload["current_revision"] == 1
    assert payload["revisions"][0]["source_locator"] == "row:2"
    assert foreign.status_code == 404
