import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.workspace import SoulDriveWorkspace
from core.workspace_backup import build_backup_preflight, backup_sqlite_snapshot
from core.project_catalog import ProjectCatalog


class WorkspaceBackupTests(unittest.TestCase):
    def test_preflight_blocks_active_indexing_and_durable_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            Path(workspace.keystore_path).write_text("secret-key-material", encoding="utf-8")
            connection = sqlite3.connect(workspace.task_journal_path)
            connection.execute("CREATE TABLE runtime_tasks (status TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO runtime_tasks(status) VALUES (?)",
                [("running",), ("pending",), ("completed",)],
            )
            connection.commit()
            connection.close()

            report = build_backup_preflight(
                workspace,
                runtime_state={"indexing": {"status": "indexing"}},
            )

        self.assertFalse(report["ready"])
        self.assertTrue(report["read_only"])
        self.assertEqual(
            {item["code"] for item in report["blockers"]},
            {"indexing_active", "durable_tasks_active"},
        )
        self.assertEqual(report["task_journal"]["active_count"], 2)

    def test_preflight_blocks_live_worker_even_when_runtime_state_is_idle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            Path(workspace.keystore_path).write_text("secret-key-material", encoding="utf-8")

            report = build_backup_preflight(
                workspace,
                runtime_state={"indexing": {"status": "idle"}},
                worker_active=True,
            )

        self.assertFalse(report["ready"])
        self.assertEqual(report["indexer_worker_status"], "active")
        self.assertIn("indexer_worker_active", {item["code"] for item in report["blockers"]})

    def test_preflight_separates_authoritative_derived_and_sensitive_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            Path(workspace.keystore_path).write_text("super-secret-key", encoding="utf-8")
            Path(workspace.papers_path, "paper.txt").write_text("source", encoding="utf-8")
            Path(workspace.graph_db_path).parent.mkdir(parents=True, exist_ok=True)
            Path(workspace.graph_db_path).write_bytes(b"derived")
            Path(workspace.models_path, "cache.bin").write_bytes(b"optional")

            report = build_backup_preflight(
                workspace,
                runtime_state={"indexing": {"status": "idle"}},
            )

        self.assertTrue(report["ready"])
        self.assertEqual(report["schema_version"], "souldrive_workspace_backup_preflight_v0.1")
        self.assertEqual(report["stop_write_order"][0]["action"], "停止索引器和任务 worker")
        scope = {item["path"]: item for item in report["scope"]}
        self.assertEqual(scope["data/papers"]["role"], "authoritative")
        self.assertEqual(scope["index"]["role"], "derived_rebuildable")
        self.assertEqual(scope["models"]["role"], "optional_cache")
        self.assertTrue(scope["config/keystore.json"]["sensitive"])
        self.assertNotIn("super-secret-key", json.dumps(report, ensure_ascii=False))
        self.assertIn("config/keystore.json", report["key_dependencies"])

    def test_preflight_fails_closed_when_task_journal_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            Path(workspace.keystore_path).write_text("key", encoding="utf-8")
            Path(workspace.task_journal_path).write_bytes(b"not sqlite")

            report = build_backup_preflight(
                workspace,
                runtime_state={"indexing": {"status": "idle"}},
            )

        self.assertFalse(report["ready"])
        self.assertEqual(report["task_journal"]["status"], "unreadable")
        self.assertIn("task_journal_unverified", {item["code"] for item in report["blockers"]})

    def test_sqlite_snapshot_rehearsal_copies_consistent_database_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.sqlite"
            destination = root / "restore" / "source.sqlite"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO facts(value) VALUES ('project-a')")
            connection.commit()
            connection.close()

            result = backup_sqlite_snapshot(source, destination)

            restored = sqlite3.connect(destination)
            row = restored.execute("SELECT value FROM facts").fetchone()
            restored.close()

            self.assertEqual(row[0], "project-a")
            self.assertEqual(result["integrity_check"], "ok")
            with self.assertRaises(FileExistsError):
                backup_sqlite_snapshot(source, destination)

    def test_isolated_project_restore_rehearsal_preserves_key_catalog_and_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = SoulDriveWorkspace.from_drive(str(root / "source-drive")).ensure()
            restored = SoulDriveWorkspace.from_drive(str(root / "restore-drive")).ensure()
            passphrase = "restore-rehearsal-passphrase"
            from core.workspace_crypto import initialize_keystore, unlock_keystore

            initialize_keystore(source, passphrase)
            source_keys = unlock_keystore(source, passphrase)
            Path(source.papers_path, "project-a-report.txt").write_text("project A evidence", encoding="utf-8")
            source_catalog = ProjectCatalog(source)
            source_catalog.create_project("Project A", project_id="project-a")
            source_catalog.close()

            shutil.copy2(source.manifest_path, restored.manifest_path)
            shutil.copy2(source.keystore_path, restored.keystore_path)
            shutil.copy2(
                Path(source.papers_path, "project-a-report.txt"),
                Path(restored.papers_path, "project-a-report.txt"),
            )
            backup_sqlite_snapshot(source.project_catalog_path, restored.project_catalog_path)

            restored_keys = unlock_keystore(restored, passphrase)
            self.assertEqual(restored_keys.workspace_data_key, source_keys.workspace_data_key)
            restored_catalog = ProjectCatalog(restored)
            project = restored_catalog.conn.execute(
                "SELECT project_id, name FROM projects WHERE project_id = ?",
                ("project-a",),
            ).fetchone()
            restored_catalog.close()
            report = build_backup_preflight(
                restored,
                runtime_state={"indexing": {"status": "idle"}},
            )
            restored_project = tuple(project)
            restored_source_text = Path(
                restored.papers_path,
                "project-a-report.txt",
            ).read_text(encoding="utf-8")

        self.assertEqual(restored_project, ("project-a", "Project A"))
        self.assertEqual(restored_source_text, "project A evidence")
        self.assertTrue(report["ready"])


if __name__ == "__main__":
    unittest.main()
