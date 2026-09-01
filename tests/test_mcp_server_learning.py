import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.conversation_store import ConversationStore
from core.learning_store import LearningStore
from core.project_catalog import ProjectCatalog
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class McpServerLearningTests(unittest.TestCase):
    def setUp(self):
        self.original_kb = mcp_server.kb
        self.original_rag = mcp_server.rag
        self.original_loaded_workspace_path = mcp_server.loaded_workspace_path
        self.addCleanup(self._restore_mcp_runtime)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # ProductionSessionStore owns a SQLite handle for the request session;
        # close it before TemporaryDirectory removes the workspace on Windows.
        self.addCleanup(mcp_server._close_session_store)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()
        initialize_keystore(self.workspace, "correct horse battery staple")
        self.keys = unlock_keystore(self.workspace, "correct horse battery staple")
        set_workspace_keys(self.workspace.root_path, self.keys)
        self.addCleanup(clear_workspace_keys)
        self.client = TestClient(mcp_server.app)
        self.env_patch = patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _mounted_state(self):
        return {
            "locked": False,
            "software_unlocked": True,
            "workspace_path": self.workspace.root_path,
            "auth_level": "HARDWARE_PLUS_PASSWORD",
        }

    def _headers(self):
        return {"X-SoulDrive-Token": "test-token"}

    def _restore_mcp_runtime(self):
        mcp_server.kb = self.original_kb
        mcp_server.rag = self.original_rag
        mcp_server.loaded_workspace_path = self.original_loaded_workspace_path

    def test_reflection_draft_requires_unlocked_workspace(self):
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value={"locked": True, "software_unlocked": False},
        ):
            response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "关闭前集中审核。"}]},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 423)

    def test_remote_skill_download_is_visible_as_pending_review(self):
        downloaded = {
            "status": "pending_review",
            "skill_name": "paper-summary",
            "skill_title": "Paper Summary",
            "description": "Summarize papers with citations",
            "source": "acme/research/skills/paper-summary",
            "path": str(Path(self.workspace.skills_path) / ".inbox" / "paper-summary"),
            "files": ["SKILL.md"],
        }
        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "install_skill_to_inbox",
            return_value=downloaded,
        ):
            response = self.client.post(
                "/learning/skills/install",
                json={"source": "acme/research/skills/paper-summary"},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending_review")
        self.assertEqual(response.json()["skill_name"], "paper-summary")

    def test_reflection_draft_returns_pending_candidates(self):
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=RuntimeError("no model available"),
        ):
            response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "我不希望每次问答后都弹窗保存记忆，希望只在关闭前集中审核。"}]},
                headers=self._headers(),
            )
            pending = self.client.get("/reflection/reviews/pending", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        self.assertGreaterEqual(len(payload["candidates"]), 1)
        self.assertEqual(payload["candidates"][0]["asset_type"], "memory")
        self.assertEqual(payload["candidates"][0]["subtype"], "user_preference")
        self.assertEqual(payload["candidates"][0]["write_gate"]["decision"], "review")
        self.assertTrue(payload["candidates"][0]["write_gate"]["requires_human_review"])
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(len(pending.json()["reviews"]), 1)

    def test_reflection_draft_filters_unbound_model_candidates(self):
        hallucinated_candidates = [
            {
                "asset_type": "memory",
                "subtype": "user_preference",
                "title": "Automatic saving",
                "content": "User prefers automatic saving after every answer.",
                "evidence": ["User prefers automatic saving after every answer."],
                "rationale": "Useful preference.",
                "confidence": 0.93,
                "selected_by_default": True,
                "source_message_indexes": [0],
                "tags": ["preference"],
            }
        ]
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "_generate_reflection_candidates",
            return_value=hallucinated_candidates,
        ):
            response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "Please remember review before saving."}]},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidates"], [])

    def test_decide_accept_creates_searchable_memory(self):
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=RuntimeError("no model available"),
        ):
            draft_response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "我不希望每次问答后都弹窗保存记忆，希望只在关闭前集中审核。"}]},
                headers=self._headers(),
            )
            draft = draft_response.json()
            candidate_id = draft["candidates"][0]["candidate_id"]
            decide_response = self.client.post(
                f"/reflection/reviews/{draft['review_id']}/decide",
                json={"decisions": [{"candidate_id": candidate_id, "action": "accept"}]},
                headers=self._headers(),
            )
            memories = self.client.get("/learning/memories", headers=self._headers())
            search = self.client.post(
                "/learning/search",
                json={"query": "以后什么时候保存记忆", "top_k_memory": 3, "top_k_skill": 2},
                headers=self._headers(),
            )

        self.assertEqual(draft_response.status_code, 200)
        self.assertEqual(decide_response.status_code, 200)
        self.assertEqual(len(memories.json()["memories"]), 1)
        self.assertEqual(len(search.json()["memories"]), 1)
        self.assertEqual(search.json()["memories"][0]["title"], "关闭前集中审核记忆")

    def test_runtime_memory_is_saved_without_model_and_can_be_revoked(self):
        state = self._mounted_state()
        message = "以后请默认用中文回答，并在涉及用户偏好时提醒我可以撤销。"
        with patch.dict(
            os.environ,
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server, "_embed_durable_memory", side_effect=AssertionError("runtime memory must defer embedding")
        ):
            saved = self.client.post(
                "/learning/memories/runtime",
                json={"message": message, "session_id": "session-runtime", "project_id": "project-runtime"},
                headers=self._headers(),
            )
            memories = self.client.get("/learning/memories", headers=self._headers())
            memory_id = saved.json()["memory_id"]
            revoked = self.client.post(
                f"/learning/memories/{memory_id}/revoke",
                headers=self._headers(),
            )
            after_revoke = self.client.get("/learning/memories", headers=self._headers())

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["status"], "saved")
        self.assertEqual(len(memories.json()["memories"]), 1)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["status"], "revoked")
        self.assertEqual(after_revoke.json()["memories"], [])

    def test_runtime_memory_ignores_non_profile_message(self):
        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.post(
                "/learning/memories/runtime",
                json={"message": "请总结当前文档的第三节。"},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.assertEqual(response.json()["reason"], "no_explicit_profile_signal")

    def test_memory_management_exposes_scope_source_and_supports_edit_revoke_and_delete(self):
        store = LearningStore(self.workspace, self.keys)
        review = store.create_review("manual", [{
            "asset_type": "memory",
            "subtype": "project_fact",
            "title": "项目旧偏好",
            "content": "old_project_preference_token",
            "evidence": ["用户明确确认项目范围偏好。"],
            "rationale": "项目内可复用偏好。",
            "confidence": 0.9,
            "selected_by_default": True,
            "source_message_indexes": [2],
            "source_excerpt": "项目范围原始表述。",
        }])
        accepted = store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
            project_id="project-a",
        )
        memory_id = accepted["accepted_assets"][0]["memory_id"]
        [before] = store.list_memories(include_metadata=True)
        store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            listed = self.client.get(
                "/learning/memories?project_id=project-a",
                headers=self._headers(),
            )
            listed_other = self.client.get(
                "/learning/memories?project_id=project-b",
                headers=self._headers(),
            )
            updated = self.client.patch(
                f"/learning/memories/{memory_id}",
                json={
                    "title": "项目新偏好",
                    "content": "new_project_preference_token",
                    "expected_updated_at": before["updated_at"],
                },
                headers=self._headers(),
            )
            stale = self.client.patch(
                f"/learning/memories/{memory_id}",
                json={
                    "title": "过期编辑",
                    "content": "stale_token",
                    "expected_updated_at": before["updated_at"],
                },
                headers=self._headers(),
            )
            missing_confirm = self.client.delete(
                f"/learning/memories/{memory_id}",
                headers=self._headers(),
            )
            deleted = self.client.delete(
                f"/learning/memories/{memory_id}?confirm=true",
                headers=self._headers(),
            )
            after_delete = self.client.get(
                "/learning/memories?project_id=project-a",
                headers=self._headers(),
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["memories"][0]["scope"], "project")
        self.assertEqual(listed.json()["memories"][0]["project_id"], "project-a")
        self.assertEqual(listed.json()["memories"][0]["source_trace"]["message_indexes"], [2])
        self.assertEqual(listed.json()["scope_policy"], "project_plus_global")
        self.assertEqual(listed_other.json()["memories"], [])
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["memory"]["title"], "项目新偏好")
        self.assertEqual(updated.json()["memory"]["record_version"], 2)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(missing_confirm.status_code, 400)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["status"], "deleted")
        self.assertEqual(after_delete.json()["memories"], [])


    def test_chat_passes_learning_context_to_rag_generation(self):
        fake_rag = _FakeRag()
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ), patch.object(mcp_server.runtime_metrics, "record_chat_latency") as record_chat_latency:
            draft_response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "我不希望每次问答后都弹窗保存记忆，希望只在关闭前集中审核。"}]},
                headers=self._headers(),
            )
            candidate_id = draft_response.json()["candidates"][0]["candidate_id"]
            self.client.post(
                f"/reflection/reviews/{draft_response.json()['review_id']}/decide",
                json={"decisions": [{"candidate_id": candidate_id, "action": "accept"}]},
                headers=self._headers(),
            )
            response = self.client.post(
                "/chat",
                json={"query": "以后什么时候保存记忆？", "top_k": 3},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(len(fake_rag.learning_context["memories"]), 1)
        self.assertEqual(fake_rag.learning_context["memories"][0]["title"], "关闭前集中审核记忆")
        self.assertTrue(record_chat_latency.call_args.kwargs["success"])

    def test_chat_passes_request_scoped_interaction_context_to_rag(self):
        fake_rag = _FakeRag()
        archive = ConversationStore(self.workspace.conversation_store_path, self.keys.metadata_key)
        archive.create_session("session-a", project_id=None, title="当前会话")
        archive.close()
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ):
            response = self.client.post(
                "/chat",
                json={
                    "query": "这份文档的结论是什么？",
                    "session_id": "session-a",
                    "query_timestamp_ms": 0,
                    "history": [
                        {"role": "user", "content": "先看当前文档", "timestamp_ms": 0}
                    ],
                    "perception_events": [
                        {
                            "event_id": "document-1",
                            "modality": "document",
                            "timestamp_ms": 0,
                            "source_name": "隐私方案.pdf",
                            "text": "用户当前选择了隐私方案",
                            "selected_text": "本地加密存储保护隐私。",
                            "confidence": 1.0,
                            "bbox": {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3},
                        }
                    ],
                    "active_context": {
                        "document_name": "隐私方案.pdf",
                        "page_number": 3,
                        "selected_text": "本地加密存储保护隐私。",
                        "selected_region": {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3},
                    },
                },
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_rag.interaction_context["session_id"], "session-a")
        self.assertEqual(fake_rag.interaction_context["query_timestamp_ms"], 0)
        self.assertEqual(fake_rag.interaction_context["active_context"]["page_number"], 3)
        self.assertEqual(fake_rag.interaction_context["active_context"]["selected_text"], "本地加密存储保护隐私。")
        self.assertEqual(fake_rag.interaction_context["active_context"]["selected_region"]["width"], 0.4)
        self.assertEqual(fake_rag.interaction_context["events"][0]["modality"], "document")
        self.assertEqual(fake_rag.interaction_context["events"][0]["selected_text"], "本地加密存储保护隐私。")
        self.assertEqual(fake_rag.interaction_context["events"][0]["bbox"]["height"], 0.3)

    def test_project_chat_without_selected_document_reaches_global_rag_context(self):
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("全局知识问答项目", project_id="project-global-rag")
        finally:
            catalog.close()
        session_id = "project-global-rag-session"
        archive = ConversationStore(self.workspace.conversation_store_path, self.keys.metadata_key)
        archive.create_session(session_id, project_id="project-global-rag", title="主对话")
        archive.close()

        fake_rag = _FakeRag()
        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ):
            response = self.client.post(
                "/chat",
                json={
                    "query": "RAG 的核心思想是什么？",
                    "session_id": session_id,
                    "active_context": {"project_id": "project-global-rag"},
                },
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(
            fake_rag.interaction_context["active_context"]["project_id"],
            "project-global-rag",
        )
        self.assertNotIn("请明确要处理的文档", response.text)

    def test_api_project_session_chat_without_active_context_reaches_rag(self):
        """Mirror the UI flow: create project/session, then ask without a document selection."""
        fake_rag = _FakeRag()
        state = self._mounted_state()
        project_id = "api-global-rag-project"
        session_id = "api-global-rag-session"
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ):
            project = self.client.post(
                "/projects",
                json={"name": "API 全局问答项目", "project_id": project_id},
                headers=self._headers(),
            )
            session = self.client.post(
                f"/projects/{project_id}/sessions",
                json={"session_id": session_id, "title": "主会话"},
                headers=self._headers(),
            )
            response = self.client.post(
                "/chat",
                json={
                    "query": "RAG 的核心思想是什么？",
                    "session_id": session_id,
                },
                headers=self._headers(),
            )

        self.assertEqual(project.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(fake_rag.query, "RAG 的核心思想是什么？")
        self.assertEqual(
            fake_rag.interaction_context["active_context"]["project_id"],
            project_id,
        )
        self.assertFalse(fake_rag.interaction_context["active_context"].get("document_name"))
        self.assertEqual(fake_rag.interaction_context["active_context"].get("document_names") or [], [])
        self.assertNotIn("请明确要处理的文档", response.text)

    def test_chat_session_history_survives_runtime_store_reopen(self):
        fake_rag = _FakeRag()
        session_id = f"test-reopen-{uuid.uuid4().hex}"
        archive = ConversationStore(self.workspace.conversation_store_path, self.keys.metadata_key)
        archive.create_session(session_id, project_id=None, title="可恢复会话")
        archive.close()
        with patch.dict(os.environ, {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"}, clear=False), patch.object(
            mcp_server, "get_runtime_state", return_value=self._mounted_state()
        ), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ):
            response = self.client.post(
                "/chat",
                json={"query": "请保存这条会话", "session_id": session_id, "top_k": 3},
                headers=self._headers(),
            )
            # 模拟 sidecar 重启时进程内会话缓存被关闭；完整历史必须从
            # 工作区的加密文件归档重新打开，而非依赖内存状态。
            mcp_server._close_session_store()
            history = self.client.get(
                f"/sessions/{session_id}/history",
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(
            [item["content"] for item in history.json()["history"]],
            ["请保存这条会话", "ok"],
        )
        self.assertTrue(Path(self.workspace.conversation_store_path).exists())

    def test_chat_does_not_fail_when_an_old_unreadable_archive_exists(self):
        session_id = f"chat-corrupt-history-{uuid.uuid4().hex}"
        archive = ConversationStore(self.workspace.conversation_store_path, self.keys.metadata_key)
        archive.create_session(session_id, project_id="chat-project", title="当前会话")
        broken = archive.archive_root / "old-corrupt-session"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text(
            '{"schema_version":2,"session_id":"old-corrupt-session","project_id":"chat-project","title":"旧损坏会话"}',
            encoding="utf-8",
        )
        (broken / "messages.jsonl.enc").write_text(
            '{"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQ=="}\n',
            encoding="utf-8",
        )
        archive.close()

        fake_rag = _FakeRag()
        with patch.dict(
            os.environ,
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ):
            response = self.client.post(
                "/chat",
                headers=self._headers(),
                json={
                    "query": "当前会话仍应可用",
                    "session_id": session_id,
                    "history": [{"role": "user", "content": "上一条消息"}],
                    "active_context": {"project_id": "chat-project"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertIn(
            "old-corrupt-session",
            str(mcp_server._conversation_store(self.workspace).archive.archive_read_error),
        )


    def test_chat_builds_memory_capsule_and_guided_skill_execution(self):
        fake_rag = _FakeRag()
        candidates = [
            {
                "asset_type": "memory",
                "subtype": "user_preference",
                "title": "Review before saving",
                "content": "User wants human review before long-term memory writes.",
                "evidence": ["Please remember that I want human review before long-term memory writes."],
                "rationale": "Durable user preference.",
                "confidence": 0.91,
                "selected_by_default": True,
                "source_message_indexes": [0],
                "tags": ["preference"],
            },
            {
                "asset_type": "skill",
                "subtype": "sop",
                "title": "TDD workflow",
                "content": "1. Write a failing test\n2. Implement minimal code\n3. Run verification",
                "evidence": ["Use TDD workflow: write a failing test, implement minimal code, run verification."],
                "rationale": "Reusable engineering workflow.",
                "confidence": 0.88,
                "selected_by_default": True,
                "source_message_indexes": [1],
                "tags": ["tdd", "engineering"],
            },
        ]
        messages = [
            {"role": "user", "content": "Please remember that I want human review before long-term memory writes."},
            {"role": "assistant", "content": "Use TDD workflow: write a failing test, implement minimal code, run verification."},
        ]
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "ensure_runtime_loaded",
            side_effect=lambda: setattr(mcp_server, "rag", fake_rag),
        ), patch.object(mcp_server, "_generate_reflection_candidates", return_value=candidates):
            draft_response = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": messages},
                headers=self._headers(),
            )
            draft = draft_response.json()
            self.client.post(
                f"/reflection/reviews/{draft['review_id']}/decide",
                json={
                    "decisions": [
                        {"candidate_id": candidate["candidate_id"], "action": "accept"}
                        for candidate in draft["candidates"]
                    ]
                },
                headers=self._headers(),
            )
            response = self.client.post(
                "/chat",
                json={"query": "Please implement this feature with tests first", "top_k": 3},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Review before saving", fake_rag.learning_context["memory_capsule"]["text"])
        self.assertEqual(fake_rag.learning_context["skill_execution"]["skill_title"], "TDD workflow")
        self.assertTrue(fake_rag.learning_context["skill_execution"]["steps"])
        store = LearningStore(self.workspace, self.keys)
        self.addCleanup(store.close)
        self.assertEqual(len(store.list_skill_runs()), 1)

    def test_learning_context_excludes_other_project_memory_before_ranking(self):
        store = LearningStore(self.workspace, self.keys)
        self.addCleanup(store.close)
        for project_id, title in (("project-a", "项目 A 规则"), ("project-b", "项目 B 规则")):
            review = store.create_review("manual", [{
                "asset_type": "memory",
                "subtype": "project_fact",
                "title": title,
                "content": "项目使用本地模型进行检索。",
                "evidence": [title],
                "rationale": "项目范围事实。",
                "confidence": 0.9,
                "selected_by_default": True,
                "source_message_indexes": [0],
                "source_excerpt": title,
            }])
            store.decide_review(review["review_id"], [{
                "candidate_id": review["candidates"][0]["candidate_id"], "action": "accept",
            }], project_id=project_id)

        with patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            context = mcp_server._learning_context_for_query(
                "项目使用本地模型检索",
                project_id="project-a",
            )

        self.assertEqual([item["title"] for item in context["memories"]], ["项目 A 规则"])
        self.assertEqual(context["memories"][0]["source_trace"]["excerpt"], "项目 A 规则")

    def test_multimodal_decide_exposes_structured_clarification_contract(self):
        with patch.object(mcp_server, "require_software_unlock", return_value=None), patch.object(
            mcp_server, "current_audit_logger"
        ) as audit_logger:
            response = self.client.post(
                "/multimodal/decide",
                json={"speech_text": "比较上个月的实验结果", "events": []},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["action"], "clarify")
        self.assertTrue(payload["clarification"]["required"])
        self.assertIn("ambiguous_reference", payload["clarification"]["reason_codes"])
        self.assertTrue(payload["clarification"]["prompt"])
        audit_logger.return_value.append_event.assert_called_once()

    def test_learning_skill_match_endpoint_returns_relevant_skill(self):
        store = LearningStore(self.workspace, self.keys)
        self.addCleanup(store.close)
        review = store.create_review(
            "manual",
            [
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD workflow",
                    "content": "Use this when implementing a feature with tests first.",
                    "evidence": ["Use this when implementing a feature with tests first."],
                    "rationale": "Reusable engineering workflow.",
                    "confidence": 0.88,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["tdd"],
                }
            ],
        )
        store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
        )
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=self._mounted_state(),
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.post(
                "/learning/skills/match",
                json={"query": "Implement this feature with tests first", "top_k": 2},
                headers=self._headers(),
            )
            chinese_response = self.client.post(
                "/learning/skills/match",
                json={"query": "实现一个功能并先编写测试", "top_k": 2},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matches"][0]["title"], "TDD workflow")
        self.assertEqual(chinese_response.status_code, 200)
        self.assertEqual(chinese_response.json()["matches"][0]["title"], "TDD workflow")

    def test_project_skill_sync_endpoint_imports_workspace_skill_file(self):
        skill_file = Path(self.workspace.skills_path) / "research" / "SKILL.md"
        skill_file.parent.mkdir()
        skill_file.write_text("---\nname: 论文汇报\ndescription: 归纳论文并生成组会大纲\n---\n1. 提取结论\n2. 核对引用\n", encoding="utf-8")
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server, "effective_runtime_state", return_value=self._mounted_state()
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.post("/learning/skills/sync", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["imported"], 1)
        self.assertEqual(response.json()["skills"][0]["title"], "论文汇报")

    def test_project_skill_sync_hides_identical_directory_copies(self):
        content = "---\nname: test-driven-development\ndescription: Test first\n---\n1. Write a test\n"
        for directory in ("tdd_acceptance", "test-driven-development"):
            skill_file = Path(self.workspace.skills_path) / directory / "SKILL.md"
            skill_file.parent.mkdir()
            skill_file.write_text(content, encoding="utf-8")
        store = LearningStore(self.workspace, self.keys)
        try:
            for directory in ("tdd_acceptance", "test-driven-development"):
                store.sync_project_skill({
                    "title": "test-driven-development",
                    "content": content,
                    "source_path": f"{directory}/SKILL.md",
                    "source_digest": directory,
                })
        finally:
            store.close()

        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server, "effective_runtime_state", return_value=self._mounted_state()
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.post("/learning/skills/sync", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["duplicates_removed"], 1)
        self.assertEqual([skill["source_path"] for skill in response.json()["skills"]], ["test-driven-development/SKILL.md"])

    def test_project_skill_is_auto_discovered_when_the_skill_list_is_read(self):
        skill_file = Path(self.workspace.skills_path) / "research" / "SKILL.md"
        skill_file.parent.mkdir()
        skill_file.write_text("---\nname: 自动发现论文汇报\ndescription: 归纳论文并生成组会大纲\n---\n1. 提取结论\n2. 核对引用\n", encoding="utf-8")
        with patch.object(mcp_server, "get_runtime_state", return_value=self._mounted_state()), patch.object(
            mcp_server, "effective_runtime_state", return_value=self._mounted_state()
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.get("/learning/skills", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["imported"], 1)
        self.assertEqual(response.json()["skills"][0]["title"], "自动发现论文汇报")

    def test_retrieval_feedback_is_explicitly_saved_and_reversible(self):
        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server, "current_audit_logger"
        ) as audit_logger:
            response = self.client.post(
                "/diagnostics/retrieval/feedback",
                json={
                    "query": "RAG 的核心思想是什么？",
                    "corrected_query": "RAG 的核心思想是什么？ 检索增强生成 retrieval augmented generation",
                    "scope": "global",
                    "project_id": "stale-project-id",
                },
                headers=self._headers(),
            )

            self.assertEqual(response.status_code, 200)
            feedback = response.json()["feedback"]
            self.assertEqual(feedback["scope"], "global")
            self.assertEqual(
                mcp_server._retrieval_feedback_for_query("RAG 的核心思想是什么？"),
                [feedback],
            )

            missing_confirm = self.client.delete(
                f"/diagnostics/retrieval/feedback/{feedback['feedback_id']}",
                headers=self._headers(),
            )
            self.assertEqual(missing_confirm.status_code, 400)

            revoked = self.client.delete(
                f"/diagnostics/retrieval/feedback/{feedback['feedback_id']}?confirm=true",
                headers=self._headers(),
            )

        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(mcp_server._retrieval_feedback_for_query("RAG 的核心思想是什么？"), [])
        audit_events = [call.args[0] for call in audit_logger.return_value.append_event.call_args_list]
        self.assertIn("retrieval.feedback_saved", audit_events)
        self.assertIn("retrieval.feedback_revoked", audit_events)

    def test_explicitly_selected_skill_is_forced_into_chat_execution(self):
        class FakeStore:
            def __init__(self):
                self.recorded = []

            def list_skills(self):
                return [{
                    "skill_id": "skill_anything",
                    "title": "paper-review",
                    "category": "skill",
                    "content": "A reusable paper review workflow.",
                    "target": "conversation",
                    "compatibility_status": "verified",
                }]

            def list_memories(self, *args, **kwargs):
                return []

            def search(self, *args, **kwargs):
                return {
                    "memories": [],
                    "skills": [],
                    "memory_capsule": {"items": [], "text": ""},
                }

            def touch_memories(self, _memory_ids):
                return 0

            def record_skill_run(self, skill_id, **kwargs):
                self.recorded.append(skill_id)
                return {"run_id": "run_anything"}

            def close(self):
                return None

        fake_store = FakeStore()
        with patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "current_learning_store",
            return_value=fake_store,
        ):
            context = mcp_server._learning_context_for_query(
                "一个完全不相关的问题",
                selected_skill_id="skill_anything",
                project_id="chat-project",
            )

        self.assertEqual(context["skill_execution"]["skill_id"], "skill_anything")
        self.assertEqual(context["skill_execution"]["execution"]["mode"], "plan_only")
        self.assertIn("自动编译并验证的短契约", context["skill_execution"]["execution"]["result"])
        self.assertEqual(fake_store.recorded, ["skill_anything"])

    def test_presentation_style_skill_accepts_only_verified_presentation_skill(self):
        class FakeStore:
            def list_skills(self):
                return [{
                    "skill_id": "skill_anything",
                    "title": "frontend-slides",
                    "category": "skill",
                    "content": "A reusable slide workflow skill.",
                    "target": "presentation",
                    "compatibility_status": "verified",
                }]

            def close(self):
                return None

        with patch.object(mcp_server, "current_learning_store", return_value=FakeStore()):
            style = mcp_server._presentation_style_skill("skill_anything")

        self.assertEqual(style["skill_id"], "skill_anything")
        self.assertEqual(style["title"], "frontend-slides")

    def test_presentation_outline_prompt_applies_selected_skill_instructions(self):
        class FakeRag:
            def __init__(self):
                self.prompt = None

            def _generate_answer_text(self, prompt, **_kwargs):
                self.prompt = prompt
                return '{"title":"Skill deck","slides":[{"kind":"cover","title":"Skill deck","points":[],"sources":["A.pdf"]},{"kind":"content","title":"Applied","points":["来自证据"],"sources":["A.pdf"]},{"kind":"sources","title":"来源","points":[],"sources":["A.pdf"]}]}'

        fake_rag = FakeRag()
        skill = {
            "skill_id": "skill_pptx",
            "title": "可观察 PPT Skill",
            "content": "必须先建立逐页证据映射，再生成可编辑演示文稿。",
            "steps": [{"instruction": "每页保留来源文件名。"}],
        }
        inputs = {
            "source_filenames": ["A.pdf"],
            "title": "Skill deck",
            "audience": "research",
            "max_slides": 6,
            "style_skill": skill,
        }
        with patch.object(mcp_server, "rag", fake_rag), patch.object(
            mcp_server, "ensure_runtime_loaded", return_value=None
        ):
            plan = mcp_server._presentation_outline_with_local_model(
                [{"source_filename": "A.pdf", "section": "摘要", "page_number": 1, "content": "证据内容"}],
                inputs,
            )

        self.assertIn("必须先建立逐页证据映射", json.dumps(fake_rag.prompt, ensure_ascii=False))
        self.assertEqual(plan["skill_application"]["skill_id"], "skill_pptx")
        self.assertTrue(plan["skill_application"]["instructions_applied"])

    def test_learning_skill_delete_removes_project_skill(self):
        skill_dir = Path(self.workspace.skills_path) / "frontend-slides"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: frontend-slides\ndescription: Build browser presentation slides\n---\n",
            encoding="utf-8",
        )
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "frontend-slides",
                "content": "Build browser presentation slides",
                "applicability": "Build browser presentation slides",
                "source_path": "skills/frontend-slides/SKILL.md",
                "source_digest": "digest-1",
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.delete(f"/learning/skills/{skill['skill_id']}", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skills"], [])

    def test_learning_skill_script_execution_is_disabled(self):
        skill_dir = Path(self.workspace.skills_path) / "runtime-check"
        script = skill_dir / "scripts" / "check.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "import json\n"
            "payload = json.load(__import__('sys').stdin)\n"
            "print('skill-ok:' + payload['case'])\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: runtime-check\ndescription: Run a local Skill check\n---\n\n"
            "Use the declared check script and report its output.\n",
            encoding="utf-8",
        )
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "runtime-check",
                "content": "Use the declared check script and report its output.",
                "applicability": "Run a local Skill check",
                "source_path": "runtime-check/SKILL.md",
                "source_digest": "runtime-check-v1",
                "resource_manifest": [{
                    "path": "scripts/check.py",
                    "kind": "script",
                    "sha256": __import__("hashlib").sha256(script.read_bytes()).hexdigest(),
                }],
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        patches = [
            patch.object(mcp_server, "get_runtime_state", return_value=state),
            patch.object(mcp_server, "effective_runtime_state", return_value=state),
            patch.object(mcp_server, "current_workspace", return_value=self.workspace),
        ]
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        planned = self.client.post(
            f"/learning/skills/{skill['skill_id']}/runs",
            json={"query": "先规划一次 Skill 自检"},
            headers=self._headers(),
        )
        executed = self.client.post(
            f"/learning/skills/{skill['skill_id']}/runs",
            json={
                "query": "明确执行一次 Skill 自检",
                "mode": "execute",
                "script_path": "scripts/check.py",
                "inputs": {"case": "explicit"},
            },
            headers=self._headers(),
        )

        self.assertEqual(planned.status_code, 200)
        self.assertEqual(planned.json()["status"], "planned")
        self.assertEqual(planned.json()["plan"]["execution"]["mode"], "plan_only")
        self.assertEqual(executed.status_code, 410)
        self.assertIn("通用 Skill 脚本执行已停用", executed.json()["error"])

        refreshed = LearningStore(self.workspace, self.keys)
        try:
            runs = refreshed.list_skill_runs()
        finally:
            refreshed.close()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], planned.json()["run_id"])

    def test_learning_skill_script_failure_is_not_executed(self):
        skill_dir = Path(self.workspace.skills_path) / "runtime-failure"
        script = skill_dir / "scripts" / "fail.py"
        script.parent.mkdir(parents=True)
        script.write_text("raise SystemExit('intentional check failure')\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            "---\nname: runtime-failure\ndescription: Check failure reporting\n---\n",
            encoding="utf-8",
        )
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "runtime-failure",
                "content": "Check failure reporting",
                "source_path": "runtime-failure/SKILL.md",
                "source_digest": "runtime-failure-v1",
                "resource_manifest": [{
                    "path": "scripts/fail.py",
                    "kind": "script",
                    "sha256": __import__("hashlib").sha256(script.read_bytes()).hexdigest(),
                }],
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.post(
                f"/learning/skills/{skill['skill_id']}/runs",
                json={
                    "query": "明确执行失败脚本",
                    "mode": "execute",
                    "script_path": "scripts/fail.py",
                },
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 410)
        self.assertIn("通用 Skill 脚本执行已停用", response.json()["error"])

    def test_learning_skill_presentation_style_endpoint_creates_and_syncs_adapter(self):
        skill_dir = Path(self.workspace.skills_path) / "custom-slides"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: Custom Slides\ndescription: Build an evidence-bound deck\n---\n",
            encoding="utf-8",
        )
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "Custom Slides",
                "content": "Build an evidence-bound deck",
                "source_path": "custom-slides/SKILL.md",
                "source_digest": "custom-slides-before-style",
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            first = self.client.post(
                f"/learning/skills/{skill['skill_id']}/presentation-style",
                headers=self._headers(),
            )
            second = self.client.post(
                f"/learning/skills/{skill['skill_id']}/presentation-style",
                headers=self._headers(),
            )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertTrue(Path(first.json()["style_path"]).is_file())
        refreshed = next(item for item in second.json()["skills"] if item["skill_id"] == skill["skill_id"])
        self.assertEqual(refreshed["presentation_style_source"], "presentation_style.json")

    def test_learning_skill_delete_preserves_record_when_source_cleanup_fails(self):
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "证据不足时的安全回答模板",
                "content": "证据不足时明确说明无法确定",
                "applicability": "安全回答",
                "source_path": "证据不足时的安全回答模板/SKILL.md",
                "source_digest": "digest-unicode",
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "delete_project_skill_source_path",
            side_effect=OSError("file is in use"),
        ):
            response = self.client.delete(f"/learning/skills/{skill['skill_id']}", headers=self._headers())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "source_cleanup")
        self.assertIn("file is in use", response.json()["detail"])
        refreshed = LearningStore(self.workspace, self.keys)
        try:
            self.assertEqual(len(refreshed.list_skills()), 1)
        finally:
            refreshed.close()

    def test_learning_skill_delete_accepts_legacy_title_and_absolute_source_path(self):
        skill_dir = Path(self.workspace.skills_path) / "证据不足时的安全回答模板"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n", encoding="utf-8")
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "证据不足时的安全回答模板",
                "content": "证据不足时明确说明无法确定",
                "applicability": "安全回答",
                "source_path": str(skill_dir / "SKILL.md"),
                "source_digest": "digest-legacy-unicode",
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.delete(
                f"/learning/skills/{quote('证据不足时的安全回答模板', safe='')}",
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "deleted")
        self.assertEqual(response.json()["skill_name"], "证据不足时的安全回答模板")
        self.assertEqual(response.json()["skills"], [])
        self.assertFalse(skill_dir.exists())
        self.assertNotEqual(skill["skill_id"], "证据不足时的安全回答模板")

    def test_learning_skill_delete_still_succeeds_when_audit_sink_fails(self):
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "audit-failure-skill",
                "content": "Delete must not be reported as failed after the record is removed.",
                "applicability": "test",
                "source_path": "skills/audit-failure-skill/SKILL.md",
                "source_digest": "digest-audit-failure",
            })
            skill = store.list_skills()[0]
        finally:
            store.close()

        state = self._mounted_state()
        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit sink unavailable")

        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server,
            "effective_runtime_state",
            return_value=state,
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace), patch.object(
            mcp_server,
            "current_audit_logger",
            return_value=FailingAuditLogger(),
        ), patch.object(
            mcp_server,
            "delete_project_skill_source_path",
            return_value={"status": "deleted", "skill_name": "audit-failure-skill", "path": "skills/audit-failure-skill"},
        ):
            response = self.client.delete(f"/learning/skills/{skill['skill_id']}", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skills"], [])
        self.assertIn("审计记录写入失败", response.json()["warning"])

    def test_learning_skill_delete_removes_previous_skill_runs(self):
        skill_dir = Path(self.workspace.skills_path) / "used-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: used-skill\ndescription: A used skill\n---\n",
            encoding="utf-8",
        )
        store = LearningStore(self.workspace, self.keys)
        try:
            store.sync_project_skill({
                "title": "used-skill",
                "content": "A skill that has already run.",
                "source_path": "used-skill/SKILL.md",
                "source_digest": "digest-used",
            })
            skill = store.list_skills()[0]
            store.record_skill_run(
                skill["skill_id"],
                query="test",
                plan={"steps": []},
            )
        finally:
            store.close()

        state = self._mounted_state()
        with patch.object(mcp_server, "get_runtime_state", return_value=state), patch.object(
            mcp_server, "effective_runtime_state", return_value=state
        ), patch.object(mcp_server, "current_workspace", return_value=self.workspace):
            response = self.client.delete(f"/learning/skills/{skill['skill_id']}", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skills"], [])


class _FakeRag:
    def __init__(self):
        self.query = None
        self.learning_context = None
        self.interaction_context = None

    def generate_response_stream(
        self,
        query,
        top_k=3,
        trace_id=None,
        learning_context=None,
        interaction_context=None,
    ):
        self.query = query
        _ = top_k
        _ = trace_id
        self.learning_context = learning_context
        self.interaction_context = interaction_context
        yield "ok"


if __name__ == "__main__":
    unittest.main()
