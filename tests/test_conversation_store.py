import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.conversation_store import ConversationStore
from core.state_backends import ProductionSessionStore, RedisSessionStore


class ConversationStoreTests(unittest.TestCase):
    def test_messages_are_persisted_and_round_trip_encrypted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "conversations.sqlite")
            store = ConversationStore(path, b"k" * 32)
            store.append("s1", "user", "中文问题")
            store.append("s1", "assistant", "中文回答")
            store.close()

            self.assertNotIn("中文问题".encode("utf-8"), Path(path).read_bytes())
            reopened = ConversationStore(path, b"k" * 32)
            self.assertEqual([item["content"] for item in reopened.recent("s1")], ["中文问题", "中文回答"])
            reopened.close()

    def test_file_archive_is_authoritative_when_backends_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "1", "SOULDRIVE_POSTGRES_ENABLED": "1"},
            clear=False,
        ), patch("core.state_backends.RedisSessionStore", side_effect=RuntimeError("redis down")), patch(
            "core.state_backends.PostgresStateStore", side_effect=RuntimeError("postgres down")
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            first = store.append_message("s1", "user", "必须保留完整会话", request_id="retry-1")
            second = store.append_message("s1", "user", "必须保留完整会话", request_id="retry-1")

            self.assertEqual(first["message_id"], second["message_id"])
            self.assertEqual(len(archive.recent("s1", limit=20)), 1)
            self.assertGreaterEqual(len(archive.pending_outbox()), 2)
            self.assertTrue(store.health()["degraded"])
            store.close()

    def test_summary_and_history_lazy_load_keep_original_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                for index in range(12):
                    store.append_message("s1", "user", f"第{index}条消息", request_id=f"request-{index}")
                summary = store.compact_session("s1", threshold=4)

                self.assertIsNotNone(summary)
                self.assertEqual(summary["covered_sequence_start"], 1)
                self.assertEqual(summary["covered_sequence_end"], 4)
                self.assertEqual([item["sequence"] for item in store.load_before("s1", 9)["records"]], list(range(1, 9)))
                self.assertEqual(archive.latest_summary("s1")["summary"], summary["summary"])
            finally:
                store.close()

    def test_project_sessions_survive_reopen_and_do_not_mix_projects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "conversations.sqlite")
            store = ConversationStore(path, b"k" * 32)
            store.create_session("project-a-chat", project_id="project-a", title="项目 A 对话")
            store.create_session("project-b-chat", project_id="project-b", title="项目 B 对话")
            store.append("project-a-chat", "user", "只属于项目 A 的消息")
            self.assertEqual([item["session_id"] for item in store.list_sessions("project-a")], ["project-a-chat"])
            store.close()

            reopened = ConversationStore(path, b"k" * 32)
            self.assertEqual(reopened.session_metadata("project-a-chat")["project_id"], "project-a")
            self.assertEqual(reopened.recent("project-a-chat")[0]["content"], "只属于项目 A 的消息")
            with self.assertRaises(ValueError):
                reopened.create_session("project-a-chat", project_id="project-b", title="错误绑定尝试")
            reopened.close()

    def test_delete_session_removes_manifest_messages_and_local_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            archive.create_session("delete-me", project_id="project-a", title="待删除会话")
            archive.append("delete-me", "user", "这条消息应随会话删除")
            session_path = archive.archive_root / "delete-me"

            deleted = archive.delete_session("delete-me", project_id="project-a")

            self.assertEqual(deleted["title"], "待删除会话")
            self.assertIsNone(archive.session_metadata("delete-me"))
            self.assertEqual(archive.list_sessions("project-a"), [])
            self.assertFalse(session_path.exists())
            archive.close()

    def test_session_name_cannot_be_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            with self.assertRaisesRegex(ValueError, "title is required"):
                archive.create_session("blank-title", project_id="project-a", title="   ")
            archive.close()

    def test_unreadable_historical_archive_does_not_block_new_idempotent_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.create_session("active-session", project_id="project-a", title="当前会话")
                broken = archive.archive_root / "broken-session"
                broken.mkdir(parents=True)
                (broken / "manifest.json").write_text(
                    '{"schema_version":2,"session_id":"broken-session","project_id":"project-a","title":"损坏历史"}',
                    encoding="utf-8",
                )
                (broken / "messages.jsonl.enc").write_text(
                    '{"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQ=="}\n',
                    encoding="utf-8",
                )

                record = archive.append(
                    "active-session",
                    "user",
                    "新消息仍应落盘",
                    message_id="new-message-after-corruption",
                )

                self.assertEqual(record["message_id"], "new-message-after-corruption")
                self.assertIn("broken-session", archive.archive_read_error)
                self.assertEqual(archive.recent("active-session")[0]["content"], "新消息仍应落盘")
            finally:
                archive.close()

    def test_production_session_store_binds_create_and_delete_to_redis(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            store.redis_enabled = True
            store.redis = fake_redis
            try:
                created = store.create_session("redis-session", project_id="project-a", title="Redis 会话")
                deleted = store.delete_session("redis-session", project_id="project-a")
            finally:
                store.close()

            fake_redis.set_session_metadata.assert_called_once_with(created)
            fake_redis.delete_session.assert_called_once_with("redis-session")
            self.assertEqual(deleted["title"], "Redis 会话")

    def test_deleted_session_outbox_create_events_cannot_resurrect_hot_state(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            fake_postgres = Mock()
            store.redis = fake_redis
            store.postgres = fake_postgres
            try:
                store._dispatch_event(
                    {
                        "event_type": "redis.session_created",
                        "aggregate_id": "already-deleted",
                        "payload": {
                            "metadata": {
                                "session_id": "already-deleted",
                                "project_id": "project-a",
                                "title": "不应恢复",
                            }
                        },
                    }
                )
                store._dispatch_event(
                    {
                        "event_type": "postgres.session_created",
                        "aggregate_id": "already-deleted",
                        "payload": {
                            "session_id": "already-deleted",
                            "project_id": "project-a",
                            "title": "不应恢复",
                            "archive_path": "missing",
                            "created_at": 1.0,
                        },
                    }
                )
            finally:
                store.close()

            fake_redis.set_session_metadata.assert_not_called()
            fake_postgres.upsert_session.assert_not_called()

    def test_redis_8_vector_set_writes_and_filters_session_memory(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VADD":
                    self.write_args = args
                    return 1
                if command == "VSIM":
                    return [b"memory-a", b"1", b'{"memory_id":"memory-a","session_id":"s1"}']
                raise AssertionError(command)

            def expire(self, *_):
                return True

            def info(self, **_):
                return {"redis_version": "8.10.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def close(self):
                pass

        fake = FakeRedis()
        with patch("redis.Redis.from_url", return_value=fake):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertEqual(store.health()["vector_index_status"], "idle")
            self.assertTrue(store.put_vector(
                memory_id="memory-a", session_id="s1", memory_type="memory",
                vector=[1.0, 0.0], archive_path="D:/archive/memory-a.json.enc",
            ))
            self.assertEqual(fake.write_args[0], store.vector_set_key)
            self.assertEqual(store.search_vector([1.0, 0.0], session_id="s1")[0]["memory_id"], "memory-a")
            self.assertEqual(store.search_vector([1.0, 0.0], session_id="other"), [])
            self.assertEqual(store.health()["vector_index_status"], "ready")

    def test_redis_8_vector_set_dimension_is_restored_after_reopen(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    return [b"vector-dim", 2560, b"size", 1]
                if command == "COMMAND INFO":
                    return [b"VADD"]
                raise RuntimeError(command)

            def scan_iter(self, **_kwargs):
                return iter(())

            def llen(self, _key):
                return 0

            def info(self, **_kwargs):
                return {"redis_version": "8.10.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def close(self):
                pass

        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertEqual(store.health()["vector_index_status"], "ready")
            self.assertFalse(store.ensure_vector_index(3))
            self.assertIn("expected 2560, got 3", store.health()["vector_index_error"])


if __name__ == "__main__":
    unittest.main()
