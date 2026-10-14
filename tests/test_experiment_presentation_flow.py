"""Engineering-only ledger -> confirmed presentation contracts, not quality gold."""
import hashlib
import json
from pathlib import Path

import pytest

from core.mcp_server import PresentationGenerateRequest
from core.presentation_service import PresentationService
from core.project_catalog import ProjectCatalog
from core.task_journal import TaskJournal, TaskPublicError
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


@pytest.mark.parametrize("max_slides", [4, 5])
def test_comparison_outline_never_silently_drops_selected_measurements(max_slides):
    comparison = {"status": "comparable", "compatibility": {"change_computation": "enabled"},
                  "baseline": {}, "current": {}, "changes": [
                      {"metric": f"metric-{index}", "unit": "%", "baseline_value": 0.81,
                       "current_value": 0.86, "absolute_delta": 0.05} for index in range(5)]}
    inputs = {"experiment_comparison": comparison, "source_filenames": ["baseline.csv", "current.csv"],
              "max_slides": max_slides}
    outline = {"slides": [{"kind": "cover"}, {"kind": "sources"}]}
    if max_slides == 4:
        with pytest.raises(TaskPublicError, match="页数上限"):
            PresentationService._with_experiment_summary(outline, inputs)
    else:
        planned = PresentationService._with_experiment_summary(outline, inputs)
        text = json.dumps(planned, ensure_ascii=False)
        assert len(planned["slides"]) <= max_slides
        assert all(f"[M{index}]" in text for index in range(1, 6))


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


def test_comparison_outline_does_not_retain_model_invented_environment_changes(comparison_presentation):
    _, service, body, _, _ = comparison_presentation
    calls = []

    def invented_outline(_records, _inputs):
        calls.append(True)
        return {"title": "比较", "outline_source": "local_ollama", "slides": [
            {"kind": "cover", "title": "比较"},
            {"kind": "content", "title": "测试环境一致性确认", "points": ["硬件环境发生变动"],
             "sources": body["source_filenames"]},
            {"kind": "sources", "title": "来源", "sources": body["source_filenames"]},
        ]}

    service.outline_generator = invented_outline
    planned = service.run_task(service.create_plan(body)["task_id"])
    assert planned["status"] == "completed", planned
    outline = planned["result"]["outline"]
    assert "硬件环境发生变动" not in json.dumps(outline, ensure_ascii=False)
    assert calls == [], "comparison facts must not depend on free-form model claims"
    assert outline["outline_source"] == "experiment_ledger_extractive"
    text = json.dumps(outline, ensure_ascii=False)
    assert "[M1]" in text
    assert "环境引用：基线 env-a；当前 env-a" in text
    assert "配置引用：基线 config-a；当前 config-b" in text
    assert outline["title"] == body["title"]


def test_ledger_outline_does_not_parse_unused_wide_table_excerpts(comparison_presentation):
    _, service, body, records, _ = comparison_presentation
    records[0]["content"] = "| metric | value | note |\n| --- | --- | --- |\n| accuracy | 0.81 | " + "engineering-note-" * 12 + " |"
    planned = service.run_task(service.create_plan(body)["task_id"])
    assert planned["status"] == "completed", planned
    outline = planned["result"]["outline"]
    assert outline["outline_source"] == "experiment_ledger_extractive"
    text = json.dumps(outline, ensure_ascii=False)
    assert "0.81" in text and "0.86" in text and "0.05" in text
    assert "engineering-note" not in text


def test_comparison_outline_rejects_an_incomplete_evidence_budget():
    comparison = {"status": "comparable", "compatibility": {"change_computation": "enabled"},
                  "baseline": {}, "current": {}, "changes": [
                      {"metric": f"metric-{index}", "unit": "%", "baseline_value": 0.81,
                       "current_value": 0.86, "absolute_delta": 0.05} for index in range(9)]}
    with pytest.raises(TaskPublicError, match="完整证据预算"):
        PresentationService._with_experiment_summary({"slides": [{"kind": "cover"}, {"kind": "sources"}]},
            {"experiment_comparison": comparison, "source_filenames": ["baseline.csv", "current.csv"], "max_slides": 16})


@pytest.mark.parametrize("stage", ["confirm", "resume"])
def test_legacy_comparison_outline_requires_new_review(comparison_presentation, monkeypatch, stage):
    _, service, body, _, _ = comparison_presentation
    original_builder = service._build_outline_step

    def legacy_builder(inputs, outputs, context=None):
        outline = original_builder(inputs, outputs, context)
        return {**outline, "outline_source": "local_ollama"}

    monkeypatch.setattr(service, "_build_outline_step", legacy_builder)
    planned = service.run_task(service.create_plan(body)["task_id"])
    assert planned["status"] == "completed", planned
    if stage == "resume":
        # Reproduce a task confirmed by the older implementation, not an
        # unconfirmed artifact which the pre-existing confirmation gate rejects.
        with monkeypatch.context() as legacy:
            legacy.setattr(service, "_require_experiment_outline", lambda *_args: None, raising=False)
            generation = service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
        journal = TaskJournal(service.workspace, service.keys)
        try:
            generation_inputs = journal.get_task(generation["task_id"])["inputs"]
        finally:
            journal.close()
    with pytest.raises(ValueError, match="实验比较大纲.*重新"):
        if stage == "confirm":
            service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
        else:
            service._load_confirmed_plan_step(generation_inputs, {})


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
