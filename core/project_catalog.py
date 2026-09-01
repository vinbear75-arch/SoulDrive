import json
import os
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 8
PROJECT_MODES = {"research", "business"}


class ProjectCatalog:
    def __init__(self, workspace):
        self.db_path = Path(workspace.project_catalog_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                project_mode TEXT NOT NULL DEFAULT 'research',
                root_path TEXT,
                instructions TEXT NOT NULL DEFAULT '',
                memory_policy TEXT NOT NULL DEFAULT 'project_plus_global',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                asset_type TEXT NOT NULL,
                title TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assets_project_type
                ON assets(project_id, asset_type);

            CREATE TABLE IF NOT EXISTS asset_versions (
                version_id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                file_ref TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                authored_at TEXT,
                imported_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_asset_versions_asset_valid
                ON asset_versions(asset_id, valid_from DESC);

            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS experiment_runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                evidence_version_id TEXT REFERENCES asset_versions(version_id),
                performed_at TEXT NOT NULL,
                dataset_version TEXT NOT NULL DEFAULT '',
                config_ref TEXT NOT NULL DEFAULT '',
                evaluation_script_ref TEXT NOT NULL DEFAULT '',
                environment_ref TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_experiment_runs_performed
                ON experiment_runs(experiment_id, performed_at);

            CREATE TABLE IF NOT EXISTS metric_definitions (
                metric_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                unit TEXT NOT NULL,
                direction TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                UNIQUE(name, unit, definition_version)
            );

            CREATE TABLE IF NOT EXISTS measurements (
                run_id TEXT NOT NULL REFERENCES experiment_runs(run_id) ON DELETE CASCADE,
                metric_id TEXT NOT NULL REFERENCES metric_definitions(metric_id),
                value REAL NOT NULL,
                split TEXT NOT NULL,
                sample_count INTEGER,
                variance REAL,
                source_locator TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(run_id, metric_id, split)
            );

            CREATE TABLE IF NOT EXISTS measurement_revisions (
                revision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES experiment_runs(run_id) ON DELETE CASCADE,
                metric_id TEXT NOT NULL REFERENCES metric_definitions(metric_id),
                split TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                value REAL NOT NULL,
                sample_count INTEGER,
                variance REAL,
                source_locator TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                UNIQUE(run_id, metric_id, split, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_measurement_revisions_lookup
                ON measurement_revisions(run_id, metric_id, split, revision);

            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                decided_at TEXT NOT NULL,
                statement TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS project_tasks (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                due_at TEXT,
                status TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS project_templates (
                template_id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                template_type TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS playbook_approvals (
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                playbook_id TEXT NOT NULL,
                playbook_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('approved', 'revoked')),
                reviewed_at TEXT NOT NULL,
                PRIMARY KEY(project_id, playbook_id)
            );
            CREATE INDEX IF NOT EXISTS idx_playbook_approvals_project
                ON playbook_approvals(project_id, status);

            CREATE TABLE IF NOT EXISTS contacts (
                contact_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                email TEXT NOT NULL,
                project_ids_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
                artifact_type TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                source_version_ids_json TEXT NOT NULL DEFAULT '[]',
                plan_json TEXT NOT NULL DEFAULT '{}',
                confirmation_status TEXT NOT NULL,
                confirmation_record_json TEXT NOT NULL DEFAULT '{}',
                change_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_deliveries (
                artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                sent_at TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(projects)")}
        if "project_mode" not in columns:
            self.conn.execute("ALTER TABLE projects ADD COLUMN project_mode TEXT NOT NULL DEFAULT 'research'")
        if "root_path" not in columns:
            self.conn.execute("ALTER TABLE projects ADD COLUMN root_path TEXT")
        if "instructions" not in columns:
            self.conn.execute("ALTER TABLE projects ADD COLUMN instructions TEXT NOT NULL DEFAULT ''")
        if "memory_policy" not in columns:
            self.conn.execute("ALTER TABLE projects ADD COLUMN memory_policy TEXT NOT NULL DEFAULT 'project_plus_global'")
        run_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(experiment_runs)")}
        if "evaluation_script_ref" not in run_columns:
            self.conn.execute("ALTER TABLE experiment_runs ADD COLUMN evaluation_script_ref TEXT NOT NULL DEFAULT ''")
        measurement_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(measurements)")}
        if "source_locator" not in measurement_columns:
            self.conn.execute("ALTER TABLE measurements ADD COLUMN source_locator TEXT NOT NULL DEFAULT ''")
        artifact_info = list(self.conn.execute("PRAGMA table_info(artifacts)"))
        artifact_columns = {row[1] for row in artifact_info}
        artifact_needs_confirmation_backfill = "confirmation_record_json" not in artifact_columns
        artifact_needs_change_summary_backfill = "change_summary" not in artifact_columns
        artifact_project = next((row for row in artifact_info if row[1] == "project_id"), None)
        if artifact_project is not None and int(artifact_project[3] or 0) == 1:
            # Rebuild the parent and child tables together; SQLite otherwise
            # leaves email_deliveries pointing at the renamed legacy table.
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys = OFF")
            try:
                email_exists = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_deliveries'"
                ).fetchone() is not None
                if email_exists:
                    self.conn.execute("ALTER TABLE email_deliveries RENAME TO email_deliveries_legacy_artifact_scope")
                self.conn.execute("ALTER TABLE artifacts RENAME TO artifacts_legacy_project_scope")
                self.conn.executescript(
                    """
                    CREATE TABLE artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
                        artifact_type TEXT NOT NULL,
                        idempotency_key TEXT UNIQUE,
                        source_version_ids_json TEXT NOT NULL DEFAULT '[]',
                        plan_json TEXT NOT NULL DEFAULT '{}',
                        confirmation_status TEXT NOT NULL,
                        confirmation_record_json TEXT NOT NULL DEFAULT '{}',
                        change_summary TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE email_deliveries (
                        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL,
                        sent_at TEXT,
                        detail_json TEXT NOT NULL DEFAULT '{}'
                    );
                    """
                )
                artifact_target_columns = (
                    "artifact_id", "project_id", "artifact_type", "idempotency_key",
                    "source_version_ids_json", "plan_json", "confirmation_status",
                    "confirmation_record_json", "change_summary", "created_at",
                )
                artifact_defaults = {
                    "artifact_id": "''",
                    "project_id": "NULL",
                    "artifact_type": "'unknown'",
                    "idempotency_key": "NULL",
                    "source_version_ids_json": "'[]'",
                    "plan_json": "'{}'",
                    "confirmation_status": "'draft'",
                    "confirmation_record_json": "'{}'",
                    "change_summary": "''",
                    "created_at": "CURRENT_TIMESTAMP",
                }
                select_expr = ", ".join(
                    f'"{column}"' if column in artifact_columns else artifact_defaults[column]
                    for column in artifact_target_columns
                )
                columns_sql = ", ".join(artifact_target_columns)
                self.conn.execute(
                    f"INSERT INTO artifacts ({columns_sql}) SELECT {select_expr} FROM artifacts_legacy_project_scope"
                )
                if email_exists:
                    email_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(email_deliveries_legacy_artifact_scope)")}
                    email_target_columns = ("artifact_id", "idempotency_key", "status", "sent_at", "detail_json")
                    email_defaults = {"sent_at": "NULL", "detail_json": "'{}'"}
                    email_select = ", ".join(
                        f'"{column}"' if column in email_columns else email_defaults[column]
                        for column in email_target_columns
                    )
                    email_columns_sql = ", ".join(email_target_columns)
                    self.conn.execute(
                        f"INSERT INTO email_deliveries ({email_columns_sql}) SELECT {email_select} FROM email_deliveries_legacy_artifact_scope"
                    )
                    self.conn.execute("DROP TABLE email_deliveries_legacy_artifact_scope")
                self.conn.execute("DROP TABLE artifacts_legacy_project_scope")
            finally:
                self.conn.commit()
                self.conn.execute("PRAGMA foreign_keys = ON")
            artifact_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(artifacts)")}
        # Calendar drafts are workspace-wide. Preserve all rows while removing
        # the old project binding so switching projects cannot hide history.
        self.conn.execute(
            "UPDATE artifacts SET project_id = NULL WHERE artifact_type = 'schedule_draft' AND project_id IS NOT NULL"
        )
        if "idempotency_key" not in artifact_columns:
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN idempotency_key TEXT")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_idempotency_key ON artifacts(idempotency_key)")
        if artifact_needs_confirmation_backfill:
            if "confirmation_record_json" not in artifact_columns:
                self.conn.execute(
                    "ALTER TABLE artifacts ADD COLUMN confirmation_record_json TEXT NOT NULL DEFAULT '{}'"
                )
            for row in self.conn.execute(
                "SELECT artifact_id, confirmation_status, plan_json, created_at FROM artifacts"
            ).fetchall():
                try:
                    plan = json.loads(row["plan_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    plan = {}
                self.conn.execute(
                    "UPDATE artifacts SET confirmation_record_json = ? WHERE artifact_id = ?",
                    (
                        json.dumps({
                            "status": row["confirmation_status"],
                            "required": bool(plan.get("requires_user_confirmation", False)),
                            "recorded_at": row["created_at"],
                        }, ensure_ascii=False, sort_keys=True),
                        row["artifact_id"],
                    ),
                )
        if artifact_needs_change_summary_backfill:
            if "change_summary" not in artifact_columns:
                self.conn.execute(
                    "ALTER TABLE artifacts ADD COLUMN change_summary TEXT NOT NULL DEFAULT ''"
                )
        self._backfill_measurement_revisions()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _backfill_measurement_revisions(self) -> None:
        """Give legacy current measurements a first immutable revision."""
        rows = self.conn.execute(
            """
            SELECT m.run_id, m.metric_id, m.split, m.value, m.sample_count,
                   m.variance, m.source_locator
            FROM measurements m
            LEFT JOIN measurement_revisions r
              ON r.run_id = m.run_id AND r.metric_id = m.metric_id
             AND r.split = m.split
            WHERE r.revision_id IS NULL
            """
        ).fetchall()
        for row in rows:
            self.conn.execute(
                """
                INSERT INTO measurement_revisions(
                    revision_id, run_id, metric_id, split, revision, value,
                    sample_count, variance, source_locator, recorded_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("measurement_revision"),
                    row["run_id"],
                    row["metric_id"],
                    row["split"],
                    row["value"],
                    row["sample_count"],
                    row["variance"],
                    str(row["source_locator"] or "")[:260],
                    _timestamp(None),
                ),
            )

    def create_project(
        self,
        name: str,
        *,
        project_id: str | None = None,
        status: str = "active",
        project_mode: str = "research",
        root_path: str | None = None,
        instructions: str = "",
        memory_policy: str = "project_plus_global",
        created_at: str | None = None,
    ) -> str:
        project_id = project_id or _new_id("project")
        self.conn.execute(
            "INSERT INTO projects(project_id, name, status, project_mode, root_path, instructions, memory_policy, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                _required(name, "name"),
                _required(status, "status"),
                _project_mode(project_mode),
                str(root_path or "").strip() or None,
                str(instructions or "").strip()[:8000],
                str(memory_policy or "project_plus_global").strip(),
                _timestamp(created_at),
            ),
        )
        self.conn.commit()
        return project_id

    def delete_project(self, project_id: str) -> dict[str, Any] | None:
        """删除项目目录记录及其级联业务数据，但保留用户项目文件夹。"""
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("project_id is required")

        project = self.conn.execute(
            "SELECT project_id, root_path FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            return None

        counts = {
            "asset_count": self.conn.execute(
                "SELECT COUNT(*) FROM assets WHERE project_id = ?", (project_id,)
            ).fetchone()[0],
            "asset_version_count": self.conn.execute(
                """
                SELECT COUNT(*)
                FROM asset_versions v
                JOIN assets a ON a.asset_id = v.asset_id
                WHERE a.project_id = ?
                """,
                (project_id,),
            ).fetchone()[0],
            "experiment_count": self.conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE project_id = ?", (project_id,)
            ).fetchone()[0],
            "experiment_run_count": self.conn.execute(
                """
                SELECT COUNT(*)
                FROM experiment_runs r
                JOIN experiments e ON e.experiment_id = r.experiment_id
                WHERE e.project_id = ?
                """,
                (project_id,),
            ).fetchone()[0],
            "measurement_count": self.conn.execute(
                """
                SELECT COUNT(*)
                FROM measurements m
                JOIN experiment_runs r ON r.run_id = m.run_id
                JOIN experiments e ON e.experiment_id = r.experiment_id
                WHERE e.project_id = ?
                """,
                (project_id,),
            ).fetchone()[0],
            "decision_count": self.conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE project_id = ?", (project_id,)
            ).fetchone()[0],
            "task_count": self.conn.execute(
                "SELECT COUNT(*) FROM project_tasks WHERE project_id = ?", (project_id,)
            ).fetchone()[0],
            "artifact_count": self.conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE project_id = ?", (project_id,)
            ).fetchone()[0],
        }

        with self.conn:
            # 联系人是全局通讯录，删除项目时只移除已经失效的项目绑定。
            for row in self.conn.execute(
                "SELECT contact_id, project_ids_json FROM contacts"
            ).fetchall():
                try:
                    project_ids = json.loads(row[1] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(project_ids, list) or project_id not in project_ids:
                    continue
                kept = [item for item in project_ids if str(item) != project_id]
                self.conn.execute(
                    "UPDATE contacts SET project_ids_json = ? WHERE contact_id = ?",
                    (json.dumps(kept, ensure_ascii=False), row[0]),
                )
            self.conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))

        return {
            "project_id": project_id,
            "root_path": project["root_path"],
            **{key: int(value) for key, value in counts.items()},
        }

    def get_playbook_approval(self, project_id: str, playbook_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT project_id, playbook_id, playbook_version, status, reviewed_at "
            "FROM playbook_approvals WHERE project_id = ? AND playbook_id = ?",
            (_required(project_id, "project_id"), _required(playbook_id, "playbook_id")),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_playbook_approvals(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT project_id, playbook_id, playbook_version, status, reviewed_at "
            "FROM playbook_approvals WHERE project_id = ? ORDER BY playbook_id",
            (_required(project_id, "project_id"),),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_playbook_approval(
        self,
        project_id: str,
        playbook_id: str,
        playbook_version: str,
        *,
        approved: bool,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        project_id = _required(project_id, "project_id")
        playbook_id = _required(playbook_id, "playbook_id")
        playbook_version = _required(playbook_version, "playbook_version")
        if self.conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone() is None:
            raise KeyError(f"unknown project: {project_id}")
        record = {
            "project_id": project_id,
            "playbook_id": playbook_id,
            "playbook_version": playbook_version,
            "status": "approved" if approved else "revoked",
            "reviewed_at": _timestamp(reviewed_at),
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO playbook_approvals(
                    project_id, playbook_id, playbook_version, status, reviewed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, playbook_id) DO UPDATE SET
                    playbook_version = excluded.playbook_version,
                    status = excluded.status,
                    reviewed_at = excluded.reviewed_at
                """,
                tuple(record.values()),
            )
        return record

    def create_asset(
        self,
        project_id: str,
        title: str,
        asset_type: str,
        *,
        source_kind: str = "imported",
        asset_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        asset_id = asset_id or _new_id("asset")
        self.conn.execute(
            """
            INSERT INTO assets(asset_id, project_id, asset_type, title, source_kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                _required(project_id, "project_id"),
                _required(asset_type, "asset_type"),
                _required(title, "title"),
                _required(source_kind, "source_kind"),
                _timestamp(created_at),
            ),
        )
        self.conn.commit()
        return asset_id

    def create_asset_version(
        self,
        asset_id: str,
        content_hash: str,
        file_ref: str,
        *,
        version_id: str | None = None,
        valid_from: str | None = None,
        authored_at: str | None = None,
        imported_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        version_id = version_id or _new_id("version")
        self.conn.execute(
            """
            INSERT INTO asset_versions(
                version_id, asset_id, content_hash, file_ref, valid_from,
                authored_at, imported_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                _required(asset_id, "asset_id"),
                _required(content_hash, "content_hash"),
                _required(file_ref, "file_ref"),
                _timestamp(valid_from),
                _optional_timestamp(authored_at),
                _timestamp(imported_at),
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        return version_id

    def register_imported_document(
        self,
        project_id: str,
        title: str,
        content_hash: str,
        file_ref: str,
        *,
        asset_type: str = "document",
        source_kind: str = "imported",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently bind one imported document to a local project."""
        project_id = _required(project_id, "project_id")
        title = _required(title, "title")
        content_hash = _required(content_hash, "content_hash")
        file_ref = _normalize_file_ref(file_ref)
        existing = self.conn.execute(
            """
            SELECT a.asset_id, a.project_id, a.asset_type, a.title,
                   v.version_id, v.content_hash, v.file_ref,
                   v.valid_from, v.imported_at, v.metadata_json
            FROM assets a
            JOIN asset_versions v ON v.asset_id = a.asset_id
            WHERE a.project_id = ?
              AND v.content_hash = ?
              AND replace(v.file_ref, '\\', '/') = ?
            ORDER BY v.valid_from DESC, v.version_id DESC
            LIMIT 1
            """,
            (project_id, content_hash, file_ref),
        ).fetchone()
        if existing is not None:
            item = dict(existing)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["status"] = "already_registered"
            return item

        asset_row = self.conn.execute(
            """
            SELECT a.asset_id
            FROM assets a
            JOIN asset_versions v ON v.asset_id = a.asset_id
            WHERE a.project_id = ?
              AND replace(v.file_ref, '\\', '/') = ?
            ORDER BY v.valid_from DESC, v.version_id DESC
            LIMIT 1
            """,
            (project_id, file_ref),
        ).fetchone()
        asset_id = str(asset_row["asset_id"]) if asset_row is not None else self.create_asset(
            project_id,
            title,
            asset_type,
            source_kind=source_kind,
        )
        version_id = self.create_asset_version(
            asset_id,
            content_hash,
            file_ref,
            metadata=metadata,
        )
        return {
            "asset_id": asset_id,
            "project_id": project_id,
            "asset_type": asset_type,
            "title": title,
            "version_id": version_id,
            "content_hash": content_hash,
            "file_ref": file_ref,
            "status": "registered",
        }

    def find_imported_document_bindings(self, file_ref: str, *, content_hash: str | None = None) -> list[dict[str, Any]]:
        """列出一个导入文档当前绑定的项目，供范围审计与安全归类使用。"""
        normalized_ref = _normalize_file_ref(file_ref)
        clauses = ["replace(v.file_ref, '\\', '/') = ?"]
        params: list[Any] = [normalized_ref]
        if content_hash:
            clauses.append("v.content_hash = ?")
            params.append(str(content_hash))
        rows = self.conn.execute(
            f"""
            SELECT a.asset_id, a.project_id, a.asset_type, a.title,
                   v.version_id, v.content_hash, v.file_ref,
                   v.valid_from, v.imported_at, v.metadata_json
            FROM assets a
            JOIN asset_versions v ON v.asset_id = a.asset_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.project_id, v.valid_from DESC, v.version_id DESC
            """,
            params,
        ).fetchall()
        bindings = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            bindings.append(item)
        return bindings

    def unbind_imported_document(self, file_ref: str, content_hash: str) -> dict[str, Any]:
        """将无外部证据引用的导入文档解除项目绑定，保留原始文档本体。"""
        bindings = self.find_imported_document_bindings(file_ref, content_hash=content_hash)
        if not bindings:
            return {"status": "already_global", "project_ids": [], "version_ids": []}

        asset_ids = sorted({str(item["asset_id"]) for item in bindings})
        version_ids = sorted({str(item["version_id"]) for item in bindings})
        referenced_versions = set()
        for version_id in version_ids:
            if self.conn.execute(
                "SELECT 1 FROM experiment_runs WHERE evidence_version_id = ? LIMIT 1",
                (version_id,),
            ).fetchone() is not None:
                referenced_versions.add(version_id)
        if referenced_versions:
            raise ValueError("文档版本已被实验记录引用，未执行全局归类")

        for row in self.conn.execute(
            "SELECT artifact_id, source_version_ids_json FROM artifacts"
        ).fetchall():
            try:
                source_version_ids = json.loads(row[1] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                source_version_ids = []
            if any(str(version_id) in version_ids for version_id in source_version_ids if version_id):
                raise ValueError("文档版本已被交付物引用，未执行全局归类")

        for asset_id in asset_ids:
            rows = self.conn.execute(
                "SELECT version_id, content_hash, file_ref FROM asset_versions WHERE asset_id = ?",
                (asset_id,),
            ).fetchall()
            if any(
                str(row[0]) not in version_ids
                or str(row[1]) != str(content_hash)
                or _normalize_file_ref(row[2]) != _normalize_file_ref(file_ref)
                for row in rows
            ):
                raise ValueError("文档存在多版本项目历史，未执行全局归类")

        self.conn.executemany("DELETE FROM assets WHERE asset_id = ?", [(asset_id,) for asset_id in asset_ids])
        self.conn.commit()
        return {
            "status": "globalized",
            "project_ids": sorted({str(item["project_id"]) for item in bindings}),
            "version_ids": version_ids,
        }

    def create_experiment(
        self,
        project_id: str,
        name: str,
        *,
        experiment_id: str | None = None,
        objective: str = "",
    ) -> str:
        experiment_id = experiment_id or _new_id("experiment")
        self.conn.execute(
            "INSERT INTO experiments(experiment_id, project_id, name, objective) VALUES (?, ?, ?, ?)",
            (experiment_id, _required(project_id, "project_id"), _required(name, "name"), str(objective)),
        )
        self.conn.commit()
        return experiment_id

    def create_experiment_run(
        self,
        experiment_id: str,
        performed_at: str,
        *,
        run_id: str | None = None,
        evidence_version_id: str | None = None,
        dataset_version: str = "",
        config_ref: str = "",
        evaluation_script_ref: str = "",
        environment_ref: str = "",
    ) -> str:
        run_id = run_id or _new_id("run")
        self.conn.execute(
            """
            INSERT INTO experiment_runs(
                run_id, experiment_id, evidence_version_id, performed_at,
                dataset_version, config_ref, evaluation_script_ref, environment_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _required(experiment_id, "experiment_id"),
                evidence_version_id,
                _timestamp(performed_at),
                str(dataset_version),
                str(config_ref),
                str(evaluation_script_ref),
                str(environment_ref),
            ),
        )
        self.conn.commit()
        return run_id

    def create_metric_definition(
        self,
        name: str,
        unit: str,
        direction: str,
        definition_version: str,
        *,
        metric_id: str | None = None,
    ) -> str:
        """Create or reuse a metric contract shared by experiment runs."""
        name = _required(name, "metric_name")
        unit = _required(unit, "metric_unit")
        direction = str(direction or "").strip().lower()
        if direction not in {"higher", "lower"}:
            raise ValueError("metric_direction must be higher or lower")
        definition_version = _required(definition_version, "definition_version")
        existing = self.conn.execute(
            "SELECT metric_id FROM metric_definitions WHERE name = ? AND unit = ? AND definition_version = ?",
            (name, unit, definition_version),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        metric_id = metric_id or _new_id("metric")
        self.conn.execute(
            "INSERT INTO metric_definitions(metric_id, name, unit, direction, definition_version) VALUES (?, ?, ?, ?, ?)",
            (metric_id, name, unit, direction, definition_version),
        )
        self.conn.commit()
        return metric_id

    def record_measurement(
        self,
        run_id: str,
        metric_id: str,
        value: float,
        split: str,
        *,
        sample_count: int | None = None,
        variance: float | None = None,
        source_locator: str = "",
    ) -> None:
        """Record one immutable measurement; exact replays remain idempotent.

        A run represents one concrete execution. Replacing a value for the same
        metric and split would rewrite that execution and invalidate later
        comparisons, so callers must create a new run for a different result.
        """
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("measurement value must be numeric") from exc
        if not math.isfinite(numeric_value):
            raise ValueError("measurement value must be finite")
        split = _required(split, "split")
        if sample_count is not None:
            try:
                sample_count = int(sample_count)
            except (TypeError, ValueError) as exc:
                raise ValueError("sample_count must be an integer") from exc
            if sample_count < 0:
                raise ValueError("sample_count must be non-negative")
        if variance is not None:
            try:
                variance = float(variance)
            except (TypeError, ValueError) as exc:
                raise ValueError("variance must be numeric") from exc
            if not math.isfinite(variance) or variance < 0.0:
                raise ValueError("variance must be finite and non-negative")
        run_id = _required(run_id, "run_id")
        metric_id = _required(metric_id, "metric_id")
        source_locator = str(source_locator or "").strip()[:260]
        existing = self.conn.execute(
            """
            SELECT value, sample_count, variance, source_locator
            FROM measurements
            WHERE run_id = ? AND metric_id = ? AND split = ?
            """,
            (run_id, metric_id, split),
        ).fetchone()
        if existing is not None:
            same_value = float(existing["value"]) == numeric_value
            same_sample_count = existing["sample_count"] == sample_count
            same_variance = existing["variance"] == variance
            same_locator = str(existing["source_locator"] or "") == source_locator
            if not (same_value and same_sample_count and same_variance and same_locator):
                raise ValueError(
                    "measurement is immutable; create a new experiment run for a new result"
                )
            # Older catalogs may contain a measurement written before revision
            # tracking existed. Backfill only the exact replay, never a change.
            revision = self.conn.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM measurement_revisions
                WHERE run_id = ? AND metric_id = ? AND split = ?
                """,
                (run_id, metric_id, split),
            ).fetchone()["revision"]
            if not revision:
                self.conn.execute(
                    """
                    INSERT INTO measurement_revisions(
                        revision_id, run_id, metric_id, split, revision, value,
                        sample_count, variance, source_locator, recorded_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id("measurement_revision"), run_id, metric_id, split,
                        numeric_value, sample_count, variance, source_locator, _timestamp(None),
                    ),
                )
                self.conn.commit()
            return

        self.conn.execute(
            """
            INSERT INTO measurements(run_id, metric_id, value, split, sample_count, variance, source_locator)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, metric_id, numeric_value, split, sample_count, variance, source_locator),
        )
        self.conn.execute(
            """
            INSERT INTO measurement_revisions(
                revision_id, run_id, metric_id, split, revision, value,
                sample_count, variance, source_locator, recorded_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("measurement_revision"), run_id, metric_id, split,
                numeric_value, sample_count, variance, source_locator, _timestamp(None),
            ),
        )
        self.conn.commit()

    def list_measurement_revisions(
        self,
        run_id: str,
        metric_id: str,
        *,
        split: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return immutable measurement revisions in ascending order."""
        run_id = _required(run_id, "run_id")
        metric_id = _required(metric_id, "metric_id")
        clauses = ["run_id = ?", "metric_id = ?"]
        params: list[Any] = [run_id, metric_id]
        if split is not None:
            clauses.append("split = ?")
            params.append(_required(split, "split"))
        rows = self.conn.execute(
            f"""
            SELECT revision_id, run_id, metric_id, split, revision, value,
                   sample_count, variance, source_locator, recorded_at
            FROM measurement_revisions
            WHERE {' AND '.join(clauses)}
            ORDER BY split, revision
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_experiment_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT r.run_id, r.experiment_id, e.project_id, e.name AS experiment_name,
                   e.objective, r.evidence_version_id, r.performed_at,
                   r.dataset_version, r.config_ref, r.evaluation_script_ref,
                   r.environment_ref, a.title AS evidence_title, v.file_ref,
                   v.content_hash
            FROM experiment_runs r
            JOIN experiments e ON e.experiment_id = r.experiment_id
            LEFT JOIN asset_versions v ON v.version_id = r.evidence_version_id
            LEFT JOIN assets a ON a.asset_id = v.asset_id
            WHERE r.run_id = ?
            """,
            (_required(run_id, "run_id"),),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["measurements"] = [
            dict(measurement)
            for measurement in self.conn.execute(
                """
                SELECT m.metric_id, d.name, d.unit, d.direction, d.definition_version,
                       m.value, m.split, m.sample_count, m.variance, m.source_locator
                FROM measurements m
                JOIN metric_definitions d ON d.metric_id = m.metric_id
                WHERE m.run_id = ?
                ORDER BY d.name, m.split
                """,
                (run_id,),
            ).fetchall()
        ]
        return item

    def list_experiment_runs(
        self,
        project_id: str,
        *,
        performed_from: str | None = None,
        performed_to: str | None = None,
        experiment_name: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["e.project_id = ?"]
        params: list[Any] = [_required(project_id, "project_id")]
        if performed_from:
            clauses.append("r.performed_at >= ?")
            params.append(_timestamp(performed_from))
        if performed_to:
            clauses.append("r.performed_at < ?")
            params.append(_timestamp(performed_to))
        if experiment_name:
            clauses.append("e.name = ?")
            params.append(_required(experiment_name, "experiment_name"))
        rows = self.conn.execute(
            f"SELECT r.run_id FROM experiment_runs r JOIN experiments e ON e.experiment_id = r.experiment_id WHERE {' AND '.join(clauses)} ORDER BY r.performed_at DESC, r.run_id DESC",
            params,
        ).fetchall()
        return [item for row in rows if (item := self.get_experiment_run(str(row[0]))) is not None]

    def find_current_versions(
        self,
        project_id: str,
        *,
        asset_type: str | None = None,
        performed_from: str | None = None,
        performed_to: str | None = None,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["a.project_id = ?"]
        params: list[Any] = [_required(project_id, "project_id")]
        if asset_type:
            clauses.append("a.asset_type = ?")
            params.append(asset_type)
        if performed_from:
            clauses.append("r.performed_at >= ?")
            params.append(_timestamp(performed_from))
        if performed_to:
            clauses.append("r.performed_at < ?")
            params.append(_timestamp(performed_to))
        if as_of:
            clauses.append("v.valid_from <= ?")
            params.append(_timestamp(as_of))
        rows = self.conn.execute(
            f"""
            SELECT a.asset_id, a.project_id, a.asset_type, a.title,
                   v.version_id, v.valid_from, v.imported_at, v.metadata_json,
                   r.performed_at
            FROM assets a
            JOIN asset_versions v ON v.asset_id = a.asset_id
            LEFT JOIN experiment_runs r ON r.evidence_version_id = v.version_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.asset_id, v.valid_from DESC, v.version_id DESC
            """,
            params,
        ).fetchall()
        current = []
        seen_assets = set()
        for row in rows:
            if row["asset_id"] in seen_assets:
                continue
            seen_assets.add(row["asset_id"])
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            current.append(item)
        return current

    def find_asset_version_for_file(
        self,
        file_ref: str,
        *,
        content_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one indexed file to its project identity when the catalog has it."""
        normalized = str(file_ref or "").replace("\\", "/").lstrip("./")
        hash_clause = " AND v.content_hash = ?" if content_hash else ""
        exact_params: list[Any] = [normalized]
        if content_hash:
            exact_params.append(str(content_hash))
        row = self.conn.execute(
            f"""
            SELECT a.project_id, a.asset_id, a.asset_type, a.title,
                   v.version_id, v.valid_from, v.authored_at, v.imported_at,
                   v.file_ref, v.metadata_json
            FROM asset_versions v
            JOIN assets a ON a.asset_id = v.asset_id
            WHERE replace(v.file_ref, '\\', '/') = ?{hash_clause}
            ORDER BY v.valid_from DESC, v.version_id DESC
            LIMIT 1
            """,
            exact_params,
        ).fetchone()
        if row is None:
            basename = os.path.basename(normalized)
            fallback_params: list[Any] = [f"%/{basename}", basename]
            if content_hash:
                fallback_params.append(str(content_hash))
            rows = self.conn.execute(
                f"""
                SELECT a.project_id, a.asset_id, a.asset_type, a.title,
                       v.version_id, v.valid_from, v.authored_at, v.imported_at,
                       v.file_ref, v.metadata_json
                FROM asset_versions v
                JOIN assets a ON a.asset_id = v.asset_id
                WHERE (replace(v.file_ref, '\\', '/') LIKE ? OR replace(v.file_ref, '\\', '/') = ?)
                  {('AND v.content_hash = ?' if content_hash else '')}
                ORDER BY v.valid_from DESC, v.version_id DESC
                """,
                fallback_params,
            ).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def create_contact(
        self,
        display_name: str,
        email: str,
        *,
        aliases: list[str] | None = None,
        project_ids: list[str] | None = None,
        contact_id: str | None = None,
    ) -> str:
        """Store a user-confirmed local contact; this is not an employee directory."""
        display_name = _required(display_name, "display_name")
        email = _required(email, "email")
        if "@" not in email or any(character.isspace() for character in email):
            raise ValueError("email must be a local contact address")
        aliases = _clean_list(aliases, 12, 120)
        project_ids = _clean_list(project_ids, 24, 128)

        # A local contact is identified by its mailbox, not by each UI save
        # operation.  Re-saving the same address must not create two equally
        # ranked candidates and make a later draft ambiguous.
        existing = self.conn.execute(
            "SELECT contact_id, aliases_json, project_ids_json FROM contacts WHERE lower(email) = lower(?) LIMIT 1",
            (email,),
        ).fetchone()
        if existing is not None:
            existing_aliases = _clean_list(json.loads(existing["aliases_json"] or "[]"), 12, 120)
            existing_projects = _clean_list(json.loads(existing["project_ids_json"] or "[]"), 24, 128)
            merged_aliases = _clean_list(list(dict.fromkeys([*existing_aliases, *aliases])), 12, 120)
            merged_projects = _clean_list(list(dict.fromkeys([*existing_projects, *project_ids])), 24, 128)
            self.conn.execute(
                """
                UPDATE contacts
                SET display_name = ?, aliases_json = ?, project_ids_json = ?
                WHERE contact_id = ?
                """,
                (
                    display_name,
                    json.dumps(merged_aliases, ensure_ascii=False),
                    json.dumps(merged_projects, ensure_ascii=False),
                    existing["contact_id"],
                ),
            )
            self.conn.commit()
            return str(existing["contact_id"])

        contact_id = contact_id or _new_id("contact")
        self.conn.execute(
            """
            INSERT INTO contacts(contact_id, display_name, aliases_json, email, project_ids_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                contact_id,
                display_name,
                json.dumps(aliases, ensure_ascii=False),
                email,
                json.dumps(project_ids, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return contact_id

    def resolve_contacts(self, query: str, *, project_id: str | None = None) -> list[dict[str, Any]]:
        query = _required(query, "query").casefold()
        rows = self.conn.execute(
            "SELECT contact_id, display_name, aliases_json, email, project_ids_json FROM contacts ORDER BY display_name"
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            aliases = json.loads(row["aliases_json"] or "[]")
            project_ids = json.loads(row["project_ids_json"] or "[]")
            if project_id and project_ids and project_id not in project_ids:
                continue
            names = [row["display_name"], *aliases, row["email"]]
            score = 0
            for name in names:
                value = str(name).casefold()
                if value == query:
                    score = max(score, 3)
                elif query in value:
                    score = max(score, 2)
            if score:
                matches.append({
                    "contact_id": row["contact_id"],
                    "display_name": row["display_name"],
                    "aliases": aliases,
                    "email": row["email"],
                    "project_ids": project_ids,
                    "match_score": score,
                })
        return sorted(matches, key=lambda item: (-item["match_score"], item["display_name"].casefold()))

    def list_contacts(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT contact_id, display_name, aliases_json, email, project_ids_json FROM contacts ORDER BY display_name"
        ).fetchall()
        contacts = []
        for row in rows:
            aliases = json.loads(row["aliases_json"] or "[]")
            project_ids = json.loads(row["project_ids_json"] or "[]")
            if project_id and project_ids and project_id not in project_ids:
                continue
            contacts.append({
                "contact_id": row["contact_id"],
                "display_name": row["display_name"],
                "aliases": aliases,
                "email": row["email"],
                "project_ids": project_ids,
            })
        return contacts

    def delete_contact(self, contact_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM contacts WHERE contact_id = ?", (_required(contact_id, "contact_id"),))
        return cursor.rowcount > 0

    def create_artifact(
        self,
        project_id: str | None,
        artifact_type: str,
        plan: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        source_version_ids: list[str] | None = None,
        confirmation_status: str = "draft",
        confirmation_record: dict[str, Any] | None = None,
        change_summary: str = "",
        artifact_id: str | None = None,
    ) -> str:
        if idempotency_key:
            row = self.conn.execute(
                "SELECT artifact_id FROM artifacts WHERE idempotency_key = ?",
                (_required(idempotency_key, "idempotency_key"),),
            ).fetchone()
            if row is not None:
                return str(row[0])
        artifact_id = artifact_id or _new_id("artifact")
        if not isinstance(plan, dict):
            raise ValueError("artifact plan must be an object")
        if confirmation_record is not None and not isinstance(confirmation_record, dict):
            raise ValueError("confirmation_record must be an object")
        created_at = _timestamp(None)
        record = dict(confirmation_record or {})
        record.setdefault("status", confirmation_status)
        record.setdefault("required", bool(plan.get("requires_user_confirmation", False)))
        record.setdefault("recorded_at", created_at)
        summary = str(change_summary or "").strip()[:2000]
        if not summary:
            summary = "首次创建；无前一版本可比较。"
        self.conn.execute(
            """
            INSERT INTO artifacts(
                artifact_id, project_id, artifact_type, idempotency_key, source_version_ids_json,
                plan_json, confirmation_status, confirmation_record_json,
                change_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                str(project_id).strip() if project_id is not None and str(project_id).strip() else None,
                _required(artifact_type, "artifact_type"),
                _required(idempotency_key, "idempotency_key") if idempotency_key else None,
                json.dumps(_clean_list(source_version_ids, 64, 160), ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False, sort_keys=True),
                _required(confirmation_status, "confirmation_status"),
                json.dumps(record, ensure_ascii=False, sort_keys=True),
                summary,
                created_at,
            ),
        )
        self.conn.commit()
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["source_version_ids"] = json.loads(item.pop("source_version_ids_json") or "[]")
        item["plan"] = json.loads(item.pop("plan_json") or "{}")
        try:
            confirmation_record = json.loads(item.pop("confirmation_record_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            confirmation_record = {}
        item["confirmation_record"] = confirmation_record if isinstance(confirmation_record, dict) else {}
        return item

    def list_artifacts(self, project_id: str | None, *, artifact_type: str | None = None) -> list[dict[str, Any]]:
        query = ["SELECT artifact_id FROM artifacts WHERE project_id IS NULL"] if project_id is None else ["SELECT artifact_id FROM artifacts WHERE project_id = ?"]
        params: list[Any] = [] if project_id is None else [project_id]
        if artifact_type:
            query.append("AND artifact_type = ?")
            params.append(artifact_type)
        query.append("ORDER BY created_at DESC, artifact_id DESC")
        rows = self.conn.execute(" ".join(query), tuple(params)).fetchall()
        return [artifact for row in rows if (artifact := self.get_artifact(row[0])) is not None]

    def set_artifact_status(self, artifact_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"draft", "confirmed", "cancelled"}:
            raise ValueError("unsupported artifact confirmation status")
        current = self.get_artifact(artifact_id)
        if current is None:
            return None
        confirmation_record = dict(current.get("confirmation_record") or {})
        confirmation_record.update({
            "previous_status": current["confirmation_status"],
            "status": status,
            "status_changed_at": _timestamp(None),
        })
        self.conn.execute(
            "UPDATE artifacts SET confirmation_status = ?, confirmation_record_json = ? WHERE artifact_id = ?",
            (status, json.dumps(confirmation_record, ensure_ascii=False, sort_keys=True), artifact_id),
        )
        self.conn.commit()
        return self.get_artifact(artifact_id)

    def update_artifact_provenance(
        self,
        artifact_id: str,
        *,
        source_version_ids: list[str] | None = None,
        plan_patch: dict[str, Any] | None = None,
        confirmation_record_patch: dict[str, Any] | None = None,
        change_summary: str | None = None,
    ) -> dict[str, Any] | None:
        """Update only auditable provenance fields; keep the artifact identity stable."""
        current = self.get_artifact(artifact_id)
        if current is None:
            return None
        if plan_patch is not None and not isinstance(plan_patch, dict):
            raise ValueError("plan_patch must be an object")
        if confirmation_record_patch is not None and not isinstance(confirmation_record_patch, dict):
            raise ValueError("confirmation_record_patch must be an object")
        plan = dict(current.get("plan") or {})
        plan.update(plan_patch or {})
        source_ids = list(current.get("source_version_ids") or [])
        if source_version_ids is not None:
            source_ids = _clean_list(source_version_ids, 64, 160)
        confirmation_record = dict(current.get("confirmation_record") or {})
        confirmation_record.update(confirmation_record_patch or {})
        summary = current.get("change_summary") or "首次创建；无前一版本可比较。"
        if change_summary is not None:
            summary = str(change_summary).strip()[:2000] or summary
        self.conn.execute(
            """
            UPDATE artifacts
            SET source_version_ids_json = ?, plan_json = ?,
                confirmation_record_json = ?, change_summary = ?
            WHERE artifact_id = ?
            """,
            (
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False, sort_keys=True),
                json.dumps(confirmation_record, ensure_ascii=False, sort_keys=True),
                summary,
                artifact_id,
            ),
        )
        self.conn.commit()
        return self.get_artifact(artifact_id)

    def reserve_email_delivery(self, artifact_id: str, idempotency_key: str) -> str:
        row = self.conn.execute("SELECT status FROM email_deliveries WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is not None:
            if row["status"] == "failed":
                self.conn.execute(
                    "UPDATE email_deliveries SET idempotency_key = ?, status = 'sending', sent_at = NULL, detail_json = '{}' WHERE artifact_id = ?",
                    (_required(idempotency_key, "idempotency_key"), artifact_id),
                )
                self.conn.commit()
                return "reserved"
            return str(row["status"])
        self.conn.execute(
            "INSERT INTO email_deliveries(artifact_id, idempotency_key, status) VALUES (?, ?, 'sending')",
            (_required(artifact_id, "artifact_id"), _required(idempotency_key, "idempotency_key")),
        )
        self.conn.commit()
        return "reserved"

    def finish_email_delivery(self, artifact_id: str, status: str, detail: dict[str, Any]) -> None:
        if status not in {"sent", "failed"}:
            raise ValueError("unsupported delivery status")
        self.conn.execute(
            "UPDATE email_deliveries SET status = ?, sent_at = ?, detail_json = ? WHERE artifact_id = ?",
            (status, _timestamp(None) if status == "sent" else None, json.dumps(detail, ensure_ascii=False), artifact_id),
        )
        self.conn.commit()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _project_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in PROJECT_MODES:
        raise ValueError("project_mode must be research or business")
    return mode


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_timestamp(value: str | None) -> str | None:
    return _timestamp(value) if value else None


def _normalize_file_ref(value: str) -> str:
    return _required(value, "file_ref").replace("\\", "/").lstrip("./")


def _clean_list(values: list[str] | None, limit: int, item_limit: int) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("list value is required")
    cleaned = []
    for value in values[:limit]:
        text = str(value or "").strip()
        if text:
            cleaned.append(text[:item_limit])
    return cleaned
