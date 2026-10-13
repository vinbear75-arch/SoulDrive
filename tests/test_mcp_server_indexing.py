import tempfile
import unittest
from base64 import b64decode
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.parent_doc_store import ParentDocumentStore
import core.runtime_state as runtime_state
from core.security_context import WORKSPACE_DATA_KEY_ENV, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class McpServerIndexingTests(unittest.TestCase):
    def setUp(self):
        self.original_indexer_process = mcp_server.indexer_process
        mcp_server.indexer_process = None

    def tearDown(self):
        mcp_server.indexer_process = self.original_indexer_process

    def test_index_run_starts_worker_for_active_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            fake_process = Mock()
            fake_process.poll.return_value = None

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ), patch(
                "core.mcp_server.subprocess.Popen",
                return_value=fake_process,
            ) as popen:
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={
                        "X-SoulDrive-Token": "test-token",
                        "Idempotency-Key": "index-command-key",
                    },
                )
                state = runtime_state.get_runtime_state()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["started"])
        self.assertEqual(state["indexing"]["status"], "queued")
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("--workspace-path", command)
        self.assertIn(workspace.root_path, command)
        self.assertEqual(command[command.index("--run-id") + 1], response.json()["run_id"])
        self.assertEqual(command[command.index("--idempotency-key") + 1], "index-command-key")

    def test_index_preview_is_read_only_for_active_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            preview = {
                "schema_version": "souldrive_index_manifest_diff_v0.1",
                "read_only": True,
                "counts": {"added": 1, "modified": 0, "deleted": 0, "unchanged": 0, "unverified": 0, "blocked": 0},
            }
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.object(
                mcp_server,
                "_index_preview_payload",
                return_value=preview,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/index/preview",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["read_only"])
        self.assertEqual(response.json()["schema_version"], "souldrive_index_manifest_diff_v0.1")

    def test_backup_diagnostics_is_read_only_and_reports_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "test-passphrase")
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "test-passphrase"))
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/diagnostics/backup",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["read_only"])
        self.assertEqual(
            response.json()["schema_version"],
            "souldrive_workspace_backup_preflight_v0.1",
        )
        self.assertTrue(response.json()["ready"])
        self.assertIn("config/keystore.json", response.json()["key_dependencies"])

    def test_index_run_passes_workspace_data_key_to_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            fake_process = Mock()
            fake_process.poll.return_value = None

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ), patch(
                "core.mcp_server.subprocess.Popen",
                return_value=fake_process,
            ) as popen:
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 202)
        worker_env = popen.call_args.kwargs["env"]
        self.assertIn(WORKSPACE_DATA_KEY_ENV, worker_env)
        self.assertEqual(b64decode(worker_env[WORKSPACE_DATA_KEY_ENV].encode("ascii")), keys.workspace_data_key)

    def test_index_run_rejects_when_worker_is_already_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            running_process = Mock()
            running_process.poll.return_value = None
            mcp_server.indexer_process = running_process

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "already_running")

    def test_runtime_status_marks_unexpected_worker_exit_as_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            failed_process = Mock()
            failed_process.poll.return_value = 17
            mcp_server.indexer_process = failed_process

            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.update_indexing_status(
                    status="indexing",
                    run_id="run-1",
                    current_file="broken.pdf",
                )
                state = mcp_server.public_runtime_state()

        self.assertEqual(state["indexing"]["status"], "failed")
        self.assertEqual(state["indexing"]["run_id"], "run-1")
        self.assertEqual(state["indexing"]["failure_summary"], {"INDEXER_WORKER_EXITED": 1})
        self.assertEqual(state["indexing"]["failures"][0]["source_filename"], "broken.pdf")

    def test_public_runtime_state_redacts_index_failure_details(self):
        secret = "redis://user:password@private-host:6379/0 secret=index-token"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runtime_state, "STATE_PATH", str(Path(temp_dir) / "runtime_state.json")
        ):
            runtime_state.update_indexing_status(
                status="failed",
                failures=[{
                    "source_filename": r"D:\\private\\secret.pdf",
                    "error_code": "INDEXING_FAILED",
                    "category": "unknown",
                    "reason": secret,
                }],
            )
            state = mcp_server.public_runtime_state()

        failure = state["indexing"]["failures"][0]
        self.assertEqual(failure["source_filename"], "secret.pdf")
        self.assertEqual(failure["reason"], "索引失败")
        self.assertEqual(failure["error_observation"]["error_type"], "UnknownError")
        self.assertRegex(failure["error_observation"]["error_digest"], r"^[0-9a-f]{16}$")
        self.assertNotIn(secret, str(state))
        self.assertNotIn("index-token", str(state))
        self.assertNotIn(r"D:\\private", str(state))

    def test_public_runtime_state_redacts_legacy_nested_index_observability(self):
        secret = "graphrag backend=redis://user:password@private-host secret=observability-token"
        state = {
            "workspace_path": r"D:\private\workspace",
            "indexing": {
                "status": "failed",
                "observability": {
                    "documents": [{
                        "source_filename": r"D:\private\secret.pdf",
                        "status": "failed",
                        "reason": secret,
                    }],
                    "graphrag": {
                        "status": "failed",
                        "error": secret,
                        "project_indexes": [{
                            "project_id": "project-a",
                            "status": "failed",
                            "error": secret,
                        }],
                    },
                },
            },
        }
        with patch.object(mcp_server, "_reconcile_indexer_state"), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ):
            public = mcp_server.public_runtime_state()

        observability = public["indexing"]["observability"]
        document = observability["documents"][0]
        graph = observability["graphrag"]
        self.assertEqual(document["source_filename"], "secret.pdf")
        self.assertEqual(document["error_observation"]["error_type"], "UnknownError")
        self.assertNotIn("reason", document)
        self.assertEqual(graph["error_observation"]["error_type"], "UnknownError")
        self.assertEqual(graph["project_indexes"][0]["error_observation"]["error_type"], "UnknownError")
        self.assertNotIn(secret, str(public))
        self.assertNotIn("observability-token", str(public))
        self.assertNotIn(r"D:\private", str(public))

    def test_chat_does_not_wait_for_worker_that_already_exited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            failed_process = Mock()
            failed_process.poll.return_value = 17
            mcp_server.indexer_process = failed_process

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server, "ensure_runtime_loaded", side_effect=AssertionError("test stops before model load")
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.update_indexing_status(status="indexing", current_file="broken.pdf")
                response = TestClient(mcp_server.app).post(
                    "/chat",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "测试问题"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("等待索引完成", response.text)

    def test_chat_workspace_startup_error_does_not_expose_raw_exception(self):
        secret = "workspace-root=D:\\private\\workspace secret=runtime-token"
        with patch.object(mcp_server, "get_runtime_state", return_value={"indexing": {"status": "idle"}}), patch.object(
            mcp_server, "current_workspace", side_effect=RuntimeError(secret)
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            response = TestClient(mcp_server.app).post(
                "/chat",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"query": "测试工作区异常"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("本地工作区暂不可用，请检查工作区状态后重试。", response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("runtime-token", response.text)

    def test_index_run_rejects_hardware_only_workspace_before_software_unlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["reason"], "workspace passphrase required")

    def test_index_run_rejects_locked_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["status"], "locked")

    def test_chat_is_rejected_while_indexing_before_runtime_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server, "ensure_runtime_loaded", side_effect=AssertionError("runtime must stay unloaded")
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.update_indexing_status(status="indexing")
                response = TestClient(mcp_server.app).post(
                    "/chat",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "测试问题", "session_id": "test-session"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("等待索引完成", response.text)

    def test_indexed_document_preview_returns_cleaned_parent_chunks_in_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            store = ParentDocumentStore(workspace.parent_doc_path)
            try:
                store.upsert_parent(
                    "report_parent_1",
                    "第二页的清洗正文。",
                    {"source_filename": "report.pdf", "parent_index": 1, "page_number": 2, "section": "结论", "child_ids": ["c2"]},
                )
                store.upsert_parent(
                    "report_parent_0",
                    "第一页的清洗正文。",
                    {"source_filename": "report.pdf", "parent_index": 0, "page_number": 1, "section": "方法", "child_ids": ["c0", "c1"]},
                )
            finally:
                store.close()

            preview = mcp_server._indexed_document_preview(workspace, "report.pdf")

        self.assertFalse(preview["truncated"])
        self.assertEqual([chunk["page"] for chunk in preview["chunks"]], [1, 2])
        self.assertEqual(preview["chunks"][0]["child_count"], 2)
        self.assertIn("第一页的清洗正文", preview["text"])
        self.assertLess(preview["text"].index("第一页"), preview["text"].index("第二页"))

    def test_indexed_document_preview_requires_scope_for_project_bound_same_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            store = ParentDocumentStore(workspace.parent_doc_path)
            try:
                store.upsert_parent(
                    "project_a_report",
                    "项目 A 的私有预览。",
                    {"source_filename": "同名报告.pdf", "project_id": "project-a", "parent_index": 0},
                )
                store.upsert_parent(
                    "project_b_report",
                    "项目 B 的私有预览。",
                    {"source_filename": "同名报告.pdf", "project_id": "project-b", "parent_index": 0},
                )
            finally:
                store.close()

            unscoped = mcp_server._indexed_document_preview(workspace, "同名报告.pdf")
            scoped = mcp_server._indexed_document_preview(
                workspace,
                "同名报告.pdf",
                project_id="project-a",
            )

        self.assertIn("scope_error", unscoped)
        self.assertEqual(unscoped["chunks"], [])
        self.assertIn("项目 A 的私有预览", scoped["text"])
        self.assertNotIn("项目 B 的私有预览", scoped["text"])

    def test_indexed_document_preview_uses_document_id_for_duplicate_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            store = ParentDocumentStore(workspace.parent_doc_path)
            try:
                store.upsert_parent(
                    "duplicate_a",
                    "对象 A 的清洗正文。",
                    {
                        "source_filename": "同名报告.pdf",
                        "document_id": "secure_object-a",
                        "parent_index": 0,
                    },
                )
                store.upsert_parent(
                    "duplicate_b",
                    "对象 B 的清洗正文。",
                    {
                        "source_filename": "同名报告.pdf",
                        "document_id": "secure_object-b",
                        "parent_index": 0,
                    },
                )
            finally:
                store.close()

            preview = mcp_server._indexed_document_preview(
                workspace,
                "同名报告.pdf",
                document_id="object-b",
            )

        self.assertEqual([chunk["id"] for chunk in preview["chunks"]], ["duplicate_b"])
        self.assertIn("对象 B 的清洗正文", preview["text"])
        self.assertNotIn("对象 A 的清洗正文", preview["text"])

    def test_retrieval_diagnostics_returns_bounded_chunks_without_loading_chat_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            fake_kb = Mock()
            fake_kb.search_with_evidence.return_value = {
                "documents": ["完整的父级检索上下文，包含章节正文。"],
                "metadatas": [{
                    "source_filename": "report.pdf",
                    "page_number": 3,
                    "section_path": ["方法", "检索"],
                    "child_id": "child-1",
                }],
                "evidence": [{
                    "id": "E1",
                    "candidate_id": "child-1",
                    "source_filename": "report.pdf",
                    "section": "检索",
                    "section_path": ["方法", "检索"],
                    "page": 3,
                    "score": 0.42,
                    "support_score": 0.8,
                    "support_summary": "命中检索主题",
                    "support_terms": ["检索"],
                    "section_route_mode": "exact_node",
                    "section_route_coverage": 1.0,
                }],
                "retrieval_mode": "test",
                "observability": {
                    "stage_counts": {"final": 1},
                    "project_focus": {"route": "global"},
                    "sufficiency": {"decision": "accept"},
                    "timings_ms": {"total": 12},
                },
            }
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
            ), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch(
                "core.mcp_server.ensure_knowledge_base_loaded",
                return_value=(fake_kb, workspace),
            ) as ensure_kb, patch(
                "core.mcp_server.current_audit_logger"
            ):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/diagnostics/retrieval",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "检索方法是什么？", "top_k": 4},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "retrieval_test_v1")
        self.assertEqual(payload["project_route"], "global")
        self.assertTrue(payload["query_variants"])
        self.assertEqual(payload["chunks"][0]["section_path"], ["方法", "检索"])
        self.assertEqual(payload["chunks"][0]["content"], "完整的父级检索上下文，包含章节正文。")
        fake_kb.search_with_evidence.assert_called_once_with(
            query="检索方法是什么？",
            top_k=4,
            project_id=None,
            global_only=True,
        )
        ensure_kb.assert_called_once()

    def test_retrieval_diagnostics_applies_bound_workflow_query_focus(self):
        fake_kb = Mock()
        fake_kb.search_with_evidence.return_value = {
            "documents": [],
            "metadatas": [],
            "evidence": [],
            "retrieval_mode": "test",
            "observability": {"project_focus": {"route": "scoped"}},
        }

        with patch.object(
            mcp_server,
            "ensure_knowledge_base_loaded",
            return_value=(fake_kb, None),
        ), patch.object(
            mcp_server,
            "_retrieval_feedback_for_query",
            return_value=[],
        ):
            payload = mcp_server._run_retrieval_test(
                "请比较两次实验的指标变化",
                4,
                "project-a",
                workflow_id="experiment_compare",
            )

        call = fake_kb.search_with_evidence.call_args.kwargs
        self.assertEqual(payload["workflow_id"], "experiment_compare")
        self.assertEqual(payload["workflow_retrieval_variant_count"], 2)
        self.assertEqual(len(call["extra_query_variants"]), 2)
        self.assertTrue(any("指标" in item and "数据集" in item for item in call["extra_query_variants"]))
        self.assertTrue(any("版本" in item and "配置" in item for item in call["extra_query_variants"]))
        self.assertFalse(call["global_only"])

    def test_retrieval_diagnostics_reports_when_legacy_backend_drops_workflow_focus(self):
        calls = []

        class LegacyKnowledgeBase:
            def search_with_evidence(self, *, query, top_k, project_id, global_only):
                calls.append((query, top_k, project_id, global_only))
                return {
                    "documents": [],
                    "metadatas": [],
                    "evidence": [],
                    "retrieval_mode": "legacy",
                    "observability": {"project_focus": {"route": "scoped"}},
                }

        with patch.object(
            mcp_server,
            "ensure_knowledge_base_loaded",
            return_value=(LegacyKnowledgeBase(), None),
        ), patch.object(
            mcp_server,
            "_retrieval_feedback_for_query",
            return_value=[],
        ):
            payload = mcp_server._run_retrieval_test(
                "请比较两次实验的指标变化",
                4,
                "project-a",
                workflow_id="experiment_compare",
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["workflow_retrieval_variant_count"], 0)

    def test_retrieval_diagnostics_keeps_result_when_audit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            fake_kb = Mock()
            fake_kb.search_with_evidence.return_value = {
                "documents": ["诊断结果仍应返回。"],
                "metadatas": [{"source_filename": "report.pdf", "page_number": 2}],
                "evidence": [{
                    "id": "E1",
                    "candidate_id": "child-1",
                    "source_filename": "report.pdf",
                    "section": "结论",
                    "page": 2,
                    "score": 0.8,
                }],
                "retrieval_mode": "test",
                "observability": {"project_focus": {"route": "global"}},
            }

            class FailingAuditLogger:
                def append_event(self, *_args, **_kwargs):
                    raise OSError("audit sink failed for secret-diagnostic-query")

            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
            ), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch(
                "core.mcp_server.ensure_knowledge_base_loaded",
                return_value=(fake_kb, workspace),
            ), patch(
                "core.mcp_server.current_audit_logger", return_value=FailingAuditLogger()
            ):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/diagnostics/retrieval",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "secret-diagnostic-query", "top_k": 4},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chunks"][0]["content"], "诊断结果仍应返回。")
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-diagnostic-query", response.json()["warning"])

    def test_retrieval_diagnostics_preserves_business_failure_when_failure_audit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            fake_kb = Mock()
            fake_kb.search_with_evidence.side_effect = RuntimeError("query=secret-query")

            class FailingAuditLogger:
                def append_event(self, *_args, **_kwargs):
                    raise OSError("audit sink failed for secret-query")

            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
            ), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch(
                "core.mcp_server.ensure_knowledge_base_loaded",
                return_value=(fake_kb, workspace),
            ), patch(
                "core.mcp_server.current_audit_logger", return_value=FailingAuditLogger()
            ):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/diagnostics/retrieval",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "secret-query", "top_k": 4},
                )

        self.assertEqual(response.status_code, 503)
        self.assertIn("检索测试暂不可用", response.json()["error"])
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-query", response.json()["warning"])

    def test_retrieval_diagnostics_does_not_audit_query_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            fake_kb = Mock()
            fake_kb.search_with_evidence.side_effect = RuntimeError("query=secret-query")
            audit_logger = Mock()
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
            ), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch(
                "core.mcp_server.ensure_knowledge_base_loaded",
                return_value=(fake_kb, workspace),
            ), patch(
                "core.mcp_server.current_audit_logger", return_value=audit_logger
            ):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/diagnostics/retrieval",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "secret-query", "top_k": 4},
                )

        self.assertEqual(response.status_code, 503)
        event_name, event_payload = audit_logger.append_event.call_args.args
        self.assertEqual(event_name, "retrieval.diagnostics_failed")
        self.assertEqual(event_payload["error_type"], "RuntimeError")
        self.assertNotIn("secret-query", str(event_payload))

    def test_unbound_retrieval_diagnostics_fails_closed_without_global_scope_contract(self):
        class LegacyKnowledgeBase:
            def search_with_evidence(self, *, query, top_k, project_id=None):
                _ = query
                _ = top_k
                _ = project_id
                return {
                    "documents": ["project-a secret", "project-b secret"],
                    "metadatas": [
                        {"project_id": "project-a", "source_filename": "a.pdf"},
                        {"project_id": "project-b", "source_filename": "b.pdf"},
                    ],
                    "evidence": [],
                    "observability": {"project_focus": {"route": "legacy-mixed"}},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False
            ), patch(
                "core.mcp_server.current_workspace", return_value=workspace
            ), patch(
                "core.mcp_server.ensure_knowledge_base_loaded",
                return_value=(LegacyKnowledgeBase(), workspace),
            ), patch("core.mcp_server.current_audit_logger"):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/diagnostics/retrieval",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"query": "项目资料", "top_k": 4},
                )

        self.assertEqual(response.status_code, 503)
        self.assertIn("检索测试暂不可用", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
