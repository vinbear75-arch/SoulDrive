import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.learning_store import LearningStore
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
        ):
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

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matches"][0]["title"], "TDD workflow")


class _FakeRag:
    def __init__(self):
        self.learning_context = None

    def generate_response_stream(self, query, top_k=3, trace_id=None, learning_context=None):
        _ = query
        _ = top_k
        _ = trace_id
        self.learning_context = learning_context
        yield "ok"


if __name__ == "__main__":
    unittest.main()
