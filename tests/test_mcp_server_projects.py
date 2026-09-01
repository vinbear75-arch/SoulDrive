import tempfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from core import mcp_server
from core.conversation_store import ConversationStore
from core.project_catalog import ProjectCatalog
from core.state_backends import ProductionSessionStore
from core.workspace import SoulDriveWorkspace
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
    assert created.status_code == 200
    assert created.json()["status"] == "created"


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
        store.close()

    assert global_created.status_code == 200
    assert global_created.json()["session"]["project_id"] is None
    assert project_created.status_code == 200
    assert project_created.json()["session"]["project_id"] == "project"
    assert len(global_list.json()["sessions"]) == 1
    assert len(project_list.json()["sessions"]) == 1


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


def test_project_delete_failure_keeps_catalog_record_and_root():
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
            "core.mcp_server._delete_project_indexes", side_effect=RuntimeError("index unavailable")
        ), patch("core.mcp_server.current_audit_logger"):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.delete("/projects/delete-failed", headers={"X-SoulDrive-Token": "test-token"})

        catalog = ProjectCatalog(workspace)
        try:
            project = catalog.conn.execute(
                "SELECT project_id FROM projects WHERE project_id = 'delete-failed'"
            ).fetchone()
            root_preserved = project_root.exists()
        finally:
            catalog.close()

    assert response.status_code == 500
    assert response.json()["error"] == "项目数据清理失败，项目目录记录已保留，请修复后重试"
    assert project is not None
    assert root_preserved


def test_experiment_metric_import_is_confirmation_gated_and_comparable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        source = Path(workspace.papers_path) / "metrics.csv"
        source.write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("实验项目", project_id="metrics")
            catalog.create_asset("metrics", "指标记录", "experiment_metrics", asset_id="metrics-asset")
            catalog.create_asset_version("metrics-asset", "metrics-hash", "metrics.csv", version_id="metrics-version")
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
    assert comparison.json()["status"] == "comparable"


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
                observability={"graphrag": {"status": "failed", "error": "community build failed"}},
            )
            client = TestClient(mcp_server.app)
            response = client.get("/diagnostics/graphrag", headers={"X-SoulDrive-Token": "test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["reason"] == "graphrag_index_build_failed"
    assert response.json()["error"] == "community build failed"


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
        ), patch("core.mcp_server.current_audit_logger"), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            imported = mcp_server._import_documents(
                mcp_server.PaperImportRequest(source_paths=[str(source)], project_id="project-rag")
            )
            client = TestClient(mcp_server.app)
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
    assert response.json()["previous_project_ids"] == ["project-rag"]
    assert listed.json()["documents"][0]["scope"] == "global"
    assert asset_count == 0


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
