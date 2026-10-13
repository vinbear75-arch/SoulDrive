from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from core.project_catalog import ProjectCatalog, SCHEMA_VERSION, project_deletion_error_observation
from core.workspace import SoulDriveWorkspace


def _capture_exception(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _catalog():
    temp_dir = tempfile.TemporaryDirectory()
    workspace = SoulDriveWorkspace.from_drive(temp_dir.name).ensure()
    return temp_dir, workspace, ProjectCatalog(workspace)


def test_catalog_creates_versioned_project_schema():
    temp_dir, workspace, catalog = _catalog()
    try:
        tables = {
            row[0]
            for row in catalog.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "projects",
            "assets",
            "asset_versions",
            "experiments",
            "experiment_runs",
            "metric_definitions",
            "measurements",
            "decisions",
            "project_tasks",
            "project_templates",
            "playbook_approvals",
            "contacts",
            "artifacts",
            "project_deletions",
            "document_deletion_receipts",
        }.issubset(tables)
        assert catalog.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert workspace.project_catalog_path.endswith("project_catalog.sqlite")
        task_columns = {
            row[1]
            for row in catalog.conn.execute("PRAGMA table_info(project_tasks)")
        }
        assert {"created_at", "updated_at", "completed_at"}.issubset(task_columns)
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_catalog_shared_connection_concurrent_project_creates_are_complete():
    temp_dir, workspace, catalog = _catalog()
    try:
        def create_batch(batch_index):
            start = batch_index * 20
            for index_number in range(start, start + 20):
                catalog.create_project(
                    f"并发项目 {index_number}",
                    project_id=f"concurrent-project-{index_number}",
                    idempotency_key=f"concurrent-project-key-{index_number}",
                )

        with ThreadPoolExecutor(max_workers=4) as pool:
            errors = [
                result
                for result in pool.map(
                    lambda batch: _capture_exception(create_batch, batch),
                    range(4),
                )
                if result is not None
            ]
        assert errors == []
        assert catalog.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 80
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_catalog_migrates_existing_projects_to_research_mode():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        legacy = sqlite3.connect(workspace.project_catalog_path)
        try:
            legacy.execute(
                "CREATE TABLE projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO projects VALUES ('legacy', '旧项目', 'active', '2026-08-01T00:00:00+00:00')"
            )
            legacy.commit()
        finally:
            legacy.close()

        catalog = ProjectCatalog(workspace)
        try:
            row = catalog.conn.execute("SELECT project_mode FROM projects WHERE project_id = 'legacy'").fetchone()
            assert row["project_mode"] == "research"
        finally:
            catalog.close()


def test_catalog_migrates_legacy_project_tasks_with_nullable_temporal_fields():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        legacy = sqlite3.connect(workspace.project_catalog_path)
        try:
            legacy.execute(
                "CREATE TABLE projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "CREATE TABLE project_tasks (task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL, evidence_refs_json TEXT NOT NULL DEFAULT '[]')"
            )
            legacy.execute(
                "INSERT INTO projects VALUES ('legacy', '旧项目', 'active', '2026-08-01T00:00:00+00:00')"
            )
            legacy.execute(
                "INSERT INTO project_tasks(task_id, project_id, title, status) VALUES ('legacy-task', 'legacy', '旧任务', 'open')"
            )
            legacy.commit()
        finally:
            legacy.close()

        catalog = ProjectCatalog(workspace)
        try:
            columns = {
                row[1]: row
                for row in catalog.conn.execute("PRAGMA table_info(project_tasks)")
            }
            assert {"created_at", "updated_at", "started_at", "completed_at"}.issubset(columns)
            row = catalog.conn.execute(
                "SELECT created_at, updated_at FROM project_tasks WHERE task_id = 'legacy-task'"
            ).fetchone()
            assert row["created_at"] is None
            assert row["updated_at"] is None
        finally:
            catalog.close()


def test_catalog_migrates_legacy_artifact_provenance_columns():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        legacy = sqlite3.connect(workspace.project_catalog_path)
        try:
            legacy.execute(
                "CREATE TABLE projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, artifact_type TEXT NOT NULL, source_version_ids_json TEXT NOT NULL DEFAULT '[]', plan_json TEXT NOT NULL DEFAULT '{}', confirmation_status TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO projects VALUES ('legacy', '旧项目', 'active', '2026-08-01T00:00:00+00:00')"
            )
            legacy.execute(
                "INSERT INTO artifacts VALUES ('artifact-legacy', 'legacy', 'email_draft', '[]', '{}', 'draft', '2026-08-01T00:00:00+00:00')"
            )
            legacy.commit()
        finally:
            legacy.close()

        catalog = ProjectCatalog(workspace)
        try:
            artifact = catalog.get_artifact("artifact-legacy")
            assert artifact["confirmation_record"]["status"] == "draft"
            assert artifact["change_summary"] == ""
        finally:
            catalog.close()


def test_catalog_artifact_scope_migration_preserves_email_delivery_foreign_key():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        legacy = sqlite3.connect(workspace.project_catalog_path)
        try:
            legacy.executescript(
                """
                CREATE TABLE projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, artifact_type TEXT NOT NULL, source_version_ids_json TEXT NOT NULL DEFAULT '[]', plan_json TEXT NOT NULL DEFAULT '{}', confirmation_status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE email_deliveries (artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, sent_at TEXT, detail_json TEXT NOT NULL DEFAULT '{}');
                INSERT INTO projects VALUES ('legacy', '旧项目', 'active', '2026-08-01T00:00:00+00:00');
                INSERT INTO artifacts VALUES ('artifact-legacy', 'legacy', 'email_draft', '[]', '{}', 'confirmed', '2026-08-01T00:00:00+00:00');
                INSERT INTO email_deliveries VALUES ('artifact-legacy', 'delivery-1', 'sent', NULL, '{}');
                """
            )
            legacy.commit()
        finally:
            legacy.close()

        catalog = ProjectCatalog(workspace)
        try:
            assert catalog.conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert catalog.conn.execute(
                "SELECT status FROM email_deliveries WHERE artifact_id = 'artifact-legacy'"
            ).fetchone()[0] == "sent"
            catalog.conn.execute("DELETE FROM artifacts WHERE artifact_id = 'artifact-legacy'")
            catalog.conn.commit()
            assert catalog.conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
        finally:
            catalog.close()


def test_artifact_idempotency_key_rejects_different_payload():
    temp_dir, _workspace, catalog = _catalog()
    try:
        first = catalog.create_artifact(
            None,
            "schedule_draft",
            {"title": "第一次", "start_at": "2026-09-09T09:00:00+08:00"},
            idempotency_key="schedule-idempotency-1",
        )
        repeated = catalog.create_artifact(
            None,
            "schedule_draft",
            {"title": "第一次", "start_at": "2026-09-09T09:00:00+08:00"},
            idempotency_key="schedule-idempotency-1",
        )

        assert repeated == first
        try:
            catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "被静默覆盖的第二次请求", "start_at": "2026-09-09T09:00:00+08:00"},
                idempotency_key="schedule-idempotency-1",
            )
        except ValueError as exc:
            assert "idempotency_key" in str(exc)
        else:
            raise AssertionError("different artifact input must not reuse an idempotency key")
    finally:
        catalog.close()


def test_project_idempotency_key_is_atomic_across_catalog_connections():
    temp_dir, workspace, catalog = _catalog()
    catalog.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []
    catalogs = []
    lock = threading.Lock()

    def create() -> None:
        worker_catalog = ProjectCatalog(workspace)
        with lock:
            catalogs.append(worker_catalog)
        try:
            barrier.wait(timeout=5)
            result = worker_catalog.create_project(
                "并发项目",
                project_mode="research",
                idempotency_key="project-concurrent-key",
                return_status=True,
            )
            with lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - assertion below reports the race
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=create), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for worker_catalog in catalogs:
        worker_catalog.close()

    try:
        assert not errors, errors
        assert len(results) == 2
        assert {item["project_id"] for item in results} == {results[0]["project_id"]}
        check = ProjectCatalog(workspace)
        try:
            assert check.conn.execute(
                "SELECT COUNT(*) FROM projects WHERE idempotency_key = ?",
                ("project-concurrent-key",),
            ).fetchone()[0] == 1
        finally:
            check.close()
    finally:
        temp_dir.cleanup()


def test_artifact_idempotency_key_is_atomic_across_catalog_connections():
    temp_dir, workspace, catalog = _catalog()
    catalog.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []
    catalogs = []
    lock = threading.Lock()

    def create() -> None:
        worker_catalog = ProjectCatalog(workspace)
        with lock:
            catalogs.append(worker_catalog)
        try:
            barrier.wait(timeout=5)
            result = worker_catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "并发草案", "start_at": "2026-09-09T09:00:00+08:00"},
                idempotency_key="artifact-concurrent-key",
                return_status=True,
            )
            with lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - assertion below reports the race
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=create), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for worker_catalog in catalogs:
        worker_catalog.close()

    try:
        assert not errors, errors
        assert len(results) == 2
        assert {item["artifact_id"] for item in results} == {results[0]["artifact_id"]}
        check = ProjectCatalog(workspace)
        try:
            assert check.conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE idempotency_key = ?",
                ("artifact-concurrent-key",),
            ).fetchone()[0] == 1
        finally:
            check.close()
    finally:
        temp_dir.cleanup()


def test_completed_project_deletion_reserves_project_id_for_replay_safety():
    temp_dir, workspace, catalog = _catalog()
    try:
        catalog.create_project("可删除项目", project_id="reserved")
        receipt = catalog.begin_project_deletion("reserved")
        catalog.delete_project("reserved")
        catalog.complete_project_deletion("reserved", {"status": "deleted", "project_id": "reserved"})
        assert receipt["status"] == "pending"
        try:
            catalog.create_project("同 ID 新项目", project_id="reserved")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("completed deletion receipts must reserve project IDs")
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_concurrent_project_deletion_receipt_claim_is_idempotent():
    temp_dir, workspace, catalog = _catalog()
    catalog.create_project("并发删除项目", project_id="concurrent-delete")
    catalog.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []
    original_snapshot = ProjectCatalog.project_deletion_snapshot

    def synchronized_snapshot(self, project_id):
        snapshot = original_snapshot(self, project_id)
        barrier.wait(timeout=10)
        return snapshot

    def claim():
        local = ProjectCatalog(workspace)
        try:
            results.append(local.begin_project_deletion("concurrent-delete"))
        except Exception as exc:
            errors.append(exc)
        finally:
            local.close()

    try:
        with patch.object(ProjectCatalog, "project_deletion_snapshot", synchronized_snapshot):
            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        assert errors == []
        assert len(results) == 2
        assert {record["status"] for record in results} == {"pending"}
    finally:
        temp_dir.cleanup()


def test_legacy_project_deletion_error_is_sanitized_on_catalog_reopen():
    secret = "root busy redis://user:password@private-host secret=delete-token"
    temp_dir, workspace, catalog = _catalog()
    try:
        catalog.create_project("可清理项目", project_id="legacy-delete")
        catalog.begin_project_deletion("legacy-delete")
        catalog.conn.execute(
            "UPDATE project_deletions SET status = 'failed', error = ? WHERE project_id = ?",
            (secret, "legacy-delete"),
        )
        catalog.conn.commit()
        catalog.close()

        reopened = ProjectCatalog(workspace)
        row = reopened.conn.execute(
            "SELECT error FROM project_deletions WHERE project_id = ?",
            ("legacy-delete",),
        ).fetchone()
        record = reopened.begin_project_deletion("legacy-delete")
        reopened.close()
    finally:
        if catalog.conn:
            try:
                catalog.close()
            except Exception:
                pass
        temp_dir.cleanup()

    assert row is not None
    assert secret not in row["error"]
    assert json.loads(row["error"]) == project_deletion_error_observation(secret)
    assert record["error"] == ""


def test_email_delivery_binds_one_idempotency_key_to_one_confirmed_draft():
    temp_dir, _workspace, catalog = _catalog()
    try:
        first = catalog.create_artifact(None, "email_draft", {"subject": "A"})
        second = catalog.create_artifact(None, "email_draft", {"subject": "B"})

        assert catalog.reserve_email_delivery(first, "email-delivery-key-1") == "reserved"
        assert catalog.reserve_email_delivery(first, "email-delivery-key-2") == "conflict"
        assert catalog.reserve_email_delivery(second, "email-delivery-key-1") == "conflict"

        catalog.finish_email_delivery(first, "failed", {"error": "temporary"})
        assert catalog.reserve_email_delivery(first, "email-delivery-key-2") == "reserved"
        catalog.finish_email_delivery(first, "sent", {"provider": "mock"})
        assert catalog.reserve_email_delivery(first, "email-delivery-key-2") == "sent"
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_email_delivery_reservation_is_safe_for_concurrent_attempts():
    temp_dir, workspace, catalog = _catalog()
    try:
        artifact_id = catalog.create_artifact(None, "email_draft", {"subject": "并发"})
    finally:
        catalog.close()

    barrier = threading.Barrier(2)
    results = []
    result_lock = threading.Lock()

    def reserve(key: str) -> None:
        worker_catalog = ProjectCatalog(workspace)
        try:
            barrier.wait(timeout=5)
            result = worker_catalog.reserve_email_delivery(artifact_id, key)
            with result_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - assertion below reports the concrete race
            with result_lock:
                results.append(exc)
        finally:
            worker_catalog.close()

    threads = [
        threading.Thread(target=reserve, args=("concurrent-key-a",)),
        threading.Thread(target=reserve, args=("concurrent-key-b",)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(result for result in results if isinstance(result, str)) == ["conflict", "reserved"]
        assert all(isinstance(result, str) for result in results), results
    finally:
        temp_dir.cleanup()


def test_document_deletion_receipt_fences_concurrent_retry_and_recovers_expired_claim():
    temp_dir, workspace, catalog = _catalog()
    try:
        first = catalog.claim_document_deletion(
            "paper.pdf",
            "sha256:first",
            "document-delete-key",
            owner_id="worker-a",
        )
        assert first["claimed"] is True
        busy = catalog.claim_document_deletion(
            "paper.pdf",
            "sha256:first",
            "document-delete-key",
            owner_id="worker-b",
        )
        assert busy["claimed"] is False
        assert busy["status"] == "in_progress"

        catalog.conn.execute(
            "UPDATE document_deletion_receipts SET lease_expires_at = ? WHERE idempotency_key = ?",
            (time.time() - 1, "document-delete-key"),
        )
        catalog.conn.commit()
        recovered = catalog.claim_document_deletion(
            "paper.pdf",
            "sha256:first",
            "document-delete-key",
            owner_id="worker-b",
        )
        assert recovered["claimed"] is True
        assert recovered["replayed"] is True

        catalog.complete_document_deletion("document-delete-key", "worker-b")
        completed = catalog.document_deletion_receipt("document-delete-key")
        assert completed["status"] == "completed"
        assert completed["owner_id"] is None
        assert catalog.claim_document_deletion(
            "paper.pdf",
            "sha256:first",
            "document-delete-key",
            owner_id="worker-c",
        )["claimed"] is False
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_document_deletion_receipt_claim_is_atomic_across_catalog_connections():
    temp_dir, workspace, catalog = _catalog()
    catalog.close()
    first_catalog = ProjectCatalog(workspace)
    second_catalog = ProjectCatalog(workspace)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def trace_factory():
        seen = False

        def trace(statement):
            nonlocal seen
            if not seen and "SELECT * FROM document_deletion_receipts WHERE idempotency_key" in statement:
                seen = True
                barrier.wait(timeout=10)

        return trace

    first_catalog.conn.set_trace_callback(trace_factory())
    second_catalog.conn.set_trace_callback(trace_factory())

    def claim(worker_catalog, owner_id):
        try:
            results.append(
                worker_catalog.claim_document_deletion(
                    "paper.pdf",
                    "sha256:concurrent",
                    "document-delete-concurrent",
                    owner_id=owner_id,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=claim, args=(first_catalog, "worker-a")),
        threading.Thread(target=claim, args=(second_catalog, "worker-b")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert sorted(item["claimed"] for item in results) == [False, True]
    finally:
        first_catalog.close()
        second_catalog.close()
        temp_dir.cleanup()


def test_project_stores_folder_and_memory_policy():
    temp_dir, workspace, catalog = _catalog()
    try:
        project_root = str((Path(temp_dir.name) / "paper-project").resolve())
        catalog.create_project(
            "论文项目",
            project_id="paper",
            root_path=project_root,
            instructions="优先核对实验数据",
            memory_policy="project_only",
        )
        row = catalog.conn.execute(
            "SELECT root_path, instructions, memory_policy FROM projects WHERE project_id = 'paper'"
        ).fetchone()
        assert row["root_path"] == project_root
        assert row["instructions"] == "优先核对实验数据"
        assert row["memory_policy"] == "project_only"
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_project_scopes_playbook_approval_and_supports_revocation():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("论文项目", project_id="paper")
        approved = catalog.set_playbook_approval(
            "paper",
            "paper_review",
            "1.0",
            approved=True,
            reviewed_at="2026-08-30T00:00:00+08:00",
        )
        assert approved["status"] == "approved"
        assert catalog.get_playbook_approval("paper", "paper_review")["playbook_version"] == "1.0"

        revoked = catalog.set_playbook_approval(
            "paper",
            "paper_review",
            "1.0",
            approved=False,
        )
        assert revoked["status"] == "revoked"
        assert catalog.list_playbook_approvals("paper") == [revoked]
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_delete_project_cascades_catalog_rows_but_preserves_project_folder():
    temp_dir, workspace, catalog = _catalog()
    project_root = Path(temp_dir.name) / "paper-project"
    project_root.mkdir()
    try:
        catalog.create_project("论文项目", project_id="paper", root_path=str(project_root))
        asset_id = catalog.create_asset("paper", "实验报告", "experiment_report", asset_id="paper-asset")
        version_id = catalog.create_asset_version(
            asset_id,
            "hash",
            "paper/report.pdf",
            version_id="paper-version",
        )
        experiment_id = catalog.create_experiment("paper", "基线", experiment_id="paper-experiment")
        run_id = catalog.create_experiment_run(
            experiment_id,
            "2026-08-01T00:00:00+00:00",
            run_id="paper-run",
            evidence_version_id=version_id,
        )
        metric_id = "paper-metric"
        catalog.conn.execute(
            "INSERT INTO metric_definitions(metric_id, name, unit, direction, definition_version) VALUES (?, ?, ?, ?, ?)",
            (metric_id, "准确率", "%", "higher", "v1"),
        )
        catalog.conn.execute(
            "INSERT INTO measurements(run_id, metric_id, value, split) VALUES (?, ?, ?, ?)",
            (run_id, metric_id, 0.9, "test"),
        )
        catalog.conn.execute(
            "INSERT INTO decisions(decision_id, project_id, decided_at, statement) VALUES (?, ?, ?, ?)",
            ("paper-decision", "paper", "2026-08-01T00:00:00+00:00", "保留方案"),
        )
        catalog.conn.execute(
            "INSERT INTO project_tasks(task_id, project_id, title, status) VALUES (?, ?, ?, ?)",
            ("paper-task", "paper", "复核", "open"),
        )
        catalog.conn.commit()

        deleted = catalog.delete_project("paper")

        assert deleted["project_id"] == "paper"
        assert deleted["asset_count"] == 1
        assert deleted["asset_version_count"] == 1
        assert catalog.conn.execute("SELECT COUNT(*) FROM projects WHERE project_id = 'paper'").fetchone()[0] == 0
        for table in ("assets", "asset_versions", "experiments", "experiment_runs", "measurements", "decisions", "project_tasks"):
            assert catalog.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {('project_id' if table not in {'asset_versions', 'experiment_runs', 'measurements'} else '1 = 1')} ").fetchone()[0] == 0
        assert project_root.exists()
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_current_version_query_isolates_project_performed_time_and_version():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("电池项目", project_id="battery", created_at="2026-06-01T00:00:00+08:00")
        catalog.create_project("视觉项目", project_id="vision", created_at="2026-06-01T00:00:00+08:00")
        catalog.create_asset("battery", "七月实验", "experiment_report", asset_id="battery_report")
        catalog.create_asset("vision", "七月实验", "experiment_report", asset_id="vision_report")
        catalog.create_asset_version(
            "battery_report", "hash-1", "battery-v1.pdf", version_id="battery_v1",
            valid_from="2026-07-13T00:00:00+08:00", imported_at="2026-07-13T00:00:00+08:00",
        )
        catalog.create_asset_version(
            "battery_report", "hash-2", "battery-v2.pdf", version_id="battery_v2",
            valid_from="2026-07-20T00:00:00+08:00", imported_at="2026-08-15T00:00:00+08:00",
        )
        catalog.create_asset_version(
            "vision_report", "hash-3", "vision.pdf", version_id="vision_v1",
            valid_from="2026-07-12T00:00:00+08:00", imported_at="2026-07-12T00:00:00+08:00",
        )
        battery_experiment = catalog.create_experiment("battery", "七月实验", experiment_id="battery_exp")
        vision_experiment = catalog.create_experiment("vision", "七月实验", experiment_id="vision_exp")
        for version_id in ("battery_v1", "battery_v2"):
            catalog.create_experiment_run(
                battery_experiment,
                "2026-07-12T09:00:00+08:00",
                run_id=f"run_{version_id}",
                evidence_version_id=version_id,
            )
        catalog.create_experiment_run(
            vision_experiment,
            "2026-07-11T09:00:00+08:00",
            run_id="run_vision",
            evidence_version_id="vision_v1",
        )

        rows = catalog.find_current_versions(
            "battery",
            asset_type="experiment_report",
            performed_from="2026-07-01T00:00:00+08:00",
            performed_to="2026-08-01T00:00:00+08:00",
            as_of="2026-08-17T12:00:00+08:00",
        )

        assert [row["version_id"] for row in rows] == ["battery_v2"]
        assert rows[0]["performed_at"].startswith("2026-07-12T01:00:00")
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_foreign_keys_reject_asset_for_unknown_project():
    temp_dir, _workspace, catalog = _catalog()
    try:
        try:
            catalog.create_asset("missing", "孤立材料", "report")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("unknown project must be rejected")
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_file_ref_resolves_project_asset_version_without_cross_project_fallback():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("演示", project_id="demo")
        catalog.create_asset("demo", "报告", "experiment_report", asset_id="asset_demo")
        catalog.create_asset_version(
            "asset_demo", "hash", "demo/report.txt", version_id="version_demo",
            valid_from="2026-08-01T00:00:00+08:00", imported_at="2026-08-01T00:00:00+08:00",
        )
        resolved = catalog.find_asset_version_for_file("demo\\report.txt")
        assert resolved["project_id"] == "demo"
        assert resolved["version_id"] == "version_demo"
        assert catalog.find_asset_version_for_file("report.txt") is not None
        assert catalog.find_asset_version_for_file("missing.txt") is None
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_imported_document_registration_is_idempotent_and_updates_same_file_versions():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("文档项目", project_id="docs")
        first = catalog.register_imported_document(
            "docs",
            "RAG 报告",
            "hash-v1",
            "RAG 报告.pdf",
            metadata={"media_type": "application/pdf"},
        )
        repeated = catalog.register_imported_document(
            "docs",
            "RAG 报告",
            "hash-v1",
            "RAG 报告.pdf",
        )
        second = catalog.register_imported_document(
            "docs",
            "RAG 报告",
            "hash-v2",
            "RAG 报告.pdf",
        )

        assert first["status"] == "registered"
        assert repeated["status"] == "already_registered"
        assert repeated["version_id"] == first["version_id"]
        assert second["status"] == "registered"
        assert second["asset_id"] == first["asset_id"]
        assert catalog.find_asset_version_for_file("RAG 报告.pdf", content_hash="hash-v2")["version_id"] == second["version_id"]
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_unbind_imported_document_removes_unreferenced_project_bindings():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("项目 A", project_id="project-a")
        catalog.create_project("项目 B", project_id="project-b")
        for project_id in ("project-a", "project-b"):
            catalog.register_imported_document(
                project_id,
                "全局 RAG 资料",
                "same-hash",
                "shared/rag.pdf",
            )

        result = catalog.unbind_imported_document("shared\\rag.pdf", "same-hash")

        assert result["status"] == "globalized"
        assert result["project_ids"] == ["project-a", "project-b"]
        assert catalog.find_imported_document_bindings("shared/rag.pdf", content_hash="same-hash") == []
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_unbind_imported_document_refuses_versions_used_as_experiment_evidence():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("实验项目", project_id="experiment-project")
        binding = catalog.register_imported_document(
            "experiment-project",
            "实验资料",
            "evidence-hash",
            "evidence/report.pdf",
        )
        experiment_id = catalog.create_experiment("experiment-project", "基线", experiment_id="experiment")
        catalog.create_experiment_run(
            experiment_id,
            "2026-08-29T00:00:00+08:00",
            run_id="run",
            evidence_version_id=binding["version_id"],
        )

        try:
            catalog.unbind_imported_document("evidence/report.pdf", "evidence-hash")
        except ValueError as exc:
            assert "实验记录引用" in str(exc)
        else:
            raise AssertionError("referenced document must not be globalized")

        assert catalog.find_imported_document_bindings("evidence/report.pdf", content_hash="evidence-hash")
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_contact_save_merges_same_mailbox_instead_of_creating_ambiguity():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("邮件项目", project_id="mail")
        first = catalog.create_contact(
            "Handsome_Vin",
            "2956584050@qq.com",
            aliases=["handsome"],
            project_ids=["mail"],
        )
        repeated = catalog.create_contact(
            "Handsome_Vin",
            "2956584050@qq.com",
            aliases=["2956584050@qq.com"],
            project_ids=["mail", "mail"],
        )

        assert repeated == first
        contacts = catalog.list_contacts(project_id="mail")
        assert len(contacts) == 1
        assert contacts[0]["aliases"] == ["handsome", "2956584050@qq.com"]
        assert len(catalog.resolve_contacts("Handsome_Vin", project_id="mail")) == 1
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_contact_save_rejects_non_whitespace_control_characters():
    temp_dir, _workspace, catalog = _catalog()
    try:
        try:
            catalog.create_contact("异常联系人", "li@example.test\x00")
        except ValueError as exc:
            assert "email" in str(exc)
        else:
            raise AssertionError("control characters must not be stored in contacts")
    finally:
        catalog.close()
        temp_dir.cleanup()


def test_global_contact_is_visible_after_switching_projects():
    temp_dir, _workspace, catalog = _catalog()
    try:
        catalog.create_project("项目 A", project_id="project-a")
        catalog.create_project("项目 B", project_id="project-b")
        catalog.create_contact("handsome_Vin", "2956584050@qq.com", aliases=["帅哥文"])

        assert [item["email"] for item in catalog.list_contacts(project_id="project-a")] == ["2956584050@qq.com"]
        assert [item["email"] for item in catalog.list_contacts(project_id="project-b")] == ["2956584050@qq.com"]
        assert len(catalog.resolve_contacts("handsome_Vin", project_id="project-b")) == 1

    finally:
        catalog.close()
        temp_dir.cleanup()


def test_contact_lists_use_contact_id_as_final_tiebreaker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_contact("同名联系人", "b@example.test", contact_id="contact-b")
            catalog.create_contact("同名联系人", "a@example.test", contact_id="contact-a")
            listed = catalog.list_contacts()
            resolved = catalog.resolve_contacts("同名联系人")
        finally:
            catalog.close()

    assert [item["contact_id"] for item in listed] == ["contact-a", "contact-b"]
    assert [item["contact_id"] for item in resolved] == ["contact-a", "contact-b"]
