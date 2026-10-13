import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from core import mcp_server
from core.conversation_store import ConversationStore
from core.graph_rag import _graph_scope_digest, _graph_snapshot_digest
from core.learning_store import LearningStore
from core.project_catalog import ProjectCatalog, project_deletion_error_observation
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.secure_document_store import SecureDocumentStore
from core.state_backends import ProductionSessionStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys, initialize_keystore, unlock_keystore
import core.runtime_state as runtime_state


def test_project_endpoints_expose_local_project_overview():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo", project_mode="research", created_at="2026-08-01T08:00:00+08:00")
            catalog.create_asset("demo", "实验报告", "experiment_report", asset_id="asset")
            catalog.create_asset("demo", "待绑定笔记", "note", asset_id="unversioned")
            catalog.create_asset_version("asset", "hash", "report.pdf", version_id="version", valid_from="2026-08-02T08:00:00+08:00")
            catalog.create_experiment("demo", "baseline", experiment_id="experiment")
            catalog.create_experiment_run("experiment", "2026-08-03T08:00:00+08:00", run_id="run", evidence_version_id="version", dataset_version="demo-v1")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            projects = client.get("/projects", headers={"X-SoulDrive-Token": "test-token"})
            overview = client.get("/projects/demo/overview", headers={"X-SoulDrive-Token": "test-token"})
            created = client.post(
                "/projects",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"name": "新建演示项目", "project_mode": "business"},
            )

    assert projects.status_code == 200
    assert projects.json()["projects"][0]["asset_count"] == 2
    assert projects.json()["projects"][0]["project_mode"] == "research"
    assert overview.status_code == 200
    assert overview.json()["project"]["project_mode"] == "research"
    assert overview.json()["recent_runs"][0]["dataset_version"] == "demo-v1"
    assert overview.json()["health"]["asset_count"] == 2
    assert overview.json()["health"]["asset_version_count"] == 1
    assert overview.json()["health"]["unversioned_asset_count"] == 1
    assert overview.json()["health"]["open_task_count"] == 0
    assert overview.json()["episodic_view"]["schema_version"] == "souldrive_episodic_view_v0.1"
    assert overview.json()["episodic_view"]["events"][0]["event_id"] == "experiment_run:run"
    assert created.status_code == 200
    assert created.json()["status"] == "created"


def test_project_create_replays_same_idempotency_key_and_rejects_changed_input():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        headers = {
            "X-SoulDrive-Token": "test-token",
            "Idempotency-Key": "project-create-retry-1",
        }
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace",
            return_value=workspace,
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            first = client.post(
                "/projects",
                headers=headers,
                json={"name": "幂等项目", "project_mode": "research"},
            )
            replay = client.post(
                "/projects",
                headers=headers,
                json={"name": "幂等项目", "project_mode": "research"},
            )
            conflict = client.post(
                "/projects",
                headers=headers,
                json={"name": "改名项目", "project_mode": "research"},
            )
            catalog = ProjectCatalog(workspace)
            try:
                count = catalog.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            finally:
                catalog.close()

    assert first.status_code == 200
    assert first.json()["status"] == "created"
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"
    assert replay.json()["replayed"] is True
    assert replay.json()["project_id"] == first.json()["project_id"]
    assert conflict.status_code == 409
    assert "idempotency" in conflict.json()["error"]
    assert count == 1


def test_project_create_uses_catalog_replay_result_after_unique_key_race():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = MagicMock()
        audit.append_event.side_effect = lambda event_type, payload=None: events.append((event_type, payload))

        class RacingCatalog:
            def get_project_by_idempotency_key(self, _key):
                return None

            def create_project(self, _name, **_kwargs):
                return {"project_id": "project-race", "replayed": True}

            def close(self):
                pass

        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.ProjectCatalog", return_value=RacingCatalog()), patch(
            "core.mcp_server.current_audit_logger", return_value=audit
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/projects",
                headers={
                    "X-SoulDrive-Token": "test-token",
                    "Idempotency-Key": "project-race-replay-1",
                },
                json={"name": "竞争回放项目", "project_mode": "research"},
            )

    assert response.status_code == 200
    assert response.json()["project_id"] == "project-race"
    assert response.json()["status"] == "replayed"
    assert response.json()["replayed"] is True
    assert events == []


def test_project_create_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/projects",
                headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "project-audit-create-001"},
                json={"name": "审计故障创建项目", "project_mode": "research"},
            )
        catalog = ProjectCatalog(workspace)
        try:
            project_count = catalog.conn.execute(
                "SELECT COUNT(*) FROM projects WHERE name = ?", ("审计故障创建项目",)
            ).fetchone()[0]
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert "审计记录写入失败" in response.json()["warning"]
    assert project_count == 1


def test_contact_create_replay_does_not_duplicate_created_audit_event():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        audit = MagicMock()
        events = []
        audit.append_event.side_effect = lambda event_type, payload=None: events.append((event_type, payload))
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "test-token"}
            payload = {
                "display_name": "李老师",
                "email": "li@example.test",
                "aliases": ["老师"],
                "project_ids": ["demo"],
            }
            first = client.post("/contacts", headers=headers, json=payload)
            replay = client.post("/contacts", headers=headers, json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["contact_id"] == first.json()["contact_id"]
    assert replay.json()["replayed"] is True
    assert [event_type for event_type, _ in events] == ["contact.created"]


def test_contact_create_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/contacts",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"display_name": "审计故障联系人", "email": "audit-contact@example.test"},
            )
        catalog = ProjectCatalog(workspace)
        try:
            contact = next(
                item for item in catalog.list_contacts()
                if item["contact_id"] == response.json().get("contact_id")
            )
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert "审计记录写入失败" in response.json()["warning"]
    assert contact["email"] == "audit-contact@example.test"


def test_project_session_create_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("会话创建审计", project_id="session-create-audit")
        finally:
            catalog.close()
        store = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"}, clear=False), patch.object(
            runtime_state, "STATE_PATH", state_path
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server._conversation_store", return_value=store
        ), patch("core.mcp_server.current_audit_logger", return_value=audit):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/projects/session-create-audit/sessions",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"session_id": "session-create-audit-1", "title": "审计故障会话"},
            )
        persisted = store.session_metadata("session-create-audit-1")
        store.close()

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert "审计记录写入失败" in response.json()["warning"]
    assert persisted["title"] == "审计故障会话"
def test_project_overview_uses_stable_identity_tiebreakers():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("排序项目", project_id="ordered")
            catalog.create_experiment("ordered", "实验", experiment_id="ordered-experiment")
            catalog.create_experiment_run(
                "ordered-experiment",
                "2026-08-03T08:00:00+08:00",
                run_id="run-b",
                dataset_version="b",
            )
            catalog.create_experiment_run(
                "ordered-experiment",
                "2026-08-03T08:00:00+08:00",
                run_id="run-a",
                dataset_version="a",
            )
            catalog.conn.executemany(
                "INSERT INTO project_tasks(task_id, project_id, title, due_at, status) VALUES (?, ?, ?, ?, ?)",
                [
                    ("task-b", "ordered", "同一任务", "2026-08-04T08:00:00+08:00", "status-b"),
                    ("task-a", "ordered", "同一任务", "2026-08-04T08:00:00+08:00", "status-a"),
                ],
            )
            catalog.conn.executemany(
                "INSERT INTO decisions(decision_id, project_id, decided_at, statement) VALUES (?, ?, ?, ?)",
                [
                    ("decision-b", "ordered", "2026-08-03T08:00:00+08:00", "decision-b"),
                    ("decision-a", "ordered", "2026-08-03T08:00:00+08:00", "decision-a"),
                ],
            )
            catalog.conn.commit()
        finally:
            catalog.close()

        overview = mcp_server._project_overview_payload(workspace, "ordered")

    assert [item["run_id"] for item in overview["recent_runs"]] == ["run-a", "run-b"]
    assert [item["status"] for item in overview["tasks"]] == ["status-a", "status-b"]
    assert [item["statement"] for item in overview["decisions"]] == ["decision-a", "decision-b"]


def test_project_overview_projects_temporal_tasks_into_meeting_activity():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("会议任务项目", project_id="meeting-project")
            catalog.conn.execute(
                """
                INSERT INTO project_tasks(
                    task_id, project_id, title, due_at, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "meeting-task",
                    "meeting-project",
                    "确认实验负责人",
                    "2026-09-15T18:00:00+08:00",
                    "open",
                    "2026-09-12T12:00:00+08:00",
                    "2026-09-12T13:00:00+08:00",
                ),
            )
            catalog.conn.commit()
        finally:
            catalog.close()

        overview = mcp_server._project_overview_payload(workspace, "meeting-project")

    task_events = [
        item
        for item in overview["episodic_view"]["events"]
        if item["event_id"] == "project_task:meeting-task"
    ]
    assert len(task_events) == 1
    assert task_events[0]["event_time"] == "2026-09-12T13:00:00+08:00"
    assert task_events[0]["due_at"] == "2026-09-15T18:00:00+08:00"


def test_project_catalog_uses_project_id_as_final_tiebreaker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("同名项目", project_id="project-b", created_at="2026-08-03T08:00:00+00:00")
            catalog.create_project("同名项目", project_id="project-a", created_at="2026-08-03T08:00:00+00:00")
        finally:
            catalog.close()

        payload = mcp_server._project_catalog_payload(workspace)

    assert [item["project_id"] for item in payload["projects"]] == ["project-a", "project-b"]


def test_project_sessions_require_names_and_can_be_deleted():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("会话项目", project_id="sessions")
        finally:
            catalog.close()
        archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict(
            "os.environ",
            {
                "SOULDRIVE_API_TOKEN": "test-token",
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
            },
            clear=False,
        ):
            store = ProductionSessionStore(archive)
            try:
                with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                    "core.mcp_server.current_workspace", return_value=workspace
                ), patch("core.mcp_server._conversation_store", return_value=store):
                    runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                    runtime_state.mark_software_unlocked()
                    client = TestClient(mcp_server.app)
                    headers = {"X-SoulDrive-Token": "test-token"}
                    missing_name = client.post("/projects/sessions/sessions", headers=headers, json={})
                    blank_name = client.post("/projects/sessions/sessions", headers=headers, json={"title": "   "})
                    created = client.post(
                        "/projects/sessions/sessions",
                        headers=headers,
                        json={"title": "RAG 论文问答"},
                    )
                    session_id = created.json()["session"]["session_id"]
                    deleted = client.delete(f"/projects/sessions/sessions/{session_id}", headers=headers)
                    history = client.get(f"/sessions/{session_id}/history", headers=headers)
            finally:
                store.close()

    assert missing_name.status_code == 422
    assert blank_name.status_code == 422
    assert created.status_code == 200
    assert created.json()["session"]["title"] == "RAG 论文问答"
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert history.status_code == 404


def test_global_sessions_are_separate_from_project_sessions():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        catalog.create_project("项目", project_id="project")
        catalog.close()
        archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        store = ProductionSessionStore(archive)
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"}, clear=False):
            with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch("core.mcp_server._conversation_store", return_value=store):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                headers = {"X-SoulDrive-Token": "test-token"}
                global_created = client.post("/sessions", headers=headers, json={"title": "全局问答"})
                project_created = client.post("/projects/project/sessions", headers=headers, json={"title": "项目问答"})
                global_list = client.get("/sessions", headers=headers)
                project_list = client.get("/projects/project/sessions", headers=headers)
                global_session_id = global_created.json()["session"]["session_id"]
                project_session_id = project_created.json()["session"]["session_id"]
                global_history = client.get(f"/sessions/{global_session_id}/history", headers=headers)
                global_history_with_project = client.get(
                    f"/sessions/{global_session_id}/history?project_id=project",
                    headers=headers,
                )
                project_history_without_scope = client.get(
                    f"/sessions/{project_session_id}/history",
                    headers=headers,
                )
                project_history = client.get(
                    f"/sessions/{project_session_id}/history?project_id=project",
                    headers=headers,
                )
                project_history_wrong_scope = client.get(
                    f"/sessions/{project_session_id}/history?project_id=other-project",
                    headers=headers,
                )
        store.close()

    assert global_created.status_code == 200
    assert global_created.json()["session"]["project_id"] is None
    assert project_created.status_code == 200
    assert project_created.json()["session"]["project_id"] == "project"
    assert len(global_list.json()["sessions"]) == 1
    assert len(project_list.json()["sessions"]) == 1
    assert global_history.status_code == 200
    assert global_history_with_project.status_code == 409
    assert project_history_without_scope.status_code == 409
    assert project_history.status_code == 200
    assert project_history_wrong_scope.status_code == 409


def test_session_id_cannot_move_global_scope_into_project_and_replay_is_explicit():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        catalog.create_project("项目", project_id="project")
        catalog.close()
        archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        store = ProductionSessionStore(archive)
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch("core.mcp_server._conversation_store", return_value=store):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                headers = {"X-SoulDrive-Token": "test-token"}
                global_created = client.post(
                    "/sessions",
                    headers=headers,
                    json={"session_id": "global-session", "title": "全局会话"},
                )
                scope_collision = client.post(
                    "/projects/project/sessions",
                    headers=headers,
                    json={"session_id": "global-session", "title": "全局会话"},
                )
                project_created = client.post(
                    "/projects/project/sessions",
                    headers=headers,
                    json={"session_id": "project-session", "title": "项目会话"},
                )
                project_replay = client.post(
                    "/projects/project/sessions",
                    headers=headers,
                    json={"session_id": "project-session", "title": "项目会话"},
                )
        store.close()

    assert global_created.status_code == 200
    assert scope_collision.status_code == 422
    assert project_created.status_code == 200
    assert project_created.json()["status"] == "created"
    assert project_created.json()["replayed"] is False
    assert project_replay.status_code == 200
    assert project_replay.json()["status"] == "replayed"
    assert project_replay.json()["replayed"] is True
    assert project_replay.json()["session"]["project_id"] == "project"


def test_project_delete_removes_catalog_sessions_indexes_and_root():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        project_root = Path(temp_dir) / "project-files"
        project_root.mkdir()
        marker = project_root / "原始资料.txt"
        marker.write_text("原始文件保留", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("待删除项目", project_id="delete-me", root_path=str(project_root))
            asset_id = catalog.create_asset("delete-me", "实验报告", "experiment_report", asset_id="asset-delete")
            catalog.create_asset_version(asset_id, "hash-delete", "report.pdf", version_id="version-delete")
        finally:
            catalog.close()

        store = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        store.create_session("session-delete", project_id="delete-me", title="项目会话")
        store.append("session-delete", "user", "待删除会话内容")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.cleanup_runtime"), patch(
            "core.mcp_server._conversation_store", return_value=store
        ), patch(
            "core.mcp_server._delete_project_indexes",
            return_value={"index_child_count": 1, "index_parent_count": 1, "graph_chunk_count": 1},
        ) as delete_indexes, patch("core.mcp_server.current_audit_logger"):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.delete("/projects/delete-me", headers={"X-SoulDrive-Token": "test-token"})

        try:
            catalog = ProjectCatalog(workspace)
            project = catalog.conn.execute(
                "SELECT project_id FROM projects WHERE project_id = 'delete-me'"
            ).fetchone()
            asset_count = catalog.conn.execute(
                "SELECT COUNT(*) FROM assets WHERE project_id = 'delete-me'"
            ).fetchone()[0]
            remaining_sessions = store.list_sessions("delete-me")
            root_deleted = not project_root.exists()
        finally:
            catalog.close()
            store.close()

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert response.json()["session_count"] == 1
    assert response.json()["index_child_count"] == 1
    assert project is None
    assert asset_count == 0
    assert remaining_sessions == []
    assert root_deleted
    assert response.json()["root_deleted"] is True
    delete_indexes.assert_called_once()


def test_project_delete_cleans_project_learning_scope_and_memory_projection():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        project_root = Path(temp_dir) / "project-files"
        project_root.mkdir()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("带学习数据的项目", project_id="delete-learning", root_path=str(project_root))
        finally:
            catalog.close()

        keys = derive_workspace_keys(b"k" * 32)
        set_workspace_keys(workspace.root_path, keys)
        learning = LearningStore(workspace, keys)
        production = ProductionSessionStore(
            ConversationStore(workspace.conversation_store_path, keys.metadata_key)
        )
        try:
            learning.sync_project_skill(
                {
                    "title": "项目 Skill",
                    "content": "只属于项目的执行步骤。",
                    "source_path": "review/SKILL.md",
                    "source_digest": "project-skill-v1",
                    "project_id": "delete-learning",
                }
            )
            learning.sync_project_skill(
                {
                    "title": "全局 Skill",
                    "content": "工作区共享执行步骤。",
                    "source_path": "shared/SKILL.md",
                    "source_digest": "global-skill-v1",
                }
            )
            review = learning.create_review(
                "manual",
                [{
                    "asset_type": "memory",
                    "subtype": "project_fact",
                    "title": "项目事实",
                    "content": "只属于待删除项目的事实。",
                    "evidence": ["项目证据"],
                    "confidence": 0.9,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                }],
                project_id="delete-learning",
            )
            memory = learning.decide_review(
                review["review_id"],
                [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
                project_id="delete-learning",
            )["accepted_assets"][0]
            [project_skill] = learning.list_skills(project_id="delete-learning", include_global=False)
            learning.record_skill_run(
                project_skill["skill_id"],
                query="删除项目时也要清理运行记录",
                plan={"steps": [{"index": 1, "instruction": "清理"}]},
                project_id="delete-learning",
            )
            production.archive.archive_memory(memory)

            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.dict(
                "os.environ",
                {
                    "SOULDRIVE_API_TOKEN": "test-token",
                    "SOULDRIVE_REDIS_ENABLED": "0",
                    "SOULDRIVE_POSTGRES_ENABLED": "0",
                },
                clear=False,
            ), patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch("core.mcp_server.cleanup_runtime"), patch(
                "core.mcp_server._conversation_store", return_value=production
            ), patch(
                "core.mcp_server._delete_project_indexes",
                return_value={"index_child_count": 0, "index_parent_count": 0, "graph_chunk_count": 0},
            ), patch("core.mcp_server.current_audit_logger"):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).delete(
                    "/projects/delete-learning",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

            reopened = LearningStore(workspace, keys)
            try:
                project_skills = reopened.list_skills(project_id="delete-learning", include_global=False)
                global_skills = reopened.list_skills(project_id=None)
                project_memories = reopened.list_memories(project_id="delete-learning", include_global=False)
                project_skill_runs = reopened.list_skill_runs(project_id="delete-learning", include_global=False)
                project_chunk_count = reopened.conn.execute(
                    "SELECT COUNT(*) FROM learning_chunks WHERE project_id = ?",
                    ("delete-learning",),
                ).fetchone()[0]
            finally:
                reopened.close()
            archived_memory = production.archive.read_memory(memory["memory_id"])
        finally:
            learning.close()
            production.close()
            clear_workspace_keys(workspace.root_path)

    assert response.status_code == 200
    assert project_skills == []
    assert len(global_skills) == 1
    assert project_memories == []
    assert project_skill_runs == []
    assert project_chunk_count == 0
    assert archived_memory is None


def test_project_delete_stays_successful_when_completion_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("审计故障删除项目", project_id="delete-audit-failed")
        finally:
            catalog.close()
        store = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            runtime_state, "STATE_PATH", state_path
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.cleanup_runtime"
        ), patch("core.mcp_server._conversation_store", return_value=store), patch(
            "core.mcp_server._delete_project_indexes",
            return_value={"index_child_count": 0, "index_parent_count": 0, "graph_chunk_count": 0},
        ), patch("core.mcp_server.current_audit_logger", return_value=audit):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.delete("/projects/delete-audit-failed", headers={"X-SoulDrive-Token": "test-token"})

        catalog = ProjectCatalog(workspace)
        try:
            receipt = catalog.conn.execute(
                "SELECT status FROM project_deletions WHERE project_id = 'delete-audit-failed'"
            ).fetchone()
        finally:
            catalog.close()
            store.close()

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert "审计记录写入失败" in response.json()["warning"]
    assert receipt["status"] == "completed"


def test_session_delete_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("会话删除项目", project_id="session-delete-audit")
        finally:
            catalog.close()
        store = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        store.create_session("session-audit", project_id="session-delete-audit", title="待删会话")
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            runtime_state, "STATE_PATH", state_path
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server._conversation_store", return_value=store
        ), patch("core.mcp_server.current_audit_logger", return_value=audit):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).delete(
                "/projects/session-delete-audit/sessions/session-audit",
                headers={"X-SoulDrive-Token": "test-token"},
            )
        remaining = store.session_metadata("session-audit")
        store.close()

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert "审计记录写入失败" in response.json()["warning"]
    assert remaining is None


def test_project_delete_failure_keeps_catalog_record_and_root():
    secret = "index unavailable redis://user:password@private-host secret=delete-token"
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        project_root = Path(temp_dir) / "project-files"
        project_root.mkdir()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("删除失败项目", project_id="delete-failed", root_path=str(project_root))
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            runtime_state, "STATE_PATH", state_path
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.cleanup_runtime"
        ), patch(
            "core.mcp_server._delete_project_indexes", side_effect=RuntimeError(secret)
        ), patch("core.mcp_server.current_audit_logger") as audit_factory:
            audit = audit_factory.return_value
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.delete("/projects/delete-failed", headers={"X-SoulDrive-Token": "test-token"})

        catalog = ProjectCatalog(workspace)
        try:
            project = catalog.conn.execute(
                "SELECT project_id FROM projects WHERE project_id = 'delete-failed'"
            ).fetchone()
            deletion = catalog.conn.execute(
                "SELECT error FROM project_deletions WHERE project_id = 'delete-failed'"
            ).fetchone()
            root_preserved = project_root.exists()
        finally:
            catalog.close()

    assert response.status_code == 500
    assert response.json()["error"] == "项目数据清理失败，项目目录记录已保留，请修复后重试"
    assert project is not None
    assert root_preserved
    assert deletion is not None
    assert secret not in deletion["error"]
    failed_events = [
        call.args[1]
        for call in audit.append_event.call_args_list
        if len(call.args) >= 2 and call.args[0] == "project.delete_failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["error"] == "项目删除失败，请查看运行诊断。"
    assert failed_events[0]["error_observation"]["error_type"] == "RuntimeError"
    assert secret not in str(failed_events[0])
    assert failed_events[0]["error_observation"]["error_digest"] == project_deletion_error_observation(secret)["error_digest"]


def test_project_delete_resumes_after_root_failure_and_replays_completed_result():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        project_root = Path(temp_dir) / "project-files"
        project_root.mkdir()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("可恢复删除项目", project_id="delete-resume", root_path=str(project_root))
        finally:
            catalog.close()
        store = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ), patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.cleanup_runtime"), patch(
            "core.mcp_server._conversation_store", return_value=store
        ), patch(
            "core.mcp_server._delete_project_indexes",
            return_value={"index_child_count": 0, "index_parent_count": 0, "graph_chunk_count": 0},
        ), patch(
            "core.mcp_server._delete_project_root",
            side_effect=[
                RuntimeError("root temporarily busy"),
                {"root_deleted": True, "root_path": str(project_root), "root_warning": ""},
            ],
        ), patch("core.mcp_server.current_audit_logger"):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "test-token"}
            first = client.delete("/projects/delete-resume", headers=headers)
            second = client.delete("/projects/delete-resume", headers=headers)
            replay = client.delete("/projects/delete-resume", headers=headers)

        try:
            catalog = ProjectCatalog(workspace)
            project = catalog.conn.execute(
                "SELECT project_id FROM projects WHERE project_id = 'delete-resume'"
            ).fetchone()
            receipt = catalog.conn.execute(
                "SELECT status FROM project_deletions WHERE project_id = 'delete-resume'"
            ).fetchone()
        finally:
            catalog.close()
            store.close()

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json()["status"] == "deleted"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert project is None
    assert receipt["status"] == "completed"


def test_experiment_metric_import_is_confirmation_gated_and_rejects_self_comparison():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(workspace.papers_path) / "metrics.csv"
        source.write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("实验项目", project_id="metrics")
            catalog.create_asset("metrics", "指标记录", "experiment_metrics", asset_id="metrics-asset")
            catalog.create_asset_version("metrics-asset", mcp_server.file_md5(source), "metrics.csv", version_id="metrics-version")
        finally:
            catalog.close()

        state_path = str(Path(temp_dir) / "runtime_state.json")
        headers = {"X-SoulDrive-Token": "test-token"}
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger"):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            planned = client.post(
                "/projects/metrics/experiments/metrics",
                headers=headers,
                json={"source_name": "metrics.csv", "experiment_name": "alignment"},
            )

            catalog = ProjectCatalog(workspace)
            try:
                before_confirm = catalog.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0]
            finally:
                catalog.close()

            confirmed = client.post(
                "/projects/metrics/experiments/metrics",
                headers=headers,
                json={
                    "source_name": "metrics.csv",
                    "experiment_name": "alignment",
                    "performed_at": "2026-08-12T10:00:00+08:00",
                    "dataset_version": "dataset-v1",
                    "config_ref": "config-a",
                    "evaluation_script_ref": "eval-v1",
                    "environment_ref": "env-a",
                    "unit": "%",
                    "direction": "higher",
                    "definition_version": "accuracy-v1",
                    "split": "test",
                    "confirmed": True,
                },
            )
            runs = client.get("/projects/metrics/experiments/runs", headers=headers)
            repeat_summary = client.post(
                "/projects/metrics/experiments/repeats/summary",
                headers=headers,
                json={"run_ids": [confirmed.json()["run_id"]]},
            )
            comparison = client.post(
                "/projects/metrics/experiments/compare",
                headers=headers,
                json={
                    "baseline_run_id": confirmed.json()["run_id"],
                    "current_run_id": confirmed.json()["run_id"],
                },
            )

    assert planned.status_code == 200
    assert planned.json()["status"] == "needs_confirmation"
    assert planned.json()["plan"]["source_name"] == "metrics.csv"
    assert before_confirm == 0
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "imported"
    assert runs.status_code == 200
    assert len(runs.json()["runs"]) == 1
    assert repeat_summary.status_code == 200
    assert repeat_summary.json()["status"] == "ready"
    assert repeat_summary.json()["run_count"] == 1
    assert comparison.status_code == 200
    assert comparison.json()["status"] == "not_comparable"
    assert "不同" in comparison.json()["reason"]


def test_experiment_results_stay_truthful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(workspace.papers_path) / "metrics.csv"
        source.write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("实验项目", project_id="metrics")
            catalog.create_asset("metrics", "指标记录", "experiment_metrics", asset_id="metrics-asset")
            catalog.create_asset_version("metrics-asset", mcp_server.file_md5(source), "metrics.csv", version_id="metrics-version")
        finally:
            catalog.close()

        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit sink failed for experiment-secret")

        state_path = str(Path(temp_dir) / "runtime_state.json")
        headers = {"X-SoulDrive-Token": "test-token"}
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_API_TOKEN": "test-token", "SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.current_audit_logger", return_value=FailingAuditLogger()
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            planned = client.post(
                "/projects/metrics/experiments/metrics",
                headers=headers,
                json={"source_name": "metrics.csv", "experiment_name": "alignment"},
            )
            confirmed = client.post(
                "/projects/metrics/experiments/metrics",
                headers=headers,
                json={
                    "source_name": "metrics.csv",
                    "experiment_name": "alignment",
                    "performed_at": "2026-08-12T10:00:00+08:00",
                    "dataset_version": "dataset-v1",
                    "config_ref": "config-a",
                    "evaluation_script_ref": "eval-v1",
                    "environment_ref": "env-a",
                    "unit": "%",
                    "direction": "higher",
                    "definition_version": "accuracy-v1",
                    "split": "test",
                    "confirmed": True,
                },
            )
            repeat_summary = client.post(
                "/projects/metrics/experiments/repeats/summary",
                headers=headers,
                json={"run_ids": [confirmed.json()["run_id"]]},
            )
            comparison = client.post(
                "/projects/metrics/experiments/compare",
                headers=headers,
                json={
                    "baseline_run_id": confirmed.json()["run_id"],
                    "current_run_id": confirmed.json()["run_id"],
                },
            )

    assert planned.status_code == 200
    assert planned.json()["status"] == "needs_confirmation"
    assert "审计记录写入失败" in planned.json()["warning"]
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "imported"
    assert "审计记录写入失败" in confirmed.json()["warning"]
    assert repeat_summary.status_code == 200
    assert repeat_summary.json()["status"] == "ready"
    assert "审计记录写入失败" in repeat_summary.json()["warning"]
    assert comparison.status_code == 200
    assert comparison.json()["status"] == "not_comparable"
    assert "不同" in comparison.json()["reason"]
    assert "审计记录写入失败" in comparison.json()["warning"]


def test_graphrag_diagnostics_uses_the_secure_graph_store_when_unlocked():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.__enter__.return_value = graph_store
        graph_store.load_graphrag_index.return_value = {
            "schema_version": "test",
            "algorithm": "leiden",
            "entity_count": 3,
            "relationship_count": 2,
            "document_count": 1,
            "communities": [{"community_id": "C1", "entity_names": [], "source_filenames": [], "report": "test"}],
        }
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["community_count"] == 1
    graph_store.close.assert_called_once()


def test_graphrag_diagnostics_exposes_all_projects_index_state():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()

        def load_index(*, index_name="default"):
            if index_name not in {"global_shared", "all_projects"}:
                return None
            return {
                "index_name": index_name,
                "schema_version": "test",
                "algorithm": "leiden",
                "entity_count": 3,
                "relationship_count": 2,
                "document_count": 1,
                "communities": [{
                    "community_id": "C1",
                    "entity_names": ["RAG"],
                    "source_filenames": ["report.pdf"],
                    "report": "test",
                }],
            }

        graph_store.load_graphrag_index.side_effect = load_index
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.object(mcp_server, "kb", None), patch.object(
            mcp_server, "loaded_workspace_path", None
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.get(
                "/diagnostics/graphrag",
                headers={"X-SoulDrive-Token": "test-token"},
            )

    assert response.status_code == 200
    assert response.json()["all_projects"]["ready"] is True
    assert response.json()["all_projects"]["index_name"] == "all_projects"


def test_graphrag_diagnostics_reports_not_built_when_index_is_absent():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.load_graphrag_index.return_value = None
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "not_built"
    assert response.json()["reason"] == "graphrag_index_not_built"


def test_graphrag_diagnostics_does_not_report_scoped_index_ready_without_metadata_provider():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        snapshot = {
            "documents": [{
                "source_filename": "项目论文.pdf",
                "title": "RAG",
                "summary": "检索增强生成",
                "keywords": ["RAG"],
                "chunk_ids": ["project-child"],
            }],
            "entities": [{"name": "RAG"}],
            "relationships": [],
        }
        chunk_project_ids = {"project-child": "project-a"}
        graph_store = MagicMock()
        graph_store.get_graph_snapshot.return_value = snapshot
        graph_store.load_graphrag_index.return_value = {
            "schema_version": "souldrive_graphrag_index_v3",
            "algorithm": "leiden_v1_local_move_refine_hierarchy",
            "source_snapshot_digest": _graph_snapshot_digest(snapshot),
            "source_scope_digest": _graph_scope_digest(
                snapshot,
                chunk_project_ids,
                project_id=None,
                global_only=True,
            ),
            "scope_metadata_required": True,
            "global_only": True,
            "index_name": "global_shared",
            "communities": [{
                "community_id": "C1",
                "entity_names": ["RAG"],
                "source_filenames": ["项目论文.pdf"],
                "report": "检索增强生成",
            }],
        }
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.object(mcp_server, "kb", None), patch.object(
            mcp_server, "loaded_workspace_path", None
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["status"] == "unverified"
    assert response.json()["reason"] == "graphrag_scope_metadata_unverified"
    assert response.json()["scope_freshness"] == "unverified"


def test_graphrag_diagnostics_reports_building_from_indexing_state():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.load_graphrag_index.return_value = None
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            runtime_state.update_indexing_status(status="indexing")
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "building"
    assert response.json()["reason"] == "graphrag_index_building"
    assert response.json()["indexing_status"] == "indexing"


def test_graphrag_diagnostics_starts_a_missing_index_once_when_documents_exist():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.load_graphrag_index.return_value = None
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch("core.mcp_server._start_indexer_worker", return_value=object()) as start_worker, patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            runtime_state.update_indexing_status(status="completed", discovered_files=4)
            mcp_server._graphrag_auto_start_attempted.clear()
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})
            repeated = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "building"
    assert response.json()["reason"] == "graphrag_auto_build_started"
    assert response.json()["indexing_status"] == "queued"
    assert repeated.status_code == 200
    start_worker.assert_called_once()


def test_graphrag_diagnostics_reports_failed_build_from_indexer_observation():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.load_graphrag_index.return_value = None
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            runtime_state.update_indexing_status(
                status="failed",
                observability={
                    "graphrag": {
                        "status": "failed",
                        "error": "community build failed secret=graph-token",
                    }
                },
            )
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["reason"] == "graphrag_index_build_failed"
    assert "error" not in response.json()
    assert response.json()["error_type"] == "UnknownError"
    assert response.json()["error_chars"] == len("community build failed secret=graph-token")
    assert len(response.json()["error_digest"]) == 16
    assert "graph-token" not in response.text


def test_graphrag_diagnostics_preserves_unavailable_snapshot_status():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch(
            "core.graph_rag.GraphRAGIndex.diagnostics",
            return_value={
                "ready": False,
                "status": "unavailable",
                "reason": "graphrag_source_snapshot_unavailable",
                "error_type": "RuntimeError",
                "error_chars": 32,
                "error_digest": "a" * 16,
            },
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            runtime_state.update_indexing_status(status="completed")
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason"] == "graphrag_source_snapshot_unavailable"
    assert response.json()["error_digest"] == "a" * 16


def test_graphrag_diagnostics_reports_degraded_project_build():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        graph_store = MagicMock()
        graph_store.load_graphrag_index.return_value = None
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch(
            "core.mcp_server.SecureGraphStore", return_value=graph_store
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            runtime_state.update_indexing_status(
                status="completed",
                observability={
                    "graphrag": {
                        "status": "degraded",
                        "failed_project_ids": ["project-a"],
                    }
                },
            )
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["reason"] == "graphrag_index_build_degraded"
    assert response.json()["failed_project_ids"] == ["project-a"]


def test_document_import_binds_asset_to_active_project_and_is_repeatable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(temp_dir) / "rag-report.txt"
        source.write_text("RAG 通过检索增强生成。", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("RAG 项目", project_id="rag")
        finally:
            catalog.close()

        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            first = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)], project_id="rag")
            )
            second = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)], project_id="rag")
            )

        catalog = ProjectCatalog(workspace)
        try:
            asset_count = catalog.conn.execute("SELECT COUNT(*) FROM assets WHERE project_id = 'rag'").fetchone()[0]
            version_count = catalog.conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        finally:
            catalog.close()

    assert first["project_id"] == "rag"
    assert first["items"][0]["project_binding"]["status"] == "registered"
    assert second["items"][0]["project_binding"]["status"] == "already_registered"
    assert asset_count == 1
    assert version_count == 1


def test_document_import_without_project_is_global_by_default():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(temp_dir) / "global-rag.txt"
        source.write_text("RAG 的核心思想是先检索证据，再生成回答。", encoding="utf-8")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            result = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)])
            )
            library = mcp_server._document_library_payload(workspace)

        catalog = ProjectCatalog(workspace)
        try:
            asset_count = catalog.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        finally:
            catalog.close()

    assert result["scope"] == "global"
    assert result["project_id"] is None
    assert result["items"][0]["scope"] == "global"
    assert asset_count == 0
    assert library["documents"][0]["scope"] == "global"


def test_document_globalize_unbinds_project_asset_and_requests_reindex():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(temp_dir) / "project-rag.txt"
        source.write_text("RAG 项目资料。", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("项目资料", project_id="project-rag")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            imported = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)], project_id="project-rag")
            )
            audit = MagicMock()
            audit.append_event.side_effect = RuntimeError("audit unavailable")
            client = TestClient(mcp_server.app)
            with patch("core.mcp_server.current_audit_logger", return_value=audit):
                response = client.post(
                    "/documents/globalize",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"name": imported["items"][0]["name"]},
                )
            listed = client.get("/documents/list", headers={"X-SoulDrive-Token": "test-token"})

        catalog = ProjectCatalog(workspace)
        try:
            asset_count = catalog.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["scope"] == "global"
    assert response.json()["reindex_required"] is True
    assert "warning" in response.json()
    assert response.json()["previous_project_ids"] == ["project-rag"]
    assert listed.json()["documents"][0]["scope"] == "global"
    assert asset_count == 0


def test_document_globalize_uses_document_id_for_duplicate_secure_names():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "secret-passphrase")
        keys = unlock_keystore(workspace, "secret-passphrase")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("项目资料", project_id="project-rag")
        finally:
            catalog.close()
        store = SecureDocumentStore(workspace, keys)
        try:
            store.import_document_bytes("same-name.txt", b"project-a payload")
            store.import_document_bytes("same-name.txt", b"project-b payload")
            documents = store.iter_documents()
            selected_id = next(
                item["object_id"]
                for item in documents
                if store.read_document_bytes(item["object_id"]) == b"project-b payload"
            )
        finally:
            store.close()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.register_imported_document(
                "project-rag",
                "same-name",
                hashlib.md5(b"project-b payload").hexdigest(),
                "same-name.txt",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=keys), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/documents/globalize",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"document_id": selected_id},
            )

    assert response.status_code == 200
    assert response.json()["document_id"] == selected_id
    assert response.json()["previous_project_ids"] == ["project-rag"]
    assert response.json()["reindex_required"] is True


def test_document_globalize_refuses_experiment_evidence_reference():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(temp_dir) / "evidence-rag.txt"
        source.write_text("实验使用的 RAG 资料。", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("实验项目", project_id="experiment-project")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            imported = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)], project_id="experiment-project")
            )
            binding = imported["items"][0]["project_binding"]
            catalog = ProjectCatalog(workspace)
            try:
                experiment_id = catalog.create_experiment("experiment-project", "基线", experiment_id="experiment")
                catalog.create_experiment_run(
                    experiment_id,
                    "2026-08-29T00:00:00+08:00",
                    run_id="run",
                    evidence_version_id=binding["version_id"],
                )
            finally:
                catalog.close()
            client = TestClient(mcp_server.app)
            response = client.post(
                "/documents/globalize",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"name": imported["items"][0]["name"]},
            )

        catalog = ProjectCatalog(workspace)
        try:
            asset_count = catalog.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        finally:
            catalog.close()

    assert response.status_code == 409
    assert "实验记录引用" in response.json()["error"]
    assert asset_count == 1


def test_document_library_exposes_latest_indexing_diagnostics_and_statuses():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        papers_path = Path(workspace.papers_path)
        for name in ("indexed.txt", "failed.txt", "processing.txt", "pending.txt"):
            (papers_path / name).write_text(name, encoding="utf-8")

        runtime_state = {
            "indexing": {
                "status": "indexing",
                "current_file": "processing.txt",
                "failures": [{
                    "source_filename": "failed.txt",
                    "error_code": "NO_PARSEABLE_CHUNKS",
                    "category": "parser",
                    "reason": "no parseable chunks redis://user:password@private-host secret=index-token",
                }],
                "observability": {
                    "documents": [
                        {
                            "source_filename": "indexed.txt",
                            "status": "indexed",
                            "elapsed_ms": 48,
                            "parse_ms": 12,
                            "chunk_count": 3,
                        },
                        {
                            "source_filename": "failed.txt",
                            "status": "failed",
                            "elapsed_ms": 7,
                            "failed_page_count": 1,
                        },
                    ],
                },
            },
        }
        with patch("core.mcp_server.get_runtime_state", return_value=runtime_state), patch(
            "core.mcp_server._indexed_source_filenames", return_value={"indexed.txt"}
        ):
            library = mcp_server._document_library_payload(workspace)

    documents = {item["name"]: item for item in library["documents"]}
    assert library["indexed_count"] == 1
    assert documents["indexed.txt"]["index_status"] == "indexed"
    assert documents["indexed.txt"]["index_observation"]["chunk_count"] == 3
    assert documents["failed.txt"]["index_status"] == "failed"
    assert documents["failed.txt"]["index_failure"] == {
        "error_code": "NO_PARSEABLE_CHUNKS",
        "category": "parser",
        "phase": "parse",
        "reason": "未提取到可用内容",
        "error_observation": mcp_server._message_observation(
            "no parseable chunks redis://user:password@private-host secret=index-token"
        ),
    }
    assert "index-token" not in str(documents["failed.txt"])
    assert "private-host" not in str(documents["failed.txt"])
    assert documents["failed.txt"]["index_observation"]["failed_page_count"] == 1
    assert documents["processing.txt"]["index_status"] == "processing"
    assert documents["pending.txt"]["index_status"] == "pending"
    assert mcp_server._bounded_indexing_reason(r"Permission denied: D:\secret\doc.pdf") == "Permission denied: <local-path>"


def test_document_library_marks_indexed_source_changed_after_hash_drift():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(workspace.papers_path) / "changed.txt"
        source.write_text("new source bytes", encoding="utf-8")
        runtime_state = {"indexing": {"status": "idle"}}
        with patch("core.mcp_server.get_runtime_state", return_value=runtime_state), patch(
            "core.mcp_server._indexed_source_filenames", return_value={"changed.txt"}
        ), patch(
            "core.mcp_server._indexed_source_hashes", return_value={"changed.txt": {"old-hash"}}
        ):
            library = mcp_server._document_library_payload(workspace)
        with patch("core.mcp_server.get_runtime_state", return_value=runtime_state), patch(
            "core.mcp_server._indexed_source_filenames", return_value={"changed.txt"}
        ), patch(
            "core.mcp_server._indexed_source_hashes",
            return_value={"changed.txt": {mcp_server.file_md5(source)}},
        ):
            current_library = mcp_server._document_library_payload(workspace)

    document = next(item for item in library["documents"] if item["name"] == "changed.txt")
    assert document["index_present"] is True
    assert document["indexed"] is False
    assert document["index_status"] == "changed"
    assert document["source_changed"] is True
    current_document = next(item for item in current_library["documents"] if item["name"] == "changed.txt")
    assert current_document["indexed"] is True
    assert current_document["index_status"] == "indexed"
    assert current_document["source_changed"] is False


def test_index_preview_returns_read_only_manifest_diff():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        library = {
            "documents": [
                {
                    "name": "new.txt",
                    "relative_path": "new.txt",
                    "index_present": False,
                    "index_status": "pending",
                    "source_changed": False,
                    "index_freshness": "unindexed",
                },
                {
                    "name": "changed.txt",
                    "relative_path": "changed.txt",
                    "index_present": True,
                    "index_status": "changed",
                    "source_changed": True,
                    "index_freshness": "changed",
                },
                {
                    "name": "same.txt",
                    "relative_path": "same.txt",
                    "index_present": True,
                    "index_status": "indexed",
                    "source_changed": False,
                    "index_freshness": "current",
                },
                {
                    "name": "legacy.txt",
                    "relative_path": "legacy.txt",
                    "index_present": True,
                    "index_status": "indexed",
                    "source_changed": False,
                    "index_freshness": "unverified",
                },
            ]
        }
        with patch(
            "core.mcp_server._document_library_payload",
            return_value=library,
        ), patch(
            "core.mcp_server._indexed_source_filenames",
            return_value={"same.txt", "changed.txt", "legacy.txt", "deleted.txt"},
        ), patch(
            "core.mcp_server._indexed_source_hashes",
            return_value={"same.txt": {"hash"}, "deleted.txt": {"hash"}},
        ):
            preview = mcp_server._index_preview_payload(workspace)

    assert preview["schema_version"] == "souldrive_index_manifest_diff_v0.1"
    assert preview["read_only"] is True
    assert [item["name"] for item in preview["added"]] == ["new.txt"]
    assert [item["name"] for item in preview["modified"]] == ["changed.txt"]
    assert [item["name"] for item in preview["deleted"]] == ["deleted.txt"]
    assert [item["name"] for item in preview["unchanged"]] == ["same.txt"]
    assert [item["name"] for item in preview["unverified"]] == ["legacy.txt"]
    assert preview["counts"] == {
        "added": 1,
        "modified": 1,
        "deleted": 1,
        "unchanged": 1,
        "unverified": 1,
        "blocked": 0,
    }
