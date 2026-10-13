import tempfile
import unittest

from core.learning_store import LearningStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class LearningOutboxCorruptionIsolationTests(unittest.TestCase):
    def _store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workspace = SoulDriveWorkspace.from_drive(temp_dir.name).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        keys = unlock_keystore(workspace, "correct horse battery staple")
        store = LearningStore(workspace, keys)
        self.addCleanup(store.close)
        return store

    def _accept_memory(self, store, title):
        review = store.create_review(
            "manual",
            [{
                "asset_type": "memory",
                "subtype": "user_preference",
                "title": title,
                "content": f"{title} 的同步事件用于损坏隔离回归。",
                "confidence": 0.9,
                "selected_by_default": True,
                "source_message_indexes": [0],
            }],
        )
        return store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
        )["accepted_assets"][0]["memory_id"]

    def _corrupted_pair(self, store):
        self._accept_memory(store, "较早的损坏同步")
        self._accept_memory(store, "较晚的健康同步")
        first, second = store.pending_memory_syncs()
        store.conn.execute(
            "UPDATE learning_sync_outbox SET payload_ciphertext = ? WHERE event_id = ?",
            ("tampered", first["event_id"]),
        )
        store.conn.commit()
        return first, second

    def test_pending_reads_skip_corrupt_event_and_fill_requested_limit(self):
        store = self._store()
        first, second = self._corrupted_pair(store)

        pending = store.pending_memory_syncs(limit=1)

        self.assertEqual([item["event_id"] for item in pending], [second["event_id"]])
        health = store.memory_health()
        self.assertEqual(health["status"], "error")
        self.assertTrue(
            any(
                issue["kind"] == "sync_payload_unreadable"
                and first["event_id"] in issue["event_ids"]
                for issue in health["issues"]
            )
        )

    def test_unscoped_claim_skips_corrupt_event_without_starving_healthy_event(self):
        store = self._store()
        first, second = self._corrupted_pair(store)

        claimed = store.claim_pending_memory_syncs("memory-sync-worker", limit=1)

        self.assertEqual([item["event_id"] for item in claimed], [second["event_id"]])
        corrupt_row = store.conn.execute(
            "SELECT attempts, claim_token FROM learning_sync_outbox WHERE event_id = ?",
            (first["event_id"],),
        ).fetchone()
        self.assertEqual(corrupt_row["attempts"], 0)
        self.assertIsNone(corrupt_row["claim_token"])


if __name__ == "__main__":
    unittest.main()
