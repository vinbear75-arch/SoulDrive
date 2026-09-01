import sqlite3
import tempfile
from pathlib import Path

from core.project_catalog import ProjectCatalog, SCHEMA_VERSION
from core.workspace import SoulDriveWorkspace


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
        }.issubset(tables)
        assert catalog.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert workspace.project_catalog_path.endswith("project_catalog.sqlite")
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
