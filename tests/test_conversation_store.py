import json
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core.conversation_compaction import build_recoverable_summary
from core.conversation_store import ConversationStore
from core.state_backends import PostgresStateStore, ProductionSessionStore, RedisSessionStore


class ConversationStoreTests(unittest.TestCase):
    def test_independent_store_handles_serialize_session_append(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "conversations.sqlite")
            first = ConversationStore(path, b"k" * 32)
            second = ConversationStore(path, b"k" * 32)
            try:
                first.create_session("parallel-session", title="并发会话")
                start = threading.Barrier(2)
                errors = []

                def worker(store, content):
                    try:
                        start.wait(timeout=5)
                        store.append("parallel-session", "user", content)
                    except BaseException as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                threads = [
                    threading.Thread(target=worker, args=(first, "第一条")),
                    threading.Thread(target=worker, args=(second, "第二条")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                records = first.recent("parallel-session", limit=10)
            finally:
                first.close()
                second.close()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual([record["sequence"] for record in records], [1, 2])
        self.assertEqual({record["content"] for record in records}, {"第一条", "第二条"})

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

    def test_request_id_message_identity_is_bound_to_session(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("session-a", project_id="project-a", title="会话 A")
                store.create_session("session-b", project_id="project-b", title="会话 B")
                first = store.append_message(
                    "session-a",
                    "user",
                    "只属于会话 A 的问题",
                    request_id="shared-request-id",
                )
                second = store.append_message(
                    "session-b",
                    "user",
                    "只属于会话 B 的问题",
                    request_id="shared-request-id",
                )

                self.assertNotEqual(first["message_id"], second["message_id"])
                self.assertEqual(archive.recent("session-a")[0]["content"], "只属于会话 A 的问题")
                self.assertEqual(archive.recent("session-b")[0]["content"], "只属于会话 B 的问题")
            finally:
                store.close()

    def test_append_message_rejects_a_project_override_on_a_bound_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("bound-session", project_id="project-a", title="项目 A 会话")
                with self.assertRaisesRegex(ValueError, "another project"):
                    store.append_message(
                        "bound-session",
                        "user",
                        "不能写入项目 B",
                        project_id="project-b",
                    )
                self.assertEqual(archive.recent("bound-session"), [])
            finally:
                store.close()

    def test_append_message_rejects_promoting_a_global_session_into_a_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("global-session", project_id=None, title="全局会话")
                with self.assertRaisesRegex(ValueError, "global session"):
                    store.append_message(
                        "global-session",
                        "user",
                        "不能临时升级范围",
                        project_id="project-a",
                    )
                self.assertEqual(archive.recent("global-session"), [])
            finally:
                store.close()

    def test_explicit_message_id_cannot_cross_session_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.create_session("session-a", project_id="project-a", title="会话 A")
                archive.create_session("session-b", project_id="project-b", title="会话 B")
                archive.append(
                    "session-a",
                    "user",
                    "原始消息",
                    message_id="shared-explicit-message",
                )

                with self.assertRaisesRegex(ValueError, "already bound to a different message"):
                    archive.append(
                        "session-a",
                        "user",
                        "改写后的消息",
                        message_id="shared-explicit-message",
                    )

                with self.assertRaisesRegex(ValueError, "belongs to another session"):
                    archive.append(
                        "session-b",
                        "user",
                        "不能跨会话复用",
                        message_id="shared-explicit-message",
                    )
            finally:
                archive.close()

    def test_request_retry_reuses_legacy_message_identity_inside_session(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("legacy-session", project_id="project-a", title="旧会话")
                legacy = archive.append(
                    "legacy-session",
                    "user",
                    "旧版本请求",
                    request_id="legacy-request",
                    message_id="legacy-message-id",
                )
                replay = store.append_message(
                    "legacy-session",
                    "user",
                    "旧版本请求",
                    request_id="legacy-request",
                )

                self.assertEqual(replay["message_id"], legacy["message_id"])
                self.assertEqual(len(archive.recent("legacy-session")), 1)
            finally:
                store.close()

    def test_pending_outbox_returns_oldest_events_with_stable_tie_breaker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                for event_id, created_at in (
                    ("evt-new", 20.0),
                    ("evt-old", 10.0),
                    ("evt-tie-b", 30.0),
                    ("evt-tie-a", 30.0),
                ):
                    archive.append_outbox({"event_id": event_id, "created_at": created_at})

                pending = archive.pending_outbox(limit=4)
            finally:
                archive.close()

        self.assertEqual(
            [item["event_id"] for item in pending],
            ["evt-old", "evt-new", "evt-tie-a", "evt-tie-b"],
        )

    def test_outbox_retry_preserves_event_payload_and_rejects_identity_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                event = {
                    "event_id": "evt-retry-payload",
                    "event_type": "redis.session_message",
                    "aggregate_id": "message-1",
                    "payload": {"record": {"message_id": "message-1", "content": "原始正文"}},
                }
                archive.append_outbox(event)
                archive.mark_outbox(event["event_id"], "pending", "redis temporarily unavailable")

                pending = archive.pending_outbox()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["event_type"], event["event_type"])
                self.assertEqual(pending[0]["aggregate_id"], event["aggregate_id"])
                self.assertEqual(pending[0]["payload"], event["payload"])
                self.assertEqual(pending[0]["error"], "redis temporarily unavailable")

                with self.assertRaisesRegex(ValueError, "outbox event conflict"):
                    archive.append_outbox({
                        **event,
                        "payload": {"record": {"message_id": "message-1", "content": "篡改正文"}},
                    })
            finally:
                archive.close()

    def test_outbox_legacy_partial_status_snapshot_recovers_original_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                event = {
                    "event_id": "evt-legacy-partial",
                    "event_type": "redis.session_message",
                    "aggregate_id": "message-legacy",
                    "payload": {"record": {"message_id": "message-legacy", "content": "原始正文"}},
                }
                archive.append_outbox(event)
                # Simulate the pre-fix status-only snapshot written by an old
                # process after a failed dispatch.
                archive._append_encrypted_line(
                    archive.outbox_path,
                    {
                        "schema_version": 1,
                        "event_id": event["event_id"],
                        "status": "pending",
                        "error": "legacy retry",
                    },
                    b"SoulDrive conversation outbox v1",
                )

                archive.mark_outbox(event["event_id"], "pending", "recovered retry")
                [pending] = archive.pending_outbox()
                self.assertEqual(pending["event_type"], event["event_type"])
                self.assertEqual(pending["payload"], event["payload"])
                self.assertEqual(pending["error"], "recovered retry")
            finally:
                archive.close()

    def test_project_memory_delete_removes_archive_and_fences_pending_upsert(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                memory = {
                    "memory_id": "project-memory",
                    "scope": "project",
                    "project_id": "project-a",
                    "record_version": 1,
                    "status": "active",
                    "title": "项目记忆",
                    "content": "只属于项目 A。",
                }
                archive.archive_memory(memory)
                archive.append_outbox(
                    {
                        "event_id": "pending-memory-upsert",
                        "event_type": "postgres.memory",
                        "aggregate_id": "project-memory",
                        "payload": {"memory": memory},
                    }
                )

                deleted = store.delete_memory("project-memory", project_id="project-a")

                self.assertTrue(deleted["archive_deleted"])
                self.assertIsNone(archive.read_memory("project-memory"))
                self.assertEqual(archive.pending_outbox(), [])
            finally:
                store.close()

    def test_memory_archive_rejects_different_payload_for_same_record_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.archive_memory({
                    "memory_id": "same-version-memory",
                    "record_version": 3,
                    "status": "active",
                    "scope": "project",
                    "project_id": "project-a",
                    "title": "原始事实",
                    "content": "原始内容",
                })
                with self.assertRaisesRegex(ValueError, "memory version conflict"):
                    archive.archive_memory({
                        "memory_id": "same-version-memory",
                        "record_version": 3,
                        "status": "active",
                        "scope": "project",
                        "project_id": "project-a",
                        "title": "篡改事实",
                        "content": "不同内容",
                    })
                self.assertEqual(
                    archive.read_memory("same-version-memory")["content"],
                    "原始内容",
                )
            finally:
                archive.close()

    def test_independent_store_handles_serialize_memory_archive_conflicts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "conversations.sqlite")
            first = ConversationStore(path, b"k" * 32)
            second = ConversationStore(path, b"k" * 32)
            try:
                first.archive_memory({
                    "memory_id": "parallel-memory",
                    "record_version": 1,
                    "status": "active",
                    "scope": "global",
                    "content": "旧内容",
                })
                start = threading.Barrier(2)
                successes = []
                conflicts = []

                def worker(store, content):
                    try:
                        start.wait(timeout=5)
                        successes.append(
                            store.archive_memory({
                                "memory_id": "parallel-memory",
                                "record_version": 2,
                                "status": "active",
                                "scope": "global",
                                "content": content,
                            })
                        )
                    except ValueError as exc:
                        conflicts.append(str(exc))

                threads = [
                    threading.Thread(target=worker, args=(first, "新内容 A")),
                    threading.Thread(target=worker, args=(second, "新内容 B")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                current = first.read_memory("parallel-memory")
            finally:
                first.close()
                second.close()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(conflicts, ["memory version conflict"])
        self.assertEqual(current["record_version"], 2)
        self.assertIn(current["content"], {"新内容 A", "新内容 B"})

    def test_postgres_pending_outbox_uses_same_stable_order(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                self.calls.append((statement, params))

            def fetchall(self):
                rows = [
                    {"event_id": "evt-tie-b", "created_at": 10.0},
                    {"event_id": "evt-tie-a", "created_at": 10.0},
                ]
                if "ORDER BY created_at ASC, event_id ASC" in self.calls[-1][0]:
                    return sorted(rows, key=lambda item: (item["created_at"], item["event_id"]))
                return rows

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)

        pending = store.pending_outbox(limit=2)

        self.assertEqual([item["event_id"] for item in pending], ["evt-tie-a", "evt-tie-b"])
        self.assertEqual(cursor.calls[0][1], (2,))

    def test_postgres_outbox_replay_claims_once_across_workers(self):
        class FakeArchive:
            def pending_outbox(self, limit=100):
                return []

            def mark_outbox(self, event_id, status, error=None):
                return None

        class FakePostgresOutbox:
            def __init__(self):
                self._lock = threading.Lock()
                self._event = {
                    "event_id": "postgres-replay-once",
                    "event_type": "test.replay",
                    "aggregate_id": "aggregate",
                    "payload_json": {},
                }
                self._claimed = False
                self._pending_barrier = threading.Barrier(2)
                self.claim_tokens = []
                self.completed = []

            def pending_outbox(self, limit=100):
                self._pending_barrier.wait(timeout=5)
                return [dict(self._event)]

            def claim_pending_outbox(self, limit=100, *, owner_id=None, lease_seconds=300.0):
                with self._lock:
                    if self._claimed:
                        return []
                    self._claimed = True
                    token = f"{owner_id}:claim"
                    self.claim_tokens.append(token)
                    return [{**self._event, "claim_token": token}]

            def mark_outbox(self, event_id, status, error="", *, claim_token=None):
                self.completed.append((event_id, status, claim_token))

        archive = FakeArchive()
        postgres = FakePostgresOutbox()
        stores = []
        dispatch_count = 0
        dispatch_lock = threading.Lock()

        def dispatch(_event):
            nonlocal dispatch_count
            with dispatch_lock:
                dispatch_count += 1

        for worker_id in ("worker-a", "worker-b"):
            store = ProductionSessionStore.__new__(ProductionSessionStore)
            store.archive = archive
            store.postgres = postgres
            store._outbox_replay_lock = threading.Lock()
            store._connect_backends = lambda: None
            store._dispatch_event = dispatch
            stores.append(store)

        results = []
        threads = [
            threading.Thread(target=lambda store=store: results.append(store._replay_outbox_locked()))
            for store in stores
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(dispatch_count, 1)
        self.assertEqual(len(postgres.claim_tokens), 1)
        self.assertEqual(sum(item["completed"] for item in results), 1)
        self.assertEqual(postgres.completed[0][2], postgres.claim_tokens[0])

    def test_postgres_outbox_lost_claim_is_not_reported_as_completed(self):
        class FakeArchive:
            def pending_outbox(self, limit=100):
                return []

        class LostClaimPostgres:
            def claim_pending_outbox(self, limit=100, *, owner_id=None, lease_seconds=300.0):
                return [
                    {
                        "event_id": "lost-postgres-claim",
                        "event_type": "test.replay",
                        "aggregate_id": "aggregate",
                        "payload_json": {},
                        "claim_token": "worker-a:claim",
                    }
                ]

            def mark_outbox(self, event_id, status, error="", *, claim_token=None):
                return False

        store = ProductionSessionStore.__new__(ProductionSessionStore)
        store.archive = FakeArchive()
        store.postgres = LostClaimPostgres()
        store._outbox_replay_lock = threading.Lock()
        store._connect_backends = lambda: None
        store._dispatch_event = lambda _event: None

        result = store._replay_outbox_locked()

        self.assertEqual(result, {"completed": 0, "failed": 1})

    def test_postgres_outbox_rejects_conflicting_event_payload(self):
        class FakeCursor:
            def __init__(self):
                self.current = None
                self.rowcount = 1
                self.last_statement = ""

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                self.last_statement = statement
                if statement.lstrip().startswith("SELECT event_type"):
                    self.rowcount = 1 if self.current is not None else 0
                elif "INSERT INTO souldrive_outbox_events" in statement:
                    self.current = {
                        "event_type": params[1],
                        "aggregate_id": params[2],
                        "payload_hash": params[4],
                        "payload_json": json.loads(params[3]),
                    }
                    self.rowcount = 1

            def fetchone(self):
                return self.current

        class FakeConnection:
            def __init__(self, cursor):
                self.cursor_value = cursor

            def cursor(self):
                return self.cursor_value

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)
        event = {
            "event_id": "evt-postgres-identity",
            "event_type": "redis.session_message",
            "aggregate_id": "message-1",
            "payload": {"record": {"message_id": "message-1", "content": "原始正文"}},
        }

        store.enqueue(event)
        store.enqueue(event)
        with self.assertRaisesRegex(ValueError, "outbox event conflict"):
            store.enqueue({
                **event,
                "payload": {"record": {"message_id": "message-1", "content": "篡改正文"}},
            })

    def test_postgres_session_projection_persists_workflow_identity(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                self.calls.append((statement, params))

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)

        store.upsert_session(
            "workflow-session",
            project_id="project-a",
            archive_path="D:/archive/workflow-session/messages.jsonl.enc",
            latest_sequence=1,
            created_at=1.0,
            title="论文窗口",
            workflow_id="paper_review",
        )

        self.assertTrue(
            any(
                "workflow_id" in statement and "paper_review" in params
                for statement, params in cursor.calls
            )
        )

    def test_postgres_session_projection_rejects_unknown_workflow_identity(self):
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        cursor = Mock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        store.connection = connection
        with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
            store.upsert_session(
                "unknown-workflow-session",
                project_id="project-a",
                archive_path="D:/archive/unknown-workflow/messages.jsonl.enc",
                latest_sequence=0,
                created_at=1.0,
                title="未知窗口",
                workflow_id="unknown_window",
            )

    def test_postgres_memory_projection_preserves_workflow_identity(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                self.calls.append((statement, params))

            def fetchone(self):
                return None

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)

        store.upsert_memory(
            {
                "memory_id": "workflow-memory",
                "record_version": 1,
                "status": "active",
                "content": "论文事实",
                "workflow_id": "paper_review",
            },
            "D:/archive/workflow-memory.json.enc",
        )

        metadata = json.loads(cursor.calls[1][1][7])
        self.assertEqual(metadata["workflow_id"], "paper_review")

    def test_postgres_memory_projection_rejects_different_payload_for_same_version(self):
        class FakeCursor:
            def __init__(self):
                self.current = {
                    "project_id": "project-a",
                    "session_id": None,
                    "status": "active",
                    "content_hash": "hash-of-another-memory",
                    "metadata_json": {
                        "record_version": 3,
                        "title": "原始事实",
                    },
                }

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, *_args, **_kwargs):
                return None

            def fetchone(self):
                return self.current

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(FakeCursor())
        with self.assertRaisesRegex(ValueError, "memory version conflict"):
            store.upsert_memory(
                {
                    "memory_id": "same-version-memory",
                    "record_version": 3,
                    "status": "active",
                    "title": "不同事实",
                    "content": "不同内容",
                },
                "D:/archive/same-version-memory.json.enc",
                project_id="project-a",
            )

    def test_postgres_memory_projection_rejects_unknown_workflow_identity(self):
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        store.connection = connection
        with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
            store.upsert_memory(
                {
                    "memory_id": "unknown-workflow-memory",
                    "record_version": 1,
                    "status": "active",
                    "content": "未知窗口事实",
                    "workflow_id": "unknown_window",
                },
                "D:/archive/unknown-workflow-memory.json.enc",
            )

    def test_request_id_lookup_finds_matching_user_and_completed_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("retry-session", project_id=None, title="重试会话")
                store.append_message(
                    "retry-session",
                    "user",
                    "原问题",
                    request_id="retry-key",
                )
                store.append_message(
                    "retry-session",
                    "assistant",
                    "缓存回答 [E1]",
                    metadata={"status": "completed"},
                    request_id="retry-key",
                )

                user = store.find_message_by_request_id(
                    "retry-session",
                    "retry-key",
                    role="user",
                )
                answer = store.find_message_by_request_id(
                    "retry-session",
                    "retry-key",
                    role="assistant",
                    status="completed",
                )

                self.assertEqual(user["content"], "原问题")
                self.assertEqual(answer["content"], "缓存回答 [E1]")
            finally:
                store.close()

    def test_request_receipt_serializes_in_flight_work_and_replays_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                first = archive.claim_request(
                    "receipt-session",
                    "receipt-key",
                    "原问题",
                    owner_id="worker-a",
                )
                second = archive.claim_request(
                    "receipt-session",
                    "receipt-key",
                    "原问题",
                    owner_id="worker-b",
                )

                self.assertEqual(first["status"], "claimed")
                self.assertTrue(first["lease_token"])
                self.assertTrue(
                    archive.renew_request(
                        "receipt-session",
                        "receipt-key",
                        owner_id="worker-a",
                        lease_token=first["lease_token"],
                    )
                )
                self.assertEqual(second["status"], "in_flight")
                self.assertGreaterEqual(second["retry_after_ms"], 0)

                self.assertTrue(
                    archive.complete_request(
                        "receipt-session",
                        "receipt-key",
                        owner_id="worker-a",
                        lease_token=first["lease_token"],
                        response_message_id="msg-response",
                    )
                )
                replay = archive.claim_request(
                    "receipt-session",
                    "receipt-key",
                    "原问题",
                    owner_id="worker-c",
                )
                self.assertEqual(replay["status"], "completed")
                self.assertEqual(replay["response_message_id"], "msg-response")

                conflict = archive.claim_request(
                    "receipt-session",
                    "receipt-key",
                    "换了一个问题",
                    owner_id="worker-c",
                )
                self.assertEqual(conflict["status"], "conflict")
            finally:
                archive.close()

    def test_request_receipt_binds_same_query_to_full_input_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                first = archive.claim_request(
                    "receipt-session",
                    "context-key",
                    "相同问题",
                    owner_id="worker-a",
                    input_digest="context-a",
                )
                self.assertEqual(first["status"], "claimed")

                replay = archive.claim_request(
                    "receipt-session",
                    "context-key",
                    "相同问题",
                    owner_id="worker-b",
                    input_digest="context-a",
                )
                self.assertEqual(replay["status"], "in_flight")

                conflict = archive.claim_request(
                    "receipt-session",
                    "context-key",
                    "相同问题",
                    owner_id="worker-b",
                    input_digest="context-b",
                )
                self.assertEqual(conflict["status"], "conflict")
            finally:
                archive.close()

    def test_expired_request_receipt_can_be_taken_over_and_failed_receipt_can_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                first = archive.claim_request(
                    "receipt-session",
                    "expired-key",
                    "原问题",
                    owner_id="worker-a",
                    lease_seconds=0.001,
                )
                time.sleep(0.02)
                takeover = archive.claim_request(
                    "receipt-session",
                    "expired-key",
                    "原问题",
                    owner_id="worker-b",
                )
                self.assertEqual(takeover["status"], "claimed")
                self.assertNotEqual(takeover["lease_token"], first["lease_token"])

                self.assertTrue(
                    archive.fail_request(
                        "receipt-session",
                        "expired-key",
                        owner_id="worker-b",
                        lease_token=takeover["lease_token"],
                        error="temporary failure",
                    )
                )
                retry = archive.claim_request(
                    "receipt-session",
                    "expired-key",
                    "原问题",
                    owner_id="worker-c",
                )
                self.assertEqual(retry["status"], "claimed")
            finally:
                archive.close()

    def test_request_receipt_has_one_winner_across_two_store_handles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "conversations.sqlite")
            first_store = ConversationStore(path, b"k" * 32)
            second_store = ConversationStore(path, b"k" * 32)
            results = []
            barrier = threading.Barrier(2)

            def claim(store, owner):
                barrier.wait()
                results.append(
                    store.claim_request(
                        "receipt-session",
                        "concurrent-key",
                        "并发问题",
                        owner_id=owner,
                    )
                )

            first_thread = threading.Thread(target=claim, args=(first_store, "worker-a"))
            second_thread = threading.Thread(target=claim, args=(second_store, "worker-b"))
            first_thread.start()
            second_thread.start()
            first_thread.join(5)
            second_thread.join(5)
            try:
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())
                self.assertEqual(
                    sorted(result["status"] for result in results),
                    ["claimed", "in_flight"],
                )
            finally:
                first_store.close()
                second_store.close()

    def test_session_retry_cannot_silently_change_title_or_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                first = archive.create_session(
                    "role-session",
                    project_id="project-a",
                    title="论文精读",
                    workflow_id="paper_review",
                )
                repeated = archive.create_session(
                    "role-session",
                    project_id="project-a",
                    title="论文精读",
                    workflow_id="paper_review",
                )
                self.assertEqual(repeated["session_id"], first["session_id"])
                with self.assertRaisesRegex(ValueError, "different title or workflow_id"):
                    archive.create_session(
                        "role-session",
                        project_id="project-a",
                        title="实验对比",
                        workflow_id="experiment_compare",
                    )
            finally:
                archive.close()

    def test_session_creation_rejects_unknown_workflow_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
                    archive.create_session(
                        "unknown-workflow-session",
                        project_id="project-a",
                        title="未知窗口",
                        workflow_id="unknown_window",
                    )
            finally:
                archive.close()

    def test_legacy_unknown_workflow_manifest_is_not_loaded_as_a_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                manifest_path = archive._manifest_path("legacy-unknown-workflow")
                manifest_path.write_text(
                    json.dumps({
                        "session_id": "legacy-unknown-workflow",
                        "project_id": "project-a",
                        "title": "遗留未知窗口",
                        "workflow_id": "unknown_window",
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
                    archive.session_metadata("legacy-unknown-workflow")
                self.assertEqual(archive.list_sessions("project-a"), [])
            finally:
                archive.close()

    def test_session_manifest_identity_must_match_its_archive_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                manifest_path = archive._manifest_path("session-a")
                manifest_path.write_text(
                    json.dumps({
                        "session_id": "session-b",
                        "project_id": "project-a",
                        "title": "错绑会话",
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "session manifest identity"):
                    archive.session_metadata("session-a")
                self.assertEqual(archive.list_sessions("project-a"), [])
            finally:
                archive.close()

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
                self.assertEqual(summary["input_message_count"], 4)
                self.assertEqual(summary["excerpt_status"], "known")
                self.assertTrue(summary["excerpt_message_ids"])
                self.assertEqual([item["sequence"] for item in store.load_before("s1", 9)["records"]], list(range(1, 9)))
                self.assertEqual(archive.latest_summary("s1")["summary"], summary["summary"])
            finally:
                store.close()

    def test_redis_summary_projection_does_not_regress_on_late_outbox_event(self):
        class FakeRedisClient:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def setex(self, key, _ttl, value):
                self.values[key] = value

            def eval(self, _script, numkeys, *args):
                summary_key, identity_key, version_key = args[:numkeys]
                incoming_version = int(args[numkeys])
                incoming_digest = args[numkeys + 1]
                sealed = args[numkeys + 2]
                expected_version = int(args[numkeys + 4])
                expected_digest = args[numkeys + 5]
                expected_value = args[numkeys + 6]
                current_value = self.values.get(summary_key)
                if current_value is not None:
                    if current_value != expected_value:
                        return -2
                    current_version = int(self.values.get(version_key) or expected_version)
                    if current_version > incoming_version:
                        return -3
                    if current_version == incoming_version:
                        return 0 if expected_digest == incoming_digest else -1
                self.values[summary_key] = sealed
                self.values[identity_key] = incoming_digest
                self.values[version_key] = str(incoming_version)
                return 2 if current_value is not None else 1

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            archive.create_session("summary-session", project_id=None, title="摘要会话")
            redis_projection = object.__new__(RedisSessionStore)
            redis_projection.client = FakeRedisClient()
            redis_projection.key = b"k" * 32
            redis_projection.prefix = "test"
            redis_projection.ttl_seconds = 60
            redis_projection._summary_lock = threading.RLock()
            store.redis = redis_projection
            try:
                store._dispatch_event({
                    "event_type": "redis.session_summary",
                    "aggregate_id": "summary-session",
                    "payload": {
                        "summary": {"session_id": "summary-session", "version": 2, "summary": "新摘要"},
                    },
                })
                store._dispatch_event({
                    "event_type": "redis.session_summary",
                    "aggregate_id": "summary-session",
                    "payload": {
                        "summary": {"session_id": "summary-session", "version": 1, "summary": "旧摘要"},
                    },
                })
            finally:
                store.close()

            self.assertEqual(redis_projection.get_summary("summary-session")["version"], 2)
            self.assertEqual(redis_projection.get_summary("summary-session")["summary"], "新摘要")

    def test_outbox_replay_is_serialized_per_session_store(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            entered = threading.Event()
            release = threading.Event()
            second_dispatch = threading.Event()
            dispatch_count = 0
            dispatch_lock = threading.Lock()

            archive.append_outbox(
                {
                    "event_id": "replay-once",
                    "event_type": "test.replay",
                    "aggregate_id": "aggregate",
                    "payload": {},
                }
            )

            def dispatch(_event):
                nonlocal dispatch_count
                with dispatch_lock:
                    dispatch_count += 1
                    if dispatch_count >= 2:
                        second_dispatch.set()
                entered.set()
                release.wait(timeout=2)

            store._dispatch_event = dispatch
            results = []
            first_thread = threading.Thread(target=lambda: results.append(store.replay_outbox()))
            second_thread = threading.Thread(target=lambda: results.append(store.replay_outbox()))
            try:
                first_thread.start()
                self.assertTrue(entered.wait(timeout=1))
                second_thread.start()
                self.assertFalse(second_dispatch.wait(timeout=0.15))
                release.set()
                first_thread.join(timeout=2)
                second_thread.join(timeout=2)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())
                self.assertEqual(dispatch_count, 1)
                self.assertEqual(archive.pending_outbox(), [])
                self.assertEqual(sum(item["completed"] for item in results), 1)
            finally:
                release.set()
                first_thread.join(timeout=2)
                second_thread.join(timeout=2)
                store.close()

    def test_outbox_replay_claims_once_across_independent_session_stores(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            database = str(Path(temp_dir) / "conversations.sqlite")
            first_archive = ConversationStore(database, b"k" * 32)
            second_archive = ConversationStore(database, b"k" * 32)
            first_store = ProductionSessionStore.__new__(ProductionSessionStore)
            second_store = ProductionSessionStore.__new__(ProductionSessionStore)
            for store, archive in (
                (first_store, first_archive),
                (second_store, second_archive),
            ):
                store.archive = archive
                store.redis = None
                store.postgres = None
                store._outbox_replay_lock = threading.Lock()
                store._connect_backends = lambda: None

            first_archive.append_outbox(
                {
                    "event_id": "cross-store-replay-once",
                    "event_type": "test.replay",
                    "aggregate_id": "aggregate",
                    "payload": {},
                }
            )
            dispatch_started = threading.Event()
            dispatch_count = 0
            dispatch_lock = threading.Lock()

            def dispatch(_event):
                nonlocal dispatch_count
                with dispatch_lock:
                    dispatch_count += 1
                dispatch_started.set()
                time.sleep(0.25)

            first_store._dispatch_event = dispatch
            second_store._dispatch_event = dispatch
            results = []
            threads = [
                threading.Thread(
                    target=lambda store=store: results.append(store._replay_outbox_locked())
                )
                for store in (first_store, second_store)
            ]
            try:
                threads[0].start()
                self.assertTrue(dispatch_started.wait(timeout=1))
                threads[1].start()
                for thread in threads:
                    thread.join(timeout=3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(dispatch_count, 1)
                self.assertEqual(sum(item["completed"] for item in results), 1)
                self.assertEqual(first_archive.pending_outbox(), [])
            finally:
                for thread in threads:
                    thread.join(timeout=1)
                first_store.close()
                second_store.close()

    def test_local_outbox_expired_claim_is_reclaimed_and_old_token_is_fenced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            archive.append_outbox(
                {
                    "event_id": "lease-recovery",
                    "event_type": "test.replay",
                    "aggregate_id": "aggregate",
                    "payload": {},
                }
            )
            first = archive.claim_outbox("lease-recovery", owner_id="worker-a", lease_seconds=30)
            self.assertIsNotNone(first)
            self.assertEqual(archive.pending_outbox(), [])

            archive.connection.execute(
                "UPDATE outbox_delivery_claims SET claim_until = 0 WHERE event_id = ?",
                ("lease-recovery",),
            )
            archive.connection.commit()
            second = archive.claim_outbox("lease-recovery", owner_id="worker-b", lease_seconds=30)

            self.assertIsNotNone(second)
            self.assertNotEqual(first["claim_token"], second["claim_token"])
            self.assertFalse(
                archive.mark_outbox(
                    "lease-recovery",
                    "completed",
                    claim_token=first["claim_token"],
                )
            )
            self.assertTrue(
                archive.mark_outbox(
                    "lease-recovery",
                    "completed",
                    claim_token=second["claim_token"],
                )
            )
            self.assertEqual(archive.pending_outbox(), [])
            archive.close()

    def test_summary_distinguishes_input_window_from_newer_budgeted_excerpt(self):
        records = [
            {
                "session_id": "s1",
                "sequence": index,
                "message_id": f"m{index}",
                "role": "user",
                "content": f"历史消息 {index} " + ("x" * 120),
            }
            for index in range(1, 13)
        ]

        summary = build_recoverable_summary(records, max_chars=180)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["input_sequence_start"], 1)
        self.assertEqual(summary["input_sequence_end"], 4)
        self.assertEqual(summary["input_message_count"], 4)
        self.assertTrue(summary["excerpt_truncated"])
        self.assertEqual(summary["excerpt_message_ids"], ["m4"])
        self.assertIn("历史消息 4", summary["summary"])
        self.assertNotIn("历史消息 1", summary["summary"])

    def test_compaction_preserves_long_empty_and_oversized_message_boundaries(self):
        records = [
            {
                "session_id": "s1",
                "sequence": 1,
                "message_id": "m1",
                "role": "user",
                "content": "   ",
            },
            {
                "session_id": "s1",
                "sequence": 2,
                "message_id": "m2",
                "role": "user",
                "content": "超预算单条 " + ("x" * 5000),
            },
            *[
                {
                    "session_id": "s1",
                    "sequence": index,
                    "message_id": f"m{index}",
                    "role": "assistant",
                    "content": f"补充消息 {index}",
                }
                for index in range(3, 11)
            ],
        ]

        summary = build_recoverable_summary(records, max_chars=180)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["input_message_count"], 2)
        self.assertEqual(summary["source_message_ids"], ["m1", "m2"])
        self.assertTrue(summary["excerpt_truncated"])
        self.assertEqual(summary["excerpt_message_ids"], [])
        self.assertIn("该摘要只压缩上下文", summary["summary"])

    def test_compaction_over_200_messages_keeps_sources_and_summary_versions_after_restart(self):
        self._check_compaction_sources_and_restart(205)

    def test_compaction_64_messages_keeps_sources_and_summary_versions_after_restart(self):
        self._check_compaction_sources_and_restart(64)

    def _check_compaction_sources_and_restart(self, message_count):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            path = str(Path(temp_dir) / "conversations.sqlite")
            archive = ConversationStore(path, b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("long-session", project_id="project-a", title="长会话")
                for index in range(1, message_count + 1):
                    store.append_message(
                        "long-session",
                        "user",
                        f"原文消息 {index}",
                        metadata={"source": "fixture", "index": index},
                        request_id=f"long-request-{index}",
                        project_id="project-a",
                    )

                first = store.compact_session(
                    "long-session",
                    project_id="project-a",
                    threshold=4,
                )
                second = store.compact_session(
                    "long-session",
                    project_id="project-a",
                    threshold=4,
                )

                self.assertEqual(len(archive._records_for_session("long-session")), message_count)
                self.assertEqual(first["version"], 1)
                self.assertEqual(first["input_sequence_start"], max(1, message_count - 8 - 192 + 1))
                self.assertEqual(first["input_sequence_end"], message_count - 8)
                self.assertEqual(first["input_message_count"], min(192, message_count - 8))
                self.assertEqual(second["version"], 2)
                self.assertTrue(
                    (archive.archive_root / "long-session" / "summaries" / "summary-1.json.enc").exists()
                )
                self.assertTrue(
                    (archive.archive_root / "long-session" / "summaries" / "summary-2.json.enc").exists()
                )
            finally:
                store.close()

            reopened_archive = ConversationStore(path, b"k" * 32)
            reopened = ProductionSessionStore(reopened_archive)
            try:
                latest = reopened_archive.latest_summary("long-session")
                self.assertEqual(latest["version"], 2)
                self.assertEqual(len(reopened_archive._records_for_session("long-session")), message_count)
            finally:
                reopened.close()

    def test_compaction_rejects_explicit_cross_project_session(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.create_session("project-a-session", project_id="project-a", title="项目 A")
                for index in range(12):
                    store.append_message(
                        "project-a-session",
                        "user",
                        f"项目 A 消息 {index}",
                        request_id=f"project-a-request-{index}",
                        project_id="project-a",
                    )

                with self.assertRaisesRegex(ValueError, "another project"):
                    store.compact_session("project-a-session", project_id="project-b", threshold=4)
            finally:
                store.close()

    def test_late_older_summary_cannot_regress_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.create_session("summary-session", project_id="project-a", title="摘要会话")
                newest = archive.write_summary(
                    "summary-session",
                    "版本 2 摘要",
                    version=2,
                    covered_sequence_start=5,
                    covered_sequence_end=8,
                    source_message_ids=["m8"],
                )
                stale = archive.write_summary(
                    "summary-session",
                    "迟到的版本 1 摘要",
                    version=1,
                    covered_sequence_start=1,
                    covered_sequence_end=4,
                    source_message_ids=["m4"],
                )

                self.assertEqual(newest["version"], 2)
                self.assertEqual(stale["version"], 2)
                self.assertEqual(archive.latest_summary("summary-session")["summary"], "版本 2 摘要")
                self.assertFalse(
                    (archive.archive_root / "summary-session" / "summaries" / "summary-1.json.enc").exists()
                )
            finally:
                archive.close()

    def test_same_summary_version_rejects_a_different_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.create_session("summary-conflict", project_id=None, title="摘要冲突")
                archive.write_summary(
                    "summary-conflict",
                    "第一次摘要",
                    version=1,
                    covered_sequence_start=1,
                    covered_sequence_end=4,
                    source_message_ids=["m4"],
                )
                with self.assertRaisesRegex(ValueError, "summary version conflict"):
                    archive.write_summary(
                        "summary-conflict",
                        "不同摘要",
                        version=1,
                        covered_sequence_start=1,
                        covered_sequence_end=4,
                        source_message_ids=["m4"],
                    )
                self.assertEqual(
                    archive.latest_summary("summary-conflict")["summary"],
                    "第一次摘要",
                )
            finally:
                archive.close()

    def test_redis_summary_projection_rejects_a_different_payload_for_same_version(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def setex(self, key, _ttl, value):
                self.values[key] = value

            def eval(self, _script, numkeys, *args):
                summary_key, identity_key, version_key = args[:numkeys]
                incoming_version = int(args[numkeys])
                incoming_digest = args[numkeys + 1]
                sealed = args[numkeys + 2]
                expected_version = int(args[numkeys + 4])
                expected_digest = args[numkeys + 5]
                expected_value = args[numkeys + 6]
                current_value = self.values.get(summary_key)
                if current_value is not None:
                    if current_value != expected_value:
                        return -2
                    current_version = int(self.values.get(version_key) or expected_version)
                    if current_version > incoming_version:
                        return -3
                    if current_version == incoming_version:
                        return 0 if expected_digest == incoming_digest else -1
                self.values[summary_key] = sealed
                self.values[identity_key] = incoming_digest
                self.values[version_key] = str(incoming_version)
                return 2 if current_value is not None else 1

        redis_projection = object.__new__(RedisSessionStore)
        redis_projection.client = FakeRedis()
        redis_projection.key = b"k" * 32
        redis_projection.prefix = "test"
        redis_projection.ttl_seconds = 60
        redis_projection._summary_lock = threading.RLock()
        first = {
            "session_id": "summary-conflict",
            "version": 1,
            "summary": "第一次摘要",
            "covered_sequence_start": 1,
            "covered_sequence_end": 4,
            "source_message_ids": ["m4"],
        }
        different = {**first, "summary": "不同摘要"}

        self.assertTrue(redis_projection.set_summary("summary-conflict", first))
        with self.assertRaisesRegex(ValueError, "summary version conflict"):
            redis_projection.set_summary("summary-conflict", different)
        self.assertEqual(redis_projection.get_summary("summary-conflict")["summary"], "第一次摘要")

    def test_redis_summary_projection_fails_closed_on_cross_process_race(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.race_on_eval = False

            def get(self, key):
                return self.values.get(key)

            def setex(self, key, _ttl, value):
                if self.race_on_eval:
                    self.race_on_eval = False
                    self.values[key] = b"raced-summary"
                self.values[key] = value

            def eval(self, _script, numkeys, *args):
                summary_key = args[0]
                if self.race_on_eval:
                    self.race_on_eval = False
                    self.values[summary_key] = b"raced-summary"
                    return -2
                sealed = args[numkeys + 2]
                identity_key = args[1]
                version_key = args[2]
                incoming_digest = args[numkeys + 1]
                incoming_version = args[numkeys]
                self.values[summary_key] = sealed
                self.values[identity_key] = incoming_digest
                self.values[version_key] = str(incoming_version)
                return 1

        redis_projection = object.__new__(RedisSessionStore)
        redis_projection.client = FakeRedis()
        redis_projection.key = b"k" * 32
        redis_projection.prefix = "test"
        redis_projection.ttl_seconds = 60
        redis_projection._summary_lock = threading.RLock()
        first = {
            "session_id": "summary-race",
            "version": 1,
            "summary": "第一次摘要",
            "covered_sequence_start": 1,
            "covered_sequence_end": 4,
            "source_message_ids": ["m4"],
        }
        second = {**first, "version": 2, "summary": "第二次摘要"}

        self.assertTrue(redis_projection.set_summary("summary-race", first))
        redis_projection.client.race_on_eval = True
        with self.assertRaisesRegex(ValueError, "summary concurrent conflict"):
            redis_projection.set_summary("summary-race", second)
        self.assertEqual(
            redis_projection.client.values["test:session:summary-race:summary"],
            b"raced-summary",
        )

    def test_redis_session_projection_rejects_conflicting_session_identity(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.eval_calls = 0
                self.replace_before_compare = False

            def get(self, key):
                return self.values.get(key)

            def setex(self, key, _ttl, value):
                self.values[key] = value

            def eval(self, _script, numkeys, *args):
                self.eval_calls += 1
                metadata_key, digest_key = args[:numkeys]
                incoming_digest, sealed, _ttl, expected_digest, expected_value = args[numkeys:]
                current_digest = self.values.get(digest_key)
                if current_digest:
                    if current_digest != incoming_digest:
                        return -1
                    self.values[metadata_key] = sealed
                    return 0
                current_value = self.values.get(metadata_key)
                if current_value is not None:
                    if self.replace_before_compare:
                        self.replace_before_compare = False
                        self.values[metadata_key] = b"raced-metadata"
                        current_value = self.values[metadata_key]
                    if current_value != expected_value:
                        return -2
                    if expected_digest != incoming_digest:
                        return -1
                    self.values[metadata_key] = sealed
                    self.values[digest_key] = incoming_digest
                    return 0
                self.values[metadata_key] = sealed
                self.values[digest_key] = incoming_digest
                return 1

        redis_projection = object.__new__(RedisSessionStore)
        redis_projection.client = FakeRedis()
        redis_projection.key = b"k" * 32
        redis_projection.prefix = "test"
        redis_projection.ttl_seconds = 60
        first = {
            "session_id": "identity-session",
            "project_id": "project-a",
            "title": "项目 A",
            "workflow_id": "paper_review",
        }
        different = {**first, "project_id": "project-b"}

        redis_projection.set_session_metadata(first)
        redis_projection.set_session_metadata(first)
        stored = redis_projection.client.values["test:session:identity-session:metadata"]
        with self.assertRaisesRegex(ValueError, "session metadata conflict"):
            redis_projection.set_session_metadata(different)
        self.assertEqual(
            redis_projection.client.values["test:session:identity-session:metadata"],
            stored,
        )
        redis_projection.client.values["test:session:identity-session:metadata_identity"] = "concurrent-writer"
        with self.assertRaisesRegex(ValueError, "session metadata conflict"):
            redis_projection.set_session_metadata(first)
        self.assertEqual(
            redis_projection.client.values["test:session:identity-session:metadata"],
            stored,
        )
        redis_projection.client.values.pop("test:session:identity-session:metadata_identity")
        redis_projection.client.replace_before_compare = True
        with self.assertRaisesRegex(ValueError, "session metadata concurrent conflict"):
            redis_projection.set_session_metadata(first)
        self.assertEqual(
            redis_projection.client.values["test:session:identity-session:metadata"],
            b"raced-metadata",
        )
        self.assertEqual(redis_projection.client.eval_calls, 4)
        self.assertNotIn("test:session:identity-session:metadata_identity", redis_projection.client.values)

    def test_redis_message_append_is_atomic_and_rejects_conflicting_payload(self):
        class FakeRedis:
            def __init__(self):
                self.seen = set()
                self.digests = {}
                self.messages = []
                self.fail_next = True

            def sadd(self, _key, value):
                if value in self.seen:
                    return 0
                self.seen.add(value)
                return 1

            def pipeline(self):
                raise RuntimeError("redis write unavailable")

            def eval(self, _script, numkeys, *args):
                self.assert_numkeys = numkeys
                message_id, digest, token = args[numkeys:numkeys + 3]
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("redis write unavailable")
                if message_id in self.digests:
                    return 0 if self.digests[message_id] == digest else -1
                if message_id in self.seen:
                    return 0
                self.seen.add(message_id)
                self.digests[message_id] = digest
                self.messages.append(token)
                return 1

            def close(self):
                return None

        redis_projection = object.__new__(RedisSessionStore)
        redis_projection.client = FakeRedis()
        redis_projection.key = b"k" * 32
        redis_projection.prefix = "test"
        redis_projection.max_messages = 8
        redis_projection.ttl_seconds = 60
        record = {
            "session_id": "atomic-session",
            "sequence": 1,
            "message_id": "message-1",
            "role": "user",
            "content": "第一次正文",
            "metadata": {"status": "user"},
            "created_at": 1.0,
        }

        with self.assertRaisesRegex(RuntimeError, "redis write unavailable"):
            redis_projection.append(record)
        self.assertNotIn("message-1", redis_projection.client.seen)

        self.assertTrue(redis_projection.append(record))
        self.assertFalse(redis_projection.append(record))
        with self.assertRaisesRegex(ValueError, "message projection conflict"):
            redis_projection.append({**record, "content": "不同正文"})
        self.assertEqual(len(redis_projection.client.messages), 1)
        self.assertEqual(redis_projection.client.assert_numkeys, 3)

    def test_postgres_summary_projection_rejects_a_different_payload_for_same_version(self):
        class FakeCursor:
            def __init__(self):
                self.result = {"summary_hash": "hash-of-another-summary", "metadata_json": {}}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                if "SELECT summary_hash, metadata_json" not in statement:
                    raise AssertionError("conflicting summary must be checked before projection write")

            def fetchone(self):
                return self.result

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)
        with self.assertRaisesRegex(ValueError, "summary version conflict"):
            store.record_summary(
                {
                    "session_id": "summary-conflict",
                    "version": 1,
                    "summary": "第一次摘要",
                    "covered_sequence_start": 1,
                    "covered_sequence_end": 4,
                    "source_message_ids": ["m4"],
                    "created_at": 1.0,
                },
                "D:/archive/summary-conflict.json.enc",
            )

    def test_postgres_session_projection_rejects_conflicting_session_identity(self):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                if "SELECT project_id, title, workflow_id" not in statement:
                    raise AssertionError("conflicting session must be checked before projection write")

            def fetchone(self):
                return {
                    "project_id": "project-a",
                    "title": "项目 A",
                    "workflow_id": "paper_review",
                }

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(FakeCursor())
        with self.assertRaisesRegex(ValueError, "session metadata conflict"):
            store.upsert_session(
                "identity-session",
                project_id="project-b",
                archive_path="D:/archive/identity-session/messages.jsonl.enc",
                latest_sequence=0,
                created_at=1.0,
                title="项目 A",
                workflow_id="paper_review",
            )

    def test_postgres_message_projection_rejects_conflicting_message_identity(self):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                if "SELECT session_id, sequence, role, content_hash, metadata_json" not in statement:
                    raise AssertionError("conflicting message must be checked before projection write")

            def fetchone(self):
                return {
                    "session_id": "message-session",
                    "sequence": 1,
                    "role": "user",
                    "content_hash": "hash-of-another-message",
                    "metadata_json": {},
                }

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(FakeCursor())
        with self.assertRaisesRegex(ValueError, "message projection conflict"):
            store.record_message(
                {
                    "message_id": "message-conflict",
                    "session_id": "message-session",
                    "sequence": 1,
                    "role": "user",
                    "content": "另一条正文",
                    "metadata": {},
                    "created_at": 1.0,
                },
                "D:/archive/message-session/messages.jsonl.enc",
            )

    def test_custom_summary_builder_marks_excerpt_metadata_unknown(self):
        records = [
            {
                "session_id": "s1",
                "sequence": index,
                "message_id": f"m{index}",
                "role": "user",
                "content": f"消息 {index}",
            }
            for index in range(1, 10)
        ]

        summary = build_recoverable_summary(
            records,
            summary_builder=lambda covered, _max_chars: f"覆盖 {len(covered)} 条",
        )

        self.assertEqual(summary["excerpt_status"], "unknown")
        self.assertIsNone(summary["excerpt_message_ids"])
        self.assertIsNone(summary["excerpt_truncated"])

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

    def test_session_list_uses_session_id_as_stable_tie_breaker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            try:
                archive.create_session("session-b", project_id="project-a", title="会话 B")
                archive.create_session("session-a", project_id="project-a", title="会话 A")
                for session_id in ("session-a", "session-b"):
                    manifest_path = archive.archive_root / session_id / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["updated_at"] = 100.0
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False),
                        encoding="utf-8",
                    )
                discovered = [
                    archive.archive_root / "session-b" / "manifest.json",
                    archive.archive_root / "session-a" / "manifest.json",
                ]
                with patch.object(Path, "glob", return_value=discovered):
                    sessions = archive.list_sessions("project-a")
            finally:
                archive.close()

        self.assertEqual([item["session_id"] for item in sessions], ["session-a", "session-b"])

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

    def test_production_session_store_binds_create_to_postgres_workflow_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_postgres = Mock()
            store.postgres_enabled = True
            store.postgres = fake_postgres
            try:
                created = store.create_session(
                    "postgres-workflow-session",
                    project_id="project-a",
                    title="论文窗口",
                    workflow_id="paper_review",
                )
            finally:
                store.close()

        self.assertEqual(created["workflow_id"], "paper_review")
        kwargs = fake_postgres.upsert_session.call_args.kwargs
        self.assertEqual(kwargs["workflow_id"], "paper_review")

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
                store._dispatch_event(
                    {
                        "event_type": "redis.session_summary",
                        "aggregate_id": "already-deleted",
                        "payload": {
                            "summary": {
                                "session_id": "already-deleted",
                                "version": 1,
                                "summary": "不应恢复的旧摘要",
                            }
                        },
                    }
                )
                store._dispatch_event(
                    {
                        "event_type": "postgres.session_summary",
                        "aggregate_id": "already-deleted",
                        "payload": {
                            "summary": {
                                "session_id": "already-deleted",
                                "version": 1,
                                "summary": "不应恢复的旧摘要",
                                "covered_sequence_start": 1,
                                "covered_sequence_end": 1,
                                "created_at": 1.0,
                            },
                            "archive_path": "missing",
                        },
                    }
                )
            finally:
                store.close()

            fake_redis.set_session_metadata.assert_not_called()
            fake_redis.set_summary.assert_not_called()
            fake_postgres.upsert_session.assert_not_called()
            fake_postgres.record_summary.assert_not_called()

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
                    return [b"memory-a", b"1", json.dumps({
                        "projection_version": "memory_vector_v2",
                        "memory_id": "memory-a",
                        "session_id": "s1",
                        "scope": "global",
                        "project_id": "",
                        "status": "active",
                    }).encode()]
                raise AssertionError(command)

            def expire(self, *_):
                return True

            def persist(self, *_):
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

    def test_redis_vector_scope_keeps_workflow_facts_in_their_workflow(self):
        class FakeRedis:
            def __init__(self):
                self.vsim_calls = []

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VADD":
                    return 1
                if command == "VSIM":
                    self.vsim_calls.append(args)
                    return [
                        b"paper", b"1", json.dumps({
                            "projection_version": "memory_vector_v2",
                            "memory_id": "paper",
                            "scope": "global",
                            "project_id": "",
                            "status": "active",
                            "workflow_id": "paper_review",
                        }).encode(),
                        b"experiment", b"0.9", json.dumps({
                            "projection_version": "memory_vector_v2",
                            "memory_id": "experiment",
                            "scope": "global",
                            "project_id": "",
                            "status": "active",
                            "workflow_id": "experiment_compare",
                        }).encode(),
                        b"shared", b"0.8", json.dumps({
                            "projection_version": "memory_vector_v2",
                            "memory_id": "shared",
                            "scope": "global",
                            "project_id": "",
                            "status": "active",
                        }).encode(),
                    ]
                raise AssertionError(command)

            def expire(self, *_):
                return True

            def persist(self, *_):
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
            store.put_vector(
                memory_id="paper",
                session_id=None,
                memory_type="memory",
                vector=[1.0, 0.0],
                archive_path="D:/archive/paper.json.enc",
                workflow_id="paper_review",
            )
            paper = store.search_vector([1.0, 0.0], workflow_id="paper_review", top_k=10)
            generic = store.search_vector([1.0, 0.0], top_k=10)
            store.close()

        self.assertEqual([item["memory_id"] for item in paper], ["paper", "shared"])
        self.assertEqual([item["memory_id"] for item in generic], ["shared"])
        self.assertIn("FILTER", fake.vsim_calls[0])
        self.assertIn("FILTER-EF", fake.vsim_calls[0])
        filter_index = fake.vsim_calls[0].index("FILTER")
        vector_filter = str(fake.vsim_calls[0][filter_index + 1])
        self.assertIn(".workflow_id", vector_filter)
        self.assertIn("paper_review", vector_filter)

    def test_redisearch_vector_query_prefilters_model_time_and_workflow_scope(self):
        class FakeRedis:
            def __init__(self):
                self.search_query = None

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                if command == "FT.INFO":
                    raise RuntimeError("index not initialized")
                if command == "FT.CREATE":
                    return 1
                if command == "FT.SEARCH":
                    self.search_query = args[1]
                    return [0]
                raise AssertionError(command)

            def hset(self, *_args, **_kwargs):
                return 1

            def persist(self, *_args):
                return True

            def expire(self, *_args):
                return True

            def close(self):
                pass

        fake = FakeRedis()
        with patch("redis.Redis.from_url", return_value=fake):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            store.put_vector(
                memory_id="scoped-memory",
                session_id="session-a",
                memory_type="memory",
                vector=[1.0, 0.0],
                archive_path="D:/archive/scoped-memory.json.enc",
                scope="project",
                project_id="project-a",
                valid_from=10.0,
                invalid_at=200.0,
                embedding_model="ollama:test-embedding",
                workflow_id="paper_review",
            )
            store.search_vector(
                [1.0, 0.0],
                session_id="session-a",
                project_id="project-a",
                include_global=False,
                as_of=100.0,
                embedding_model="ollama:test-embedding",
                workflow_id="paper_review",
            )
            store.close()

        self.assertIsNotNone(fake.search_query)
        self.assertIn(r"@embedding_model:{ollama:test\-embedding}", fake.search_query)
        self.assertIn("@workflow_id:{paper_review}", fake.search_query)
        self.assertIn("@valid_from:", fake.search_query)
        self.assertIn("@invalid_at:", fake.search_query)

    def test_redis_vector_projection_rejects_unknown_workflow_identity(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                raise AssertionError(command)

            def close(self):
                pass

        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
                store.put_vector(
                    memory_id="unknown-workflow-vector",
                    session_id=None,
                    memory_type="memory",
                    vector=[1.0, 0.0],
                    archive_path="D:/archive/unknown-workflow-vector.json.enc",
                    workflow_id="unknown_window",
                )
            store.close()

    def test_redis_session_projection_rejects_unknown_workflow_identity(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                raise AssertionError(command)

            def close(self):
                pass

        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
                store.set_session_metadata({
                    "session_id": "unknown-workflow-session",
                    "workflow_id": "unknown_window",
                })
            store.close()

    def test_memory_vector_projection_does_not_inherit_short_term_session_ttl(self):
        class FakeRedis:
            def __init__(self):
                self.expire_calls = []

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VADD":
                    return 1
                raise AssertionError(command)

            def expire(self, *args):
                self.expire_calls.append(args)
                return True

            def persist(self, *args):
                self.persist_calls = getattr(self, "persist_calls", []) + [args]
                return True

            def info(self, **_):
                return {"redis_version": "8.10.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def scan_iter(self, **_):
                return iter(())

            def close(self):
                pass

        fake = FakeRedis()
        with patch("redis.Redis.from_url", return_value=fake):
            store = RedisSessionStore(
                "redis://127.0.0.1:6379/0",
                b"k" * 32,
                ttl_seconds=600,
            )
            self.assertTrue(store.put_vector(
                memory_id="durable-memory",
                session_id="s1",
                memory_type="memory",
                vector=[1.0, 0.0],
                archive_path="D:/archive/durable-memory.json.enc",
            ))
            health = store.health()
            store.close()
            default_expire_calls = list(fake.expire_calls)
            default_persist_calls = list(fake.persist_calls)

            bounded_store = RedisSessionStore(
                "redis://127.0.0.1:6379/0",
                b"k" * 32,
                ttl_seconds=600,
                memory_vector_ttl_seconds=3600,
            )
            self.assertTrue(bounded_store.put_vector(
                memory_id="bounded-memory",
                session_id="s1",
                memory_type="memory",
                vector=[0.0, 1.0],
                archive_path="D:/archive/bounded-memory.json.enc",
            ))
            bounded_health = bounded_store.health()
            bounded_store.close()

        self.assertEqual(default_expire_calls, [])
        self.assertEqual(default_persist_calls, [("souldrive:memory_vectors",)])
        self.assertIsNone(health["memory_vector_ttl_seconds"])
        self.assertEqual(health["memory_vector_expiration_policy"], "no_expiry")
        self.assertEqual(fake.expire_calls, [("souldrive:memory_vectors", 3600)])
        self.assertEqual(bounded_health["memory_vector_ttl_seconds"], 3600)
        self.assertEqual(bounded_health["memory_vector_expiration_policy"], "ttl")

    def test_production_memory_vector_projection_carries_scope_and_validity(self):
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
                store.persist_memory(
                    {
                        "memory_id": "project-memory",
                        "scope": "project",
                        "project_id": "project-a",
                        "status": "active",
                        "valid_from": 100.0,
                        "invalid_at": 200.0,
                    },
                    project_id="project-a",
                    vector=[1.0, 0.0],
                )
            finally:
                store.close()

        vector_call = fake_redis.put_vector.call_args.kwargs
        self.assertEqual(vector_call["scope"], "project")
        self.assertEqual(vector_call["project_id"], "project-a")
        self.assertEqual(vector_call["status"], "active")
        self.assertEqual(vector_call["valid_from"], 100.0)
        self.assertEqual(vector_call["invalid_at"], 200.0)
        self.assertEqual(vector_call["record_version"], 1)

    def test_project_argument_is_persisted_for_memory_replay_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            store.redis = fake_redis
            store._put_redis_vector_or_raise = Mock()
            try:
                store.persist_memory(
                    {
                        "memory_id": "project-argument-memory",
                        "status": "active",
                        "content": "项目参数传入的事实",
                    },
                    project_id="project-a",
                    vector=[0.25, 0.75],
                )
                archived = archive.read_memory("project-argument-memory")
                store._dispatch_event(
                    {
                        "event_type": "redis.memory_vector",
                        "aggregate_id": "project-argument-memory",
                        "payload": {
                            "vector": [0.25, 0.75],
                            "memory_type": "memory",
                            "record_version": 1,
                            "status": "active",
                        },
                    }
                )
            finally:
                store.close()

        self.assertEqual(archived["scope"], "project")
        self.assertEqual(archived["project_id"], "project-a")
        self.assertEqual(
            store._put_redis_vector_or_raise.call_args.kwargs["project_id"],
            "project-a",
        )

    def test_production_memory_postgres_projection_uses_memory_project_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_postgres = Mock()
            store.postgres_enabled = True
            store.postgres = fake_postgres
            try:
                store.persist_memory(
                    {
                        "memory_id": "project-scope-memory",
                        "scope": "project",
                        "project_id": "project-a",
                        "status": "active",
                        "content": "项目事实",
                    }
                )
            finally:
                store.close()

        self.assertEqual(
            fake_postgres.upsert_memory.call_args.args[2],
            "project-a",
        )

    def test_memory_projection_preserves_source_session_when_projection_call_omits_session(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            fake_postgres = Mock()
            store.redis_enabled = True
            store.redis = fake_redis
            store.postgres_enabled = True
            store.postgres = fake_postgres
            try:
                store.persist_memory(
                    {
                        "memory_id": "source-session-projection-memory",
                        "scope": "project",
                        "project_id": "project-a",
                        "source_session_id": "origin-session-42",
                        "status": "active",
                        "content": "来源会话必须跟随记忆投影。",
                    },
                    project_id="project-a",
                    vector=[0.5, 0.5],
                )
                archived = archive.read_memory("source-session-projection-memory")
            finally:
                store.close()

        self.assertEqual(archived["session_id"], "origin-session-42")
        self.assertEqual(
            fake_postgres.upsert_memory.call_args.args[3],
            "origin-session-42",
        )
        self.assertEqual(
            fake_redis.put_vector.call_args.kwargs["session_id"],
            "origin-session-42",
        )

    def test_revoked_memory_projection_preserves_source_session_when_projection_call_omits_session(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_postgres = Mock()
            store.postgres_enabled = True
            store.postgres = fake_postgres
            try:
                store.revoke_memory(
                    {
                        "memory_id": "source-session-revoked-memory",
                        "scope": "project",
                        "project_id": "project-a",
                        "source_session_id": "origin-session-43",
                        "status": "archived",
                        "content": "撤销后仍要保留来源会话。",
                    },
                    project_id="project-a",
                )
                archived = archive.read_memory("source-session-revoked-memory")
            finally:
                store.close()

        self.assertEqual(archived["session_id"], "origin-session-43")
        self.assertEqual(
            fake_postgres.upsert_memory.call_args.args[3],
            "origin-session-43",
        )

    def test_postgres_memory_replay_uses_canonical_archive_after_projection_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            failing_postgres = Mock()
            failing_postgres.upsert_memory.side_effect = RuntimeError("postgres offline")
            store.postgres_enabled = True
            store.postgres = failing_postgres
            try:
                store.persist_memory(
                    {
                        "memory_id": "replay-canonical-project-memory",
                        "status": "active",
                        "content": "项目事实",
                    },
                    project_id="project-a",
                )
                event = next(
                    item
                    for item in archive.pending_outbox(limit=20)
                    if item["event_type"] == "postgres.memory"
                )
                replay_postgres = Mock()
                store.postgres = replay_postgres
                store._dispatch_event(event)
            finally:
                store.close()

        replayed_memory = replay_postgres.upsert_memory.call_args.args[0]
        self.assertEqual(replayed_memory["scope"], "project")
        self.assertEqual(replayed_memory["project_id"], "project-a")
        self.assertEqual(
            replay_postgres.upsert_memory.call_args.args[2],
            "project-a",
        )

    def test_revoke_memory_persists_project_argument_for_durable_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            failing_postgres = Mock()
            failing_postgres.upsert_memory.side_effect = RuntimeError("postgres offline")
            store.postgres_enabled = True
            store.postgres = failing_postgres
            try:
                store.revoke_memory(
                    {
                        "memory_id": "revoked-project-memory",
                        "status": "revoked",
                        "content": "项目事实",
                    },
                    project_id="project-a",
                )
                archived = archive.read_memory("revoked-project-memory")
                event = next(
                    item
                    for item in archive.pending_outbox(limit=20)
                    if item["event_type"] == "postgres.memory"
                )
                replay_postgres = Mock()
                store.postgres = replay_postgres
                store._dispatch_event(event)
            finally:
                store.close()

        self.assertEqual(archived["scope"], "project")
        self.assertEqual(archived["project_id"], "project-a")
        self.assertEqual(
            replay_postgres.upsert_memory.call_args.args[2],
            "project-a",
        )

    def test_postgres_memory_outbox_replay_recovers_project_scope_from_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_postgres = Mock()
            store.postgres = fake_postgres
            try:
                memory = {
                    "memory_id": "replayed-project-memory",
                    "scope": "project",
                    "project_id": "project-a",
                    "status": "active",
                    "record_version": 2,
                    "content": "项目事实",
                }
                archive.archive_memory(memory)
                store._dispatch_event(
                    {
                        "event_type": "postgres.memory",
                        "aggregate_id": "replayed-project-memory",
                        "payload": {
                            "memory": memory,
                            "archive_path": "D:/archive/replayed-project-memory.json.enc",
                        },
                    }
                )
            finally:
                store.close()

        self.assertEqual(
            fake_postgres.upsert_memory.call_args.args[2],
            "project-a",
        )

    def test_redis_memory_vector_outbox_replay_recovers_project_scope_from_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            store.redis = fake_redis
            store._put_redis_vector_or_raise = Mock()
            try:
                memory = {
                    "memory_id": "replayed-project-vector",
                    "scope": "project",
                    "project_id": "project-a",
                    "status": "active",
                    "record_version": 2,
                    "content": "项目向量事实",
                }
                archive.archive_memory(memory)
                store._dispatch_event(
                    {
                        "event_type": "redis.memory_vector",
                        "aggregate_id": "replayed-project-vector",
                        "payload": {
                            "vector": [0.25, 0.75],
                            "memory_type": "memory",
                            "record_version": 2,
                            "status": "active",
                            "archive_path": "D:/archive/replayed-project-vector.json.enc",
                        },
                    }
                )
            finally:
                store.close()

        self.assertEqual(
            store._put_redis_vector_or_raise.call_args.kwargs["project_id"],
            "project-a",
        )

    def test_production_memory_archive_rejects_unknown_workflow_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
                    store.persist_memory(
                        {
                            "memory_id": "unknown-workflow-memory",
                            "subtype": "workflow_fact",
                            "workflow_id": "unknown_window",
                            "title": "未知窗口事实",
                            "content": "不得进入事实源",
                        }
                    )
                self.assertIsNone(archive.read_memory("unknown-workflow-memory"))
            finally:
                store.close()

    def test_redis_memory_vector_control_outbox_preserves_scope_and_workflow_identity(self):
        store = ProductionSessionStore.__new__(ProductionSessionStore)
        event = {
            "event_type": "redis.memory_vector",
            "aggregate_id": "workflow-memory",
            "payload": {
                "archive_path": "D:/archive/workflow-memory.json.enc",
                "session_id": "paper-session",
                "memory_type": "memory",
                "scope": "project",
                "project_id": "project-a",
                "status": "active",
                "valid_from": 100.0,
                "invalid_at": 200.0,
                "record_version": 3,
                "memory_identity_digest": "memory-digest-v3",
                "embedding_model": "ollama:test-embedding",
                "workflow_id": "paper_review",
                "memory_subtype": "workflow_fact",
                "vector": [0.25, 0.75],
            },
        }

        control = store._control_payload(event)

        self.assertEqual(control["scope"], "project")
        self.assertEqual(control["project_id"], "project-a")
        self.assertEqual(control["status"], "active")
        self.assertEqual(control["valid_from"], 100.0)
        self.assertEqual(control["invalid_at"], 200.0)
        self.assertEqual(control["workflow_id"], "paper_review")
        self.assertEqual(control["memory_subtype"], "workflow_fact")
        self.assertEqual(control["memory_identity_digest"], "memory-digest-v3")

    def test_memory_update_without_embedding_removes_stale_vector_projection(self):
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
                store.persist_memory(
                    {
                        "memory_id": "stale-vector-memory",
                        "record_version": 1,
                        "status": "active",
                        "title": "旧标题",
                        "content": "旧内容",
                    },
                    vector=[1.0, 0.0],
                )
                fake_redis.reset_mock()
                store.persist_memory(
                    {
                        "memory_id": "stale-vector-memory",
                        "record_version": 2,
                        "status": "active",
                        "title": "新标题",
                        "content": "新内容",
                    }
                )
            finally:
                store.close()

        fake_redis.put_vector.assert_not_called()
        fake_redis.remove_vector.assert_called_once_with("stale-vector-memory")

    def test_memory_vector_projection_rehydrates_from_encrypted_archive_with_matching_model(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
                "SOULDRIVE_EMBEDDING_PROVIDER": "ollama",
                "SOULDRIVE_OLLAMA_EMBEDDING_MODEL": "test-embedding",
            },
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            store.redis_enabled = True
            store.redis = fake_redis
            try:
                store.persist_memory(
                    {
                        "memory_id": "rehydrated-memory",
                        "record_version": 3,
                        "status": "active",
                        "scope": "project",
                        "project_id": "project-a",
                        "title": "可恢复记忆",
                        "content": "保留已生成向量",
                    },
                    project_id="project-a",
                    session_id="session-a",
                    vector=[0.25, 0.75],
                    vector_model_id="ollama:test-embedding",
                )
                archive.archive_memory(
                    {
                        "memory_id": "old-model-memory",
                        "record_version": 1,
                        "status": "active",
                        "scope": "global",
                        "semantic_embedding": [0.9, 0.1],
                        "semantic_embedding_model": "ollama:old-embedding",
                    }
                )
                fake_redis.reset_mock()
                result = store.rehydrate_memory_vectors()
                archived = archive.read_memory("rehydrated-memory")
            finally:
                store.close()

        fake_redis.put_vector.assert_called_once()
        vector_call = fake_redis.put_vector.call_args.kwargs
        self.assertEqual(vector_call["memory_id"], "rehydrated-memory")
        self.assertEqual(vector_call["vector"], [0.25, 0.75])
        self.assertEqual(vector_call["embedding_model"], "ollama:test-embedding")
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(archived["semantic_embedding"], [0.25, 0.75])
        self.assertEqual(archived["semantic_embedding_model"], "ollama:test-embedding")

    def test_memory_vector_rehydrate_is_not_skipped_by_legacy_outbox_error(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(
            ProductionSessionStore,
            "replay_outbox",
            side_effect=ValueError("legacy archive key"),
        ), patch.object(
            ProductionSessionStore,
            "rehydrate_memory_vectors",
            return_value={
                "schema_version": "memory_vector_rehydrate_v1",
                "status": "disabled",
                "scanned": 0,
                "attempted": 0,
                "restored": 0,
                "skipped": 0,
                "failed": 0,
            },
        ) as rehydrate:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                self.assertEqual(store.memory_vector_rehydrate["status"], "disabled")
            finally:
                store.close()

        rehydrate.assert_called_once_with()

    def test_degraded_memory_vector_rehydrate_marks_storage_health_degraded(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                store.memory_vector_rehydrate = {
                    **store.memory_vector_rehydrate,
                    "status": "degraded",
                    "failed": 1,
                }
                health = store.health()
            finally:
                store.close()

        self.assertTrue(health["degraded"])

    def test_health_rehydrates_vectors_after_redis_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch.object(
            ProductionSessionStore,
            "rehydrate_memory_vectors",
            return_value={
                "schema_version": "memory_vector_rehydrate_v1",
                "status": "ok",
                "scanned": 1,
                "attempted": 1,
                "restored": 1,
                "skipped": 0,
                "failed": 0,
            },
        ) as rehydrate:
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            fake_redis.health.return_value = {"enabled": True, "status": "ok"}
            store.redis_enabled = True
            store.redis = fake_redis
            store.memory_vector_rehydrate = {
                **store.memory_vector_rehydrate,
                "status": "unavailable",
            }
            rehydrate.reset_mock()
            try:
                health = store.health()
            finally:
                store.close()

        rehydrate.assert_called_once_with()
        self.assertEqual(health["memory_vector_rehydrate"]["status"], "ok")

    def test_memory_vector_rehydrate_uses_durable_cursor_across_store_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
                "SOULDRIVE_EMBEDDING_PROVIDER": "ollama",
                "SOULDRIVE_OLLAMA_EMBEDDING_MODEL": "test-embedding",
            },
            clear=False,
        ):
            database = str(Path(temp_dir) / "conversations.sqlite")
            archive = ConversationStore(database, b"k" * 32)
            for memory_id, vector in (("memory-a", [0.1, 0.9]), ("memory-b", [0.2, 0.8])):
                archive.archive_memory(
                    {
                        "memory_id": memory_id,
                        "record_version": 1,
                        "status": "active",
                        "scope": "global",
                        "semantic_embedding": vector,
                        "semantic_embedding_model": "ollama:test-embedding",
                    }
                )
            first_store = ProductionSessionStore(archive)
            first_redis = Mock()
            first_store.redis_enabled = True
            first_store.redis = first_redis
            first = first_store.rehydrate_memory_vectors(limit=1)
            first_store.close()

            reopened_archive = ConversationStore(database, b"k" * 32)
            second_store = ProductionSessionStore(reopened_archive)
            second_redis = Mock()
            second_store.redis_enabled = True
            second_store.redis = second_redis
            try:
                second = second_store.rehydrate_memory_vectors(limit=1)
            finally:
                second_store.close()

        self.assertEqual(first["scanned"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["scanned"], 1)
        self.assertFalse(second["has_more"])
        self.assertEqual(second_redis.put_vector.call_args.kwargs["memory_id"], "memory-b")

    def test_durable_memory_replay_cannot_regress_to_an_older_record_version(self):
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
                store.persist_memory(
                    {
                        "memory_id": "versioned-memory",
                        "record_version": 2,
                        "status": "active",
                        "content": "新版本内容",
                    },
                    vector=[1.0, 0.0],
                )
                store.revoke_memory(
                    {
                        "memory_id": "versioned-memory",
                        "record_version": 2,
                        "status": "archived",
                        "content": "新版本内容",
                    }
                )
                store.persist_memory(
                    {
                        "memory_id": "versioned-memory",
                        "record_version": 2,
                        "status": "active",
                        "content": "不应恢复的活动版本",
                    },
                    vector=[0.5, 0.5],
                )
                store.persist_memory(
                    {
                        "memory_id": "versioned-memory",
                        "record_version": 1,
                        "status": "active",
                        "content": "旧版本内容",
                    },
                    vector=[0.0, 1.0],
                )
                current = archive.read_memory("versioned-memory")
            finally:
                store.close()

        self.assertEqual(current["record_version"], 2)
        self.assertEqual(current["content"], "新版本内容")
        self.assertEqual(current["status"], "archived")
        self.assertEqual(fake_redis.put_vector.call_count, 1)

    def test_memory_backend_outbox_event_ids_include_record_version(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                for version in (1, 2):
                    store._try_backend(
                        "redis",
                        lambda: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
                        event={
                            "event_type": "redis.memory_vector",
                            "aggregate_id": "versioned-memory",
                            "payload": {
                                "memory_id": "versioned-memory",
                                "record_version": version,
                                "status": "active",
                            },
                        },
                    )
                store._try_backend(
                    "postgres",
                    lambda: (_ for _ in ()).throw(RuntimeError("postgres unavailable")),
                    event={
                        "event_type": "postgres.memory",
                        "aggregate_id": "versioned-memory",
                        "payload": {
                            "memory": {
                                "memory_id": "versioned-memory",
                                "record_version": 2,
                                "status": "archived",
                            },
                        },
                    },
                )
                pending = archive.pending_outbox()
            finally:
                store.close()

        self.assertEqual(len(pending), 3)
        self.assertEqual(
            {item["event_id"] for item in pending},
            {
                "redis_versioned-memory_v1_redis_memory_vector_active",
                "redis_versioned-memory_v2_redis_memory_vector_active",
                "postgres_versioned-memory_v2_postgres_memory_archived",
            },
        )

    def test_session_backend_outbox_events_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            try:
                def fail_backend():
                    raise RuntimeError("backend unavailable")

                store._try_backend(
                    "redis",
                    fail_backend,
                    event={
                        "event_type": "redis.session_created",
                        "aggregate_id": "session-lifecycle",
                        "payload": {"metadata": {"session_id": "session-lifecycle"}},
                    },
                )
                store._try_backend(
                    "redis",
                    fail_backend,
                    event={
                        "event_type": "redis.session_summary",
                        "aggregate_id": "session-lifecycle",
                        "payload": {"summary": {"session_id": "session-lifecycle", "version": 1}},
                    },
                )
                pending = archive.pending_outbox()
            finally:
                store.close()

        self.assertEqual(len(pending), 2)
        self.assertEqual(
            {item["event_id"] for item in pending},
            {
                "redis_session-lifecycle_redis_session_created",
                "redis_session-lifecycle_redis_session_summary",
            },
        )

    def test_stale_memory_backend_events_are_acknowledged_without_side_effects(self):
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
                archive.archive_memory(
                    {
                        "memory_id": "versioned-memory",
                        "record_version": 2,
                        "status": "active",
                    }
                )
                store._dispatch_event(
                    {
                        "event_type": "redis.memory_vector_delete",
                        "aggregate_id": "versioned-memory",
                        "payload": {
                            "memory_id": "versioned-memory",
                            "record_version": 1,
                        },
                    }
                )
                store._dispatch_event(
                    {
                        "event_type": "postgres.memory",
                        "aggregate_id": "versioned-memory",
                        "payload": {
                            "memory": {
                                "memory_id": "versioned-memory",
                                "record_version": 1,
                            },
                            "record_version": 1,
                        },
                    }
                )
            finally:
                store.close()

        fake_redis.remove_vector.assert_not_called()
        fake_postgres.upsert_memory.assert_not_called()

    def test_redis_vector_false_result_is_kept_in_outbox_for_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            fake_redis = Mock()
            fake_redis.put_vector.return_value = False
            store.redis = fake_redis
            store.redis_enabled = True
            try:
                store.persist_memory(
                    {
                        "memory_id": "vector-outbox-memory",
                        "record_version": 1,
                        "status": "active",
                        "scope": "project",
                        "project_id": "project-a",
                        "title": "向量投影失败",
                        "content": "Redis 向量索引暂不可用。",
                    },
                    project_id="project-a",
                    vector=[0.25, 0.75],
                    vector_model_id="ollama:test-embedding",
                )
                pending = archive.pending_outbox()
                fake_redis.put_vector.return_value = True
                store.redis = fake_redis
                replay = store.replay_outbox()
            finally:
                store.close()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_type"], "redis.memory_vector")
        self.assertEqual(pending[0]["aggregate_id"], "vector-outbox-memory")
        self.assertIn("vector projection", store.backend_errors["redis"])
        self.assertEqual(replay["completed"], 1)
        self.assertEqual(archive.pending_outbox(), [])

    def test_redis_vector_delete_exception_is_kept_in_outbox(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VREM":
                    raise RuntimeError("vector delete unavailable")
                raise AssertionError(command)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"SOULDRIVE_REDIS_ENABLED": "0", "SOULDRIVE_POSTGRES_ENABLED": "0"},
            clear=False,
        ), patch("redis.Redis.from_url", return_value=FakeRedis()):
            archive = ConversationStore(str(Path(temp_dir) / "conversations.sqlite"), b"k" * 32)
            store = ProductionSessionStore(archive)
            redis_store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertTrue(redis_store.ensure_vector_index(2))
            store.redis = redis_store
            store.redis_enabled = True
            try:
                store._try_backend(
                    "redis",
                    lambda: store._require_redis().remove_vector("delete-outbox-memory"),
                    event={
                        "event_type": "redis.memory_vector_delete",
                        "aggregate_id": "delete-outbox-memory",
                        "payload": {"memory_id": "delete-outbox-memory", "record_version": 2},
                    },
                )
                pending = archive.pending_outbox()
            finally:
                store.close()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_type"], "redis.memory_vector_delete")
        self.assertEqual(pending[0]["aggregate_id"], "delete-outbox-memory")

    def test_postgres_memory_projection_rejects_older_version_before_upsert(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=()):
                self.calls.append((statement, params))

            def fetchone(self):
                return {"metadata_json": {"record_version": 2}}

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self):
                return self._cursor

        cursor = FakeCursor()
        store = PostgresStateStore.__new__(PostgresStateStore)
        store.lock = threading.RLock()
        store.connection = FakeConnection(cursor)

        store.upsert_memory(
            {
                "memory_id": "versioned-memory",
                "record_version": 1,
                "status": "active",
                "content": "旧版本",
            },
            "D:/archive/versioned-memory.json.enc",
        )

        self.assertEqual(len(cursor.calls), 1)
        self.assertTrue(cursor.calls[0][0].startswith("SELECT project_id, session_id"))

    def test_redis_vector_set_filters_project_scope_and_validity(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VADD":
                    return 1
                if command == "VSIM":
                    records = [
                        {
                            "projection_version": "memory_vector_v2",
                            "memory_id": "project-a-memory",
                            "session_id": "s1",
                            "scope": "project",
                            "project_id": "project-a",
                            "status": "active",
                            "embedding_model": "ollama:test-embedding",
                        },
                        {
                            "projection_version": "memory_vector_v2",
                            "memory_id": "global-memory",
                            "session_id": "s1",
                            "scope": "global",
                            "project_id": "",
                            "status": "active",
                            "embedding_model": "ollama:test-embedding",
                        },
                        {
                            "projection_version": "memory_vector_v2",
                            "memory_id": "project-b-memory",
                            "session_id": "s1",
                            "scope": "project",
                            "project_id": "project-b",
                            "status": "active",
                            "embedding_model": "ollama:test-embedding",
                        },
                        {
                            "projection_version": "memory_vector_v2",
                            "memory_id": "future-memory",
                            "session_id": "s1",
                            "scope": "project",
                            "project_id": "project-a",
                            "status": "active",
                            "embedding_model": "ollama:test-embedding",
                            "valid_from": 200.0,
                        },
                    ]
                    raw = []
                    for index, record in enumerate(records):
                        raw.extend([
                            record["memory_id"].encode(),
                            str(1.0 - index / 10).encode(),
                            json.dumps(record).encode(),
                        ])
                    return raw
                raise AssertionError(command)

            def expire(self, *_):
                return True

            def persist(self, *_):
                return True

            def info(self, **_):
                return {"redis_version": "8.10.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def close(self):
                pass

        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertTrue(store.put_vector(
                memory_id="seed",
                session_id="s1",
                memory_type="memory",
                vector=[1.0, 0.0],
                archive_path="D:/archive/seed.json.enc",
                scope="project",
                project_id="project-a",
            ))
            scoped = store.search_vector(
                [1.0, 0.0],
                session_id="s1",
                project_id="project-a",
                include_global=True,
                as_of=150.0,
                embedding_model="ollama:test-embedding",
                top_k=10,
            )
            project_only = store.search_vector(
                [1.0, 0.0],
                session_id="s1",
                project_id="project-a",
                include_global=False,
                as_of=150.0,
                embedding_model="ollama:test-embedding",
                top_k=10,
            )
            store.close()

        self.assertEqual(
            [item["memory_id"] for item in scoped],
            ["project-a-memory", "global-memory"],
        )
        self.assertEqual([item["memory_id"] for item in project_only], ["project-a-memory"])

    def test_redis_vector_set_prefilters_temporal_validity(self):
        class FakeRedis:
            def __init__(self):
                self.vsim_calls = []

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command.startswith("FT."):
                    raise RuntimeError("RediSearch is unavailable")
                if command == "COMMAND INFO":
                    return [b"VADD"]
                if command == "VADD":
                    return 1
                if command == "VSIM":
                    self.vsim_calls.append(args)
                    if "FILTER" in args:
                        return []
                    return [
                        b"legacy-memory",
                        b"1",
                        json.dumps({
                            "projection_version": "memory_vector_v2",
                            "memory_id": "legacy-memory",
                            "scope": "global",
                            "project_id": "",
                            "status": "active",
                        }).encode(),
                    ]
                raise AssertionError(command)

            def expire(self, *_):
                return True

            def persist(self, *_):
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
            store.put_vector(
                memory_id="temporal-memory",
                session_id=None,
                memory_type="memory",
                vector=[1.0, 0.0],
                archive_path="D:/archive/temporal-memory.json.enc",
            )
            results = store.search_vector([1.0, 0.0], as_of=100.0, top_k=3)
            store.close()

        self.assertEqual([item["memory_id"] for item in results], ["legacy-memory"])
        self.assertGreaterEqual(len(fake.vsim_calls), 1)
        filter_index = fake.vsim_calls[0].index("FILTER")
        vector_filter = str(fake.vsim_calls[0][filter_index + 1])
        self.assertIn(".valid_from", vector_filter)
        self.assertIn(".invalid_at", vector_filter)
        self.assertIn("<= 100", vector_filter)
        self.assertIn("> 100", vector_filter)

    def test_redisearch_vector_search_exposes_distance_for_semantic_merge(self):
        class FakeRedis:
            def __init__(self):
                self.search_args = None

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "FT.INFO":
                    raise RuntimeError("index missing")
                if command == "FT.CREATE":
                    return b"OK"
                if command == "FT.SEARCH":
                    self.search_args = args
                    return [
                        1,
                        b"souldrive:memory:memory-a",
                        [
                            b"memory_id", b"memory-a",
                            b"session_id", b"",
                            b"memory_type", b"memory",
                            b"archive_path", b"archive",
                            b"projection_version", b"memory_vector_v2",
                            b"scope", b"global",
                            b"project_id", b"",
                            b"status", b"active",
                            b"distance", b"0.2",
                        ],
                    ]
                raise AssertionError(command)

            def info(self, **_):
                return {"redis_version": "7.4.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def hget(self, key, field):
                self.hget_call = (key, field)
                return b"2" if field == "record_version" else None

            def scan_iter(self, **_):
                return iter(())

            def close(self):
                pass

        fake = FakeRedis()
        with patch("redis.Redis.from_url", return_value=fake):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertTrue(store.ensure_vector_index(2))
            results = store.search_vector([1.0, 0.0], top_k=1)
            store.close()

        self.assertEqual(results[0]["memory_id"], "memory-a")
        self.assertEqual(results[0]["distance"], "0.2")
        self.assertEqual(results[0]["record_version"], 2)
        self.assertEqual(fake.hget_call, ("souldrive:memory:memory-a", "record_version"))
        self.assertIn("distance", fake.search_args)

    def test_redisearch_index_creation_race_rechecks_existing_index(self):
        class FakeRedis:
            def __init__(self):
                self.info_calls = 0

            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                if command == "FT.INFO":
                    self.info_calls += 1
                    if self.info_calls == 1:
                        raise RuntimeError("index missing")
                    return [b"index_name", b"test:memory_idx"]
                if command == "FT.CREATE":
                    raise RuntimeError("Index already exists")
                if command == "COMMAND INFO":
                    raise RuntimeError("Vector Set unavailable")
                raise AssertionError(command)

            def info(self, **_):
                return {"redis_version": "7.4.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def scan_iter(self, **_):
                return iter(())

            def close(self):
                pass

        fake = FakeRedis()
        with patch("redis.Redis.from_url", return_value=fake):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertTrue(store.ensure_vector_index(2))
            store.close()

        self.assertEqual(fake.info_calls, 2)

    def test_redisearch_existing_index_dimension_mismatch_fails_closed(self):
        class FakeRedis:
            def ping(self):
                return True

            def execute_command(self, command, *args):
                if command == "VINFO":
                    raise RuntimeError("vector set not initialized")
                if command == "FT.INFO":
                    return [
                        b"attributes",
                        [[
                            b"identifier", b"embedding",
                            b"attribute", b"embedding",
                            b"type", b"VECTOR",
                            b"algorithm", b"HNSW",
                            b"algorithm_attribute",
                            [b"TYPE", b"FLOAT32", b"DIM", b"2560", b"DISTANCE_METRIC", b"COSINE"],
                        ]],
                    ]
                raise AssertionError(command)

            def info(self, **_):
                return {"redis_version": "7.4.0"}

            def config_get(self, *_):
                return {"appendonly": "yes"}

            def scan_iter(self, **_):
                return iter(())

            def close(self):
                pass

        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            store = RedisSessionStore("redis://127.0.0.1:6379/0", b"k" * 32)
            self.assertFalse(store.ensure_vector_index(3))
            self.assertIn("expected 2560, got 3", store.health()["vector_index_error"])
            store.close()

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
