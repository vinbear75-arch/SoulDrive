"""Engineering-only ledger -> confirmed presentation contracts, not quality gold."""
import hashlib
import json
from pathlib import Path

import pytest

from core.mcp_server import PresentationGenerateRequest
from core.presentation_service import PresentationService
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys


@pytest.fixture
def comparison_presentation(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    keys = derive_workspace_keys(b"p" * 32)
    catalog = ProjectCatalog(workspace)
    records, run_ids = [], []
    try:
        catalog.create_project("engineering", project_id="project")
        experiment_id = catalog.create_experiment("project", "alignment")
        metric_id = catalog.create_metric_definition("accuracy", "%", "higher", "metric-v1")
        for role, value, performed_at in (("baseline", 0.81, "2026-09-12T10:00:00+08:00"), ("current", 0.86, "2026-10-12T10:00:00+08:00")):
            name = f"{role}.csv"
            payload = f"metric,value\naccuracy,{value}\n".encode("utf-8")
            source = tmp_path / name
            source.write_bytes(payload)
            binding = catalog.register_imported_document("project", role, hashlib.md5(payload).hexdigest(), name)
            run_id = catalog.create_experiment_run(experiment_id, performed_at, evidence_version_id=binding["version_id"],
                                                   dataset_version="dataset-v1", evaluation_script_ref="eval-v1",
                                                   config_ref="config-a" if role == "baseline" else "config-b", environment_ref="env-a")
            catalog.record_measurement(run_id, metric_id, value, "test", source_locator="row:1")
            run_ids.append(run_id)
            records.append({"parent_id": role, "document_id": role, "source_filename": name,
                            "project_id": "project", "asset_id": binding["asset_id"],
                            "asset_version_id": binding["version_id"], "content": payload.decode("utf-8"),
                            "section": "metrics", "page_number": 0, "parent_index": 0})
    finally:
        catalog.close()
    monkeypatch.setattr("core.presentation_service.collect_indexed_evidence", lambda *_args, **_kwargs: records)
    body = {"project_id": "project", "request_id": "comparison-plan", "source_filenames": [item["source_filename"] for item in records],
            "source_document_ids": [item["document_id"] for item in records],
            "baseline_run_id": run_ids[0], "current_run_id": run_ids[1],
            "title": "工程实验比较", "max_slides": 4, "open_in_wps": False}
    return workspace, PresentationService(workspace, keys), body, records, metric_id


def test_presentation_api_preserves_explicit_run_pair():
    parsed = PresentationGenerateRequest(source_filenames=["baseline.csv", "current.csv"], project_id="project",
                                         baseline_run_id="run-baseline", current_run_id="run-current")
    assert parsed.model_dump().get("baseline_run_id") == "run-baseline"
    assert parsed.model_dump().get("current_run_id") == "run-current"


def test_comparison_slide_preserves_existing_ledger_display_precision():
    comparison = {"status": "comparable", "compatibility": {"change_computation": "enabled"},
                  "baseline": {}, "current": {}, "changes": [{"metric": "accuracy", "unit": "%",
                  "baseline_value": 0.123456789, "current_value": 0.223456789, "absolute_delta": 0.1}]}
    outline = PresentationService._with_experiment_summary({"slides": [{"kind": "cover"}, {"kind": "sources"}]},
                                                         {"experiment_comparison": comparison, "source_filenames": ["baseline.csv", "current.csv"], "max_slides": 4})
    text = json.dumps(outline, ensure_ascii=False)
    assert "0.123456789%" in text and "0.223456789%" in text


def test_comparison_plan_contains_server_measurements_and_rejects_ledger_drift(comparison_presentation):
    workspace, service, body, _, metric_id = comparison_presentation
    task = service.create_plan(body)
    planned = service.run_task(task["task_id"])
    assert planned["status"] == "completed", planned
    result = planned["result"]
    assert result["experiment_comparison"]["changes"][0]["absolute_delta"] == 0.05
    assert result["measurement_evidence"][0]["baseline_source"]["run_id"] == body["baseline_run_id"]
    outline = json.dumps(result["outline"], ensure_ascii=False)
    assert "0.81" in outline and "0.86" in outline and "0.05" in outline and "[M1]" in outline
    assert "因果" in outline
    assert service.create_plan(body)["task_id"] == task["task_id"]
    catalog = ProjectCatalog(workspace)
    try:
        # Simulate out-of-band ledger drift; the public write API is immutable.
        catalog.conn.execute("UPDATE measurements SET value=0.87 WHERE run_id=? AND metric_id=?", (body["current_run_id"], metric_id))
        catalog.conn.commit()
    finally:
        catalog.close()
    with pytest.raises(ValueError, match="实验.*变化|测量.*变化"):
        service.confirm_plan(task["task_id"], result["plan_digest"])


@pytest.mark.parametrize("change", ["missing_pair", "foreign_project", "incompatible"])
def test_invalid_comparison_never_creates_plan(comparison_presentation, change):
    workspace, service, body, _, _ = comparison_presentation
    if change == "missing_pair":
        body.pop("current_run_id")
    elif change == "foreign_project":
        body["project_id"] = "other-project"
    else:
        catalog = ProjectCatalog(workspace)
        try:
            catalog.conn.execute("UPDATE experiment_runs SET dataset_version='dataset-v2' WHERE run_id=?", (body["current_run_id"],))
            catalog.conn.commit()
        finally:
            catalog.close()
    with pytest.raises(ValueError):
        service.create_plan(body)


def test_comparison_plan_rejects_wrong_selected_source_version(comparison_presentation):
    _, service, body, records, _ = comparison_presentation
    records[1]["asset_version_id"] = "unrelated-version"
    planned = service.run_task(service.create_plan(body)["task_id"])
    assert planned["status"] == "failed"


def test_generation_rejects_out_of_band_ledger_drift_after_confirmation(comparison_presentation):
    workspace, service, body, _, metric_id = comparison_presentation
    planned = service.run_task(service.create_plan(body)["task_id"])
    generation = service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
    catalog = ProjectCatalog(workspace)
    try:
        catalog.conn.execute("UPDATE measurements SET value=0.87 WHERE run_id=? AND metric_id=?", (body["current_run_id"], metric_id))
        catalog.conn.commit()
    finally:
        catalog.close()
    generated = service.run_task(generation["task_id"])
    assert generated["status"] == "failed"
    assert list((Path(workspace.root_path) / "data" / "presentations").glob("*.pptx")) == []


def test_confirmed_comparison_generates_once_with_locatable_measurements(comparison_presentation):
    _, service, body, _, _ = comparison_presentation
    planned = service.run_task(service.create_plan(body)["task_id"])
    generation = service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
    assert service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])["task_id"] == generation["task_id"]
    generated = service.run_task(generation["task_id"])
    assert generated["status"] == "completed", generated
    assert generated["result"]["experiment_comparison"] == planned["result"]["experiment_comparison"]
    assert generated["result"]["measurement_evidence"][0]["current_source"]["source_locator"] == "row:1"
    assert service.run_task(generation["task_id"])["result"]["output_path"] == generated["result"]["output_path"]
