import zipfile
from datetime import datetime, timezone

import pytest

from core.experiment_ledger import (
    build_experiment_evidence,
    build_experiment_evidence_text,
    compare_experiment_runs,
    import_metric_source,
    parse_metric_source,
    plan_metric_import,
    previous_month_bounds,
    summarize_experiment_repeats,
)
from core.project_catalog import ProjectCatalog
from core.paper_importer import file_md5
from core.workspace import SoulDriveWorkspace


def _catalog(tmp_path, content_hash="hash-1"):
    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    catalog = ProjectCatalog(workspace)
    catalog.create_project("实验项目", project_id="project")
    catalog.create_asset("project", "指标文件", "metrics", asset_id="asset")
    catalog.create_asset_version("asset", content_hash, "metrics.csv", version_id="version-1")
    return workspace, catalog


def _defaults(**overrides):
    values = {
        "performed_at": "2026-07-12T10:00:00+08:00",
        "dataset_version": "dataset-v1",
        "config_ref": "config-a",
        "evaluation_script_ref": "eval-v1",
        "environment_ref": "env-a",
        "unit": "%",
        "direction": "higher",
        "definition_version": "metric-v1",
        "split": "test",
    }
    values.update(overrides)
    return values


def test_metric_import_requires_confirmation_and_required_provenance(tmp_path):
    source = tmp_path / "metrics.csv"
    source.write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")

    plan = plan_metric_import(source, experiment_name="baseline")

    assert plan["writes"] is False
    assert plan["requires_confirmation"] is False
    assert set(plan["missing_fields"]) >= {"unit", "split", "dataset_version", "evaluation_script_ref"}


def test_metric_import_and_comparison_are_traceable_and_gated(tmp_path):
    source = tmp_path / "metrics.csv"
    source.write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")
    workspace, catalog = _catalog(tmp_path, file_md5(source))
    try:
        baseline = import_metric_source(
            catalog,
            project_id="project",
            evidence_version_id="version-1",
            path=source,
            experiment_name="alignment",
            confirmed=True,
            **_defaults(),
        )
        assert baseline["status"] == "imported"
        run = catalog.get_experiment_run(baseline["run_id"])
        assert run["measurements"][0]["source_locator"] == "row:1"

        source.write_text("metric,value\naccuracy,0.86\n", encoding="utf-8")
        catalog.create_asset_version("asset", file_md5(source), "metrics-v2.csv", version_id="version-2")
        current = import_metric_source(
            catalog,
            project_id="project",
            evidence_version_id="version-2",
            path=source,
            experiment_name="alignment",
            confirmed=True,
            **_defaults(performed_at="2026-08-12T10:00:00+08:00", config_ref="config-b"),
        )

        comparison = compare_experiment_runs(catalog, baseline["run_id"], current["run_id"], project_id="project")
        assert comparison["status"] == "comparable"
        assert comparison["changes"][0]["absolute_delta"] == 0.05
        assert comparison["changes"][0]["current_source"]["source_locator"] == "row:1"
        assert comparison["factors"][0]["factor"] == "config_ref"
        assert "单因素" in comparison["next_validation"]

        repeated = summarize_experiment_repeats(
            catalog,
            [baseline["run_id"], current["run_id"]],
            project_id="project",
        )
        assert repeated["status"] == "not_comparable"
        assert repeated["reason"] == "mismatch"
        assert repeated["compatibility"]["config_ref"] is False
        assert repeated["metrics"] == []

        source.write_text("metric,value\naccuracy,0.87\n", encoding="utf-8")
        catalog.create_asset_version("asset", file_md5(source), "metrics-v3.csv", version_id="version-3")
        repeat = import_metric_source(
            catalog,
            project_id="project",
            evidence_version_id="version-3",
            path=source,
            experiment_name="alignment",
            confirmed=True,
            **_defaults(performed_at="2026-08-20T10:00:00+08:00"),
        )
        same_condition = summarize_experiment_repeats(
            catalog,
            [baseline["run_id"], repeat["run_id"]],
            project_id="project",
        )
        assert same_condition["status"] == "ready"
        assert same_condition["metrics"][0]["n"] == 2
        assert same_condition["metrics"][0]["mean"] == 0.84
        assert same_condition["metrics"][0]["variance"] == 0.0018
        assert same_condition["metrics"][0]["confidence_interval_95"]

        one_run = summarize_experiment_repeats(catalog, [baseline["run_id"]], project_id="project")
        assert one_run["status"] == "ready"
        assert one_run["metrics"][0]["n"] == 1
        assert one_run["metrics"][0]["variance"] is None
        assert one_run["metrics"][0]["confidence_interval_95"] is None

        source.write_text("metric,value\naccuracy,0.87\n", encoding="utf-8")
        catalog.create_asset_version("asset", file_md5(source), "metrics-v4.csv", version_id="version-4")
        blocked_run = import_metric_source(
            catalog,
            project_id="project",
            evidence_version_id="version-4",
            path=source,
            experiment_name="alignment",
            confirmed=True,
            **_defaults(
                performed_at="2026-08-20T10:00:00+08:00",
                dataset_version="dataset-v2",
                config_ref="config-c",
            ),
        )
        blocked = compare_experiment_runs(
            catalog,
            baseline["run_id"],
            blocked_run["run_id"],
            project_id="project",
        )
        assert blocked["status"] == "not_comparable"
        assert blocked["compatibility"]["dataset_version"] is False
        assert blocked["compatibility"]["change_computation"] == "blocked"
        assert blocked["changes"] == []
    finally:
        catalog.close()


def test_partial_metric_match_does_not_expose_changes(tmp_path):
    workspace, catalog = _catalog(tmp_path)
    try:
        experiment_id = catalog.create_experiment("project", "alignment")
        baseline_run = catalog.create_experiment_run(
            experiment_id,
            "2026-07-12T10:00:00+08:00",
            dataset_version="dataset-v1",
            evaluation_script_ref="eval-v1",
        )
        current_run = catalog.create_experiment_run(
            experiment_id,
            "2026-08-12T10:00:00+08:00",
            dataset_version="dataset-v1",
            evaluation_script_ref="eval-v1",
        )
        accuracy_id = catalog.create_metric_definition("accuracy", "%", "higher", "metric-v1")
        recall_id = catalog.create_metric_definition("recall", "%", "higher", "metric-v1")
        catalog.record_measurement(baseline_run, accuracy_id, 0.81, "test", source_locator="row:1")
        catalog.record_measurement(current_run, accuracy_id, 0.86, "test", source_locator="row:1")
        catalog.record_measurement(current_run, recall_id, 0.74, "test", source_locator="row:2")

        comparison = compare_experiment_runs(
            catalog,
            baseline_run,
            current_run,
            project_id="project",
        )

        assert comparison["status"] == "not_comparable"
        assert comparison["compatibility"]["matching_metric_count"] == 1
        assert comparison["compatibility"]["missing_or_incompatible_metric_count"] == 1
        assert comparison["compatibility"]["change_computation"] == "blocked"
        assert comparison["changes"] == []
    finally:
        catalog.close()


def test_repeat_summary_rejects_duplicate_ids_and_metric_keys():
    class FakeCatalog:
        def __init__(self):
            self.runs = {
                "run-a": {
                    "run_id": "run-a",
                    "project_id": "project",
                    "experiment_id": "experiment",
                    "experiment_name": "alignment",
                    "dataset_version": "dataset-v1",
                    "evaluation_script_ref": "eval-v1",
                    "config_ref": "config-a",
                    "environment_ref": "env-a",
                    "measurements": [
                        {"name": "accuracy", "unit": "%", "definition_version": "metric-v1", "split": "test", "direction": "higher", "value": 0.81},
                        {"name": "accuracy", "unit": "%", "definition_version": "metric-v1", "split": "test", "direction": "higher", "value": 0.82},
                    ],
                },
                "run-b": {
                    "run_id": "run-b",
                    "project_id": "project",
                    "experiment_id": "experiment",
                    "experiment_name": "alignment",
                    "dataset_version": "dataset-v1",
                    "evaluation_script_ref": "eval-v1",
                    "config_ref": "config-a",
                    "environment_ref": "env-a",
                    "measurements": [
                        {"name": "accuracy", "unit": "%", "definition_version": "metric-v1", "split": "test", "direction": "higher", "value": 0.83},
                    ],
                },
            }

        def get_experiment_run(self, run_id):
            return self.runs.get(run_id)

    catalog = FakeCatalog()
    duplicate_ids = summarize_experiment_repeats(catalog, ["run-a", "run-a"], project_id="project")
    assert duplicate_ids["status"] == "not_comparable"
    assert duplicate_ids["reason"] == "duplicate_run_ids"

    duplicate_metrics = summarize_experiment_repeats(catalog, ["run-a", "run-b"], project_id="project")
    assert duplicate_metrics["status"] == "not_comparable"
    assert duplicate_metrics["reason"] == "duplicate_metric_key"
    assert duplicate_metrics["metrics"] == []


def test_previous_month_uses_local_timezone_boundaries():
    bounds = previous_month_bounds("2026-08-29T09:00:00+08:00", "Asia/Shanghai")

    assert bounds["label"] == "2026-07"
    assert bounds["performed_from"] == "2026-06-30T16:00:00+00:00"
    assert bounds["performed_to"] == "2026-07-31T16:00:00+00:00"


def test_comparable_experiment_result_becomes_locatable_m_evidence():
    evidence = build_experiment_evidence({
        "schema_version": "souldrive_experiment_comparison_v0.1",
        "status": "comparable",
        "project_id": "project",
        "baseline": {
            "run_id": "run-baseline",
            "experiment_name": "alignment",
            "dataset_version": "dataset-v1",
            "evaluation_script_ref": "eval-v1",
            "evidence_title": "baseline.csv",
            "file_ref": "baseline.csv",
        },
        "current": {
            "run_id": "run-current",
            "experiment_name": "alignment",
            "dataset_version": "dataset-v1",
            "evaluation_script_ref": "eval-v1",
            "evidence_title": "current.csv",
            "file_ref": "current.csv",
        },
        "compatibility": {
            "same_project": True,
            "dataset_version": True,
            "evaluation_script_ref": True,
            "matching_metric_count": 1,
            "missing_or_incompatible_metric_count": 0,
            "change_computation": "enabled",
        },
        "changes": [{
            "evidence_id": "M1",
            "metric": "accuracy",
            "unit": "",
            "definition_version": "metric-v1",
            "split": "test",
            "direction": "higher",
            "baseline_value": 0.86,
            "current_value": 0.81,
            "absolute_delta": -0.05,
            "relative_delta": -0.058139534884,
            "assessment": "regressed",
            "baseline_source": {
                "run_id": "run-baseline",
                "file_ref": "baseline.csv",
                "source_locator": "row:2",
            },
            "current_source": {
                "run_id": "run-current",
                "file_ref": "current.csv",
                "source_locator": "row:2",
            },
        }],
        "factors": [],
    })

    assert len(evidence) == 1
    assert evidence[0]["id"] == "M1"
    assert evidence[0]["evidence_type"] == "experiment_measurement"
    assert evidence[0]["project_id"] == "project"
    assert "当前值=0.81" in evidence[0]["context"]
    assert "current.csv" in evidence[0]["context"] or "row:2" in evidence[0]["context"]
    assert "M1" in build_experiment_evidence_text(evidence)


def test_blocked_experiment_result_never_becomes_m_evidence():
    assert build_experiment_evidence({
        "status": "not_comparable",
        "compatibility": {"change_computation": "blocked"},
        "changes": [{"metric": "accuracy", "baseline_value": 0.8, "current_value": 0.7}],
    }) == []


def test_mlflow_local_run_archive_uses_latest_metric_and_preserves_locator(tmp_path):
    source = tmp_path / "mlflow-run.zip"
    start_time = int(datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc).timestamp() * 1000)
    root = "mlruns/7/run-abc"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            f"{root}/meta.yaml",
            f"experiment_id: '7'\nrun_id: run-abc\nstart_time: {start_time}\n",
        )
        archive.writestr(
            f"{root}/metrics/accuracy",
            "1720749600000 0.81 0\n1720749601000 0.86 1\n",
        )
        archive.writestr(f"{root}/params/souldrive.dataset_version", "dataset-v1")
        archive.writestr(f"{root}/params/souldrive.config_ref", "config-a")
        archive.writestr(f"{root}/params/souldrive.evaluation_script_ref", "eval-v1")
        archive.writestr(f"{root}/params/souldrive.environment_ref", "env-a")
        archive.writestr(f"{root}/params/souldrive.unit", "%")
        archive.writestr(f"{root}/params/souldrive.direction", "higher")
        archive.writestr(f"{root}/params/souldrive.definition_version", "metric-v1")
        archive.writestr(f"{root}/params/souldrive.split", "test")

    parsed = parse_metric_source(source)
    assert parsed["source_format"] == "mlflow"
    assert parsed["rows"][0]["value"] == 0.86
    assert parsed["rows"][0]["__source_locator"].endswith("metrics/accuracy:line:2")

    plan = plan_metric_import(source, experiment_name="alignment", source_name="mlflow-run.zip")
    assert plan["ready"] is True
    assert plan["missing_fields"] == []
    assert plan["rows"][0]["config_ref"] == "config-a"
    assert plan["rows"][0]["performed_at"] == "2026-07-12T02:00:00+00:00"


def test_lightweight_xlsx_fallback_reads_embedded_metric_rows_with_sheet_locators(tmp_path):
    source = tmp_path / "metrics.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet><sheetData><row><c><v>metric,value\naccuracy,0.921\np95_latency_ms,960</v></c></row></sheetData></worksheet>",
        )

    parsed = parse_metric_source(source)

    assert [row["metric"] for row in parsed["rows"]] == ["accuracy", "p95_latency_ms"]
    assert parsed["rows"][0]["value"] == "0.921"
    assert parsed["rows"][0]["__source_locator"] == "sheet:sheet1/embedded_row:2"
    assert parsed["rows"][1]["__source_locator"] == "sheet:sheet1/embedded_row:3"


def test_standard_xlsx_parser_keeps_sheet_and_row_locators(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "metrics.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Metrics"
    sheet.append(["metric", "value"])
    sheet.append(["accuracy", 0.921])
    workbook.save(source)
    workbook.close()

    parsed = parse_metric_source(source)

    assert parsed["rows"] == [{
        "__source_locator": "sheet:Metrics/row:2",
        "metric": "accuracy",
        "value": 0.921,
    }]
