import tempfile
import unittest
from unittest.mock import patch

from core.learning_store import LearningStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class LearningSearchCorruptionIsolationTests(unittest.TestCase):
    def _store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workspace = SoulDriveWorkspace.from_drive(temp_dir.name).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        keys = unlock_keystore(workspace, "correct horse battery staple")
        store = LearningStore(workspace, keys)
        self.addCleanup(store.close)
        return store

    def _accept_memory(self, store, title, content):
        review = store.create_review(
            "manual",
            [{
                "asset_type": "memory",
                "subtype": "project_fact",
                "title": title,
                "content": content,
                "confidence": 0.9,
                "selected_by_default": True,
                "source_message_indexes": [0],
            }],
        )
        return store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
        )["accepted_assets"][0]["memory_id"]

    def test_search_skips_corrupt_derived_chunk_and_keeps_healthy_memory(self):
        store = self._store()
        corrupt_memory_id = self._accept_memory(
            store,
            "损坏派生 chunk",
            "corrupt_memory_token 只用于隔离回归。",
        )
        healthy_memory_id = self._accept_memory(
            store,
            "健康派生 chunk",
            "healthy_memory_token 应继续可检索。",
        )
        chunk = store.conn.execute(
            "SELECT chunk_id FROM learning_chunks WHERE asset_id = ? AND asset_type = 'memory'",
            (corrupt_memory_id,),
        ).fetchone()
        store.conn.execute(
            "UPDATE learning_chunks SET payload_ciphertext = ? WHERE chunk_id = ?",
            ("tampered", chunk["chunk_id"]),
        )
        store.conn.commit()

        result = store.search("healthy_memory_token", top_k_memory=10)

        self.assertEqual(
            [item["asset_id"] for item in result["memories"]],
            [healthy_memory_id],
        )

    def test_memory_health_reports_corrupt_memory_chunk_without_exposing_content(self):
        store = self._store()
        memory_id = self._accept_memory(
            store,
            "不可读派生 chunk",
            "corrupt_chunk_health_token 不应进入诊断文本。",
        )
        chunk = store.conn.execute(
            "SELECT chunk_id FROM learning_chunks WHERE asset_id = ? AND asset_type = 'memory'",
            (memory_id,),
        ).fetchone()
        store.conn.execute(
            "UPDATE learning_chunks SET payload_ciphertext = ? WHERE chunk_id = ?",
            ("tampered", chunk["chunk_id"]),
        )
        store.conn.commit()

        health = store.memory_health()

        self.assertEqual(health["status"], "error")
        unreadable = [
            issue for issue in health["issues"]
            if issue["kind"] == "memory_chunk_payload_unreadable"
        ]
        self.assertEqual(len(unreadable), 1)
        self.assertEqual(unreadable[0]["memory_ids"], [memory_id])
        self.assertNotIn("corrupt_chunk_health_token", str(health))

    def test_history_fact_authentication_failure_is_not_silently_skipped(self):
        store = self._store()
        with patch("core.learning_store._now", return_value=100.0):
            memory_id = self._accept_memory(store, "历史事实", "historical_fact_token")
        with patch("core.learning_store._now", return_value=200.0):
            store.update_memory(
                memory_id,
                title="当前事实",
                content="current_fact_token",
                valid_from=200.0,
            )
        store.conn.execute(
            "UPDATE memory_versions SET payload_ciphertext = ? WHERE memory_id = ?",
            ("tampered", memory_id),
        )
        store.conn.commit()

        with self.assertRaisesRegex(ValueError, "learning payload authentication failed"):
            store.search("historical_fact_token", as_of=150.0)


if __name__ == "__main__":
    unittest.main()
