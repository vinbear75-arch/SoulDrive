import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server, runtime_state
from core.project_catalog import ProjectCatalog
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


def test_presentation_http_flow_requires_exact_plan_confirmation():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "correct horse battery staple"))
        catalog = ProjectCatalog(workspace)
        catalog.create_project("PPT API 项目", project_id="ppt-api")
        catalog.close()

        parent_db = sqlite3.connect(workspace.parent_doc_path)
        try:
            parent_db.execute(
                "CREATE TABLE IF NOT EXISTS parent_documents "
                "(parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)"
            )
            parent_db.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "parent-report",
                    "项目采用本地加密存储，并按阶段完成验证。",
                    json.dumps({
                        "source_filename": "report.md",
                        "document_id": "secure_api-report",
                        "project_id": "ppt-api",
                        "page_number": 1,
                    }),
                ),
            )
            parent_db.commit()
        finally:
            parent_db.close()

        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ",
            {
                "SOULDRIVE_API_TOKEN": "test-token",
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
            },
            clear=False,
        ), patch.object(mcp_server, "_start_presentation_thread", return_value=False), patch.object(
            mcp_server,
            "_presentation_outline_with_local_model",
            return_value={"outline_source": "extractive", "slides": []},
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "test-token"}
            response = client.post(
                "/presentations/generate",
                headers=headers,
                json={
                    "request_id": "api-outline-1",
                    "project_id": "ppt-api",
                    "source_filenames": ["report.md"],
                    "source_document_ids": ["api-report"],
                    "title": "项目验证汇报",
                    "open_in_wps": False,
                },
            )
            assert response.status_code == 202
            plan_task_id = response.json()["task_id"]
            assert response.json()["kind"] == "presentation.plan"
            assert response.json()["project_id"] == "ppt-api"

            service = mcp_server.current_presentation_service()
            planned = service.run_task(plan_task_id, suppress_wps=True)
            assert planned["status"] == "completed"
            unscoped_list = client.get("/presentations/tasks", headers=headers)
            assert unscoped_list.status_code == 200
            assert plan_task_id not in {task["task_id"] for task in unscoped_list.json()["tasks"]}
            foreign_list = client.get(
                "/presentations/tasks",
                headers=headers,
                params={"project_id": "other-project"},
            )
            assert foreign_list.status_code == 200
            assert plan_task_id not in {task["task_id"] for task in foreign_list.json()["tasks"]}
            project_list = client.get(
                "/presentations/tasks",
                headers=headers,
                params={"project_id": "ppt-api"},
            )
            assert project_list.status_code == 200
            assert plan_task_id in {task["task_id"] for task in project_list.json()["tasks"]}

            missing_scope = client.get(f"/presentations/tasks/{plan_task_id}", headers=headers)
            assert missing_scope.status_code == 409
            foreign_scope = client.get(
                f"/presentations/tasks/{plan_task_id}",
                headers=headers,
                params={"project_id": "other-project"},
            )
            assert foreign_scope.status_code == 409
            outline = client.get(
                f"/presentations/tasks/{plan_task_id}",
                headers=headers,
                params={"project_id": "ppt-api"},
            )
            assert outline.status_code == 200
            plan_digest = outline.json()["result"]["plan_digest"]

            missing_resume = client.post(f"/presentations/tasks/{plan_task_id}/resume", headers=headers)
            assert missing_resume.status_code == 409
            foreign_resume = client.post(
                f"/presentations/tasks/{plan_task_id}/resume",
                headers=headers,
                params={"project_id": "other-project"},
            )
            assert foreign_resume.status_code == 409
            completed_resume = client.post(
                f"/presentations/tasks/{plan_task_id}/resume",
                headers=headers,
                params={"project_id": "ppt-api"},
            )
            assert completed_resume.status_code == 200

            foreign_confirm = client.post(
                f"/presentations/tasks/{plan_task_id}/confirm",
                headers=headers,
                params={"project_id": "other-project"},
                json={"plan_digest": plan_digest},
            )
            assert foreign_confirm.status_code == 409
            confirmed = client.post(
                f"/presentations/tasks/{plan_task_id}/confirm",
                headers=headers,
                params={"project_id": "ppt-api"},
                json={"plan_digest": plan_digest},
            )
            assert confirmed.status_code == 202
            assert confirmed.json()["kind"] == "presentation.generate"
            assert confirmed.json()["project_id"] == "ppt-api"
            generation_task_id = confirmed.json()["task_id"]
            repeated = client.post(
                f"/presentations/tasks/{plan_task_id}/confirm",
                headers=headers,
                params={"project_id": "ppt-api"},
                json={"plan_digest": plan_digest},
            )
            assert repeated.status_code == 202
            assert repeated.json()["task_id"] == generation_task_id

            foreign_cancel = client.post(
                f"/presentations/tasks/{generation_task_id}/cancel",
                headers=headers,
                params={"project_id": "other-project"},
            )
            assert foreign_cancel.status_code == 409
            cancelled = client.post(
                f"/presentations/tasks/{generation_task_id}/cancel",
                headers=headers,
                params={"project_id": "ppt-api"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            runtime_state.lock_runtime("test cleanup")
            mcp_server.cleanup_runtime()
    clear_workspace_keys(workspace.root_path)


def test_presentation_generate_replay_of_completed_plan_does_not_start_worker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        completed_task = {
            "task_id": "completed-plan-replay",
            "kind": "presentation.plan",
            "status": "completed",
            "project_id": None,
        }

        class FakePresentationService:
            def create_plan(self, _payload):
                return {**completed_task, "replayed": True}

            def get_task(self, _task_id):
                return dict(completed_task)

        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.current_presentation_service", return_value=FakePresentationService()
        ), patch("core.mcp_server._start_presentation_thread", return_value=False) as start_worker:
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/presentations/generate",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"request_id": "completed-plan-replay", "source_filenames": ["report.pdf"]},
            )

    assert response.status_code == 202
    assert response.json()["worker_started"] is False
    assert response.json()["replayed"] is True
    start_worker.assert_not_called()


def test_presentation_tool_generate_replay_of_completed_plan_does_not_start_worker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        completed_task = {
            "task_id": "completed-tool-plan-replay",
            "kind": "presentation.plan",
            "status": "completed",
            "project_id": None,
        }

        class FakePresentationService:
            def create_plan(self, _payload):
                return {**completed_task, "replayed": True}

            def get_task(self, _task_id):
                return dict(completed_task)

        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.current_presentation_service", return_value=FakePresentationService()
        ), patch("core.mcp_server._start_presentation_thread", return_value=False) as start_worker:
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/tools/call",
                headers={"X-SoulDrive-Token": "test-token"},
                json={
                    "name": "souldrive.presentation_generate",
                    "arguments": {
                        "request_id": "completed-tool-plan-replay",
                        "source_filenames": ["report.pdf"],
                    },
                },
            )

    assert response.status_code == 200
    assert response.json()["result"]["worker_started"] is False
    assert response.json()["result"]["replayed"] is True
    start_worker.assert_not_called()


def test_presentation_http_confirm_replay_of_terminal_generation_does_not_start_worker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        plan_task = {
            "task_id": "completed-http-confirm-plan",
            "kind": "presentation.plan",
            "status": "completed",
            "project_id": None,
        }
        generation_task = {
            "task_id": "completed-http-generation",
            "kind": "presentation.generate",
            "status": "completed",
            "project_id": None,
        }

        class FakePresentationService:
            def get_task(self, task_id):
                assert task_id == plan_task["task_id"]
                return dict(plan_task)

            def confirm_plan(self, _task_id, _plan_digest):
                return dict(generation_task)

        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.current_presentation_service", return_value=FakePresentationService()
        ), patch("core.mcp_server._start_presentation_thread", return_value=False) as start_worker:
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                f"/presentations/tasks/{plan_task['task_id']}/confirm",
                headers={"X-SoulDrive-Token": "test-token"},
                    json={"plan_digest": "digest-0123456789"},
            )

    assert response.status_code == 202
    assert response.json()["status"] == "completed"
    assert response.json()["worker_started"] is False
    start_worker.assert_not_called()


def test_presentation_tool_confirm_replay_of_terminal_generation_does_not_start_worker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        plan_task = {
            "task_id": "completed-tool-confirm-plan",
            "kind": "presentation.plan",
            "status": "completed",
            "project_id": None,
        }
        generation_task = {
            "task_id": "cancelled-tool-generation",
            "kind": "presentation.generate",
            "status": "cancelled",
            "project_id": None,
        }

        class FakePresentationService:
            def get_task(self, task_id):
                assert task_id == plan_task["task_id"]
                return dict(plan_task)

            def confirm_plan(self, _task_id, _plan_digest):
                return dict(generation_task)

        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ), patch("core.mcp_server.current_workspace", return_value=workspace), patch(
            "core.mcp_server.current_presentation_service", return_value=FakePresentationService()
        ), patch("core.mcp_server._start_presentation_thread", return_value=False) as start_worker:
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/tools/call",
                headers={"X-SoulDrive-Token": "test-token"},
                json={
                    "name": "souldrive.presentation_confirm",
                    "arguments": {
                        "plan_task_id": plan_task["task_id"],
                        "plan_digest": "digest",
                    },
                },
            )

    assert response.status_code == 200
    assert response.json()["result"]["status"] == "cancelled"
    assert response.json()["result"]["worker_started"] is False
    start_worker.assert_not_called()


def test_presentation_tool_path_reuses_project_scope_validation():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "correct horse battery staple"))
        catalog = ProjectCatalog(workspace)
        catalog.create_project("PPT 工具项目", project_id="ppt-tool")
        catalog.close()
        parent_db = sqlite3.connect(workspace.parent_doc_path)
        try:
            parent_db.execute(
                "CREATE TABLE IF NOT EXISTS parent_documents "
                "(parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)"
            )
            parent_db.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "tool-report",
                    "工具调用也必须经过项目范围校验。",
                    json.dumps({"source_filename": "report.md", "project_id": "ppt-tool", "page_number": 1}),
                ),
            )
            parent_db.commit()
        finally:
            parent_db.close()

        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
            "os.environ",
            {
                "SOULDRIVE_API_TOKEN": "test-token",
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
            },
            clear=False,
        ), patch.object(mcp_server, "_start_presentation_thread", return_value=False), patch.object(
            mcp_server,
            "_presentation_outline_with_local_model",
            return_value={"outline_source": "extractive", "slides": []},
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "test-token"}

            invalid_project = client.post(
                "/tools/call",
                headers=headers,
                json={
                    "name": "souldrive.presentation_generate",
                    "arguments": {
                        "request_id": "tool-invalid-project",
                        "project_id": "other-project",
                        "source_filenames": ["report.md"],
                        "open_in_wps": False,
                    },
                },
            )
            assert invalid_project.status_code == 400

            created = client.post(
                "/tools/call",
                headers=headers,
                json={
                    "name": "souldrive.presentation_generate",
                    "arguments": {
                        "request_id": "tool-plan-1",
                        "project_id": "ppt-tool",
                        "source_filenames": ["report.md"],
                        "open_in_wps": False,
                    },
                },
            )
            assert created.status_code == 200
            plan_task_id = created.json()["result"]["task_id"]
            service = mcp_server.current_presentation_service()
            planned = service.run_task(plan_task_id, suppress_wps=True)
            assert planned["status"] == "completed"
            plan_digest = service.get_task(plan_task_id)["result"]["plan_digest"]

            missing_scope = client.post(
                "/tools/call",
                headers=headers,
                json={
                    "name": "souldrive.presentation_confirm",
                    "arguments": {"plan_task_id": plan_task_id, "plan_digest": plan_digest},
                },
            )
            assert missing_scope.status_code == 400
            foreign_scope = client.post(
                "/tools/call",
                headers=headers,
                json={
                    "name": "souldrive.presentation_confirm",
                    "arguments": {
                        "plan_task_id": plan_task_id,
                        "plan_digest": plan_digest,
                        "project_id": "other-project",
                    },
                },
            )
            assert foreign_scope.status_code == 400
            confirmed = client.post(
                "/tools/call",
                headers=headers,
                json={
                    "name": "souldrive.presentation_confirm",
                    "arguments": {
                        "plan_task_id": plan_task_id,
                        "plan_digest": plan_digest,
                        "project_id": "ppt-tool",
                    },
                },
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["result"]["project_id"] == "ppt-tool"
            runtime_state.lock_runtime("test cleanup")
            mcp_server.cleanup_runtime()
        clear_workspace_keys(workspace.root_path)
