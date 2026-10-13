import threading
from unittest.mock import patch

from core.conversation_store import ConversationStore
from core.state_backends import ProductionSessionStore


def _local_store(archive):
    store = ProductionSessionStore.__new__(ProductionSessionStore)
    store.archive = archive
    store.redis = None
    store.postgres = None
    store._outbox_replay_lock = threading.Lock()
    store._connect_backends = lambda: None
    return store


def test_local_outbox_failure_receipt_error_does_not_stop_later_events(tmp_path):
    archive = ConversationStore(str(tmp_path / "conversations.sqlite"), b"k" * 32)
    try:
        archive.append_outbox({"event_id": "local-fail", "event_type": "test.replay", "created_at": 1.0, "payload": {}})
        archive.append_outbox({"event_id": "local-ok", "event_type": "test.replay", "created_at": 2.0, "payload": {}})
        original_mark = archive.mark_outbox
        mark_calls = []

        def mark_outbox(event_id, status, error=None, *, claim_token=None):
            mark_calls.append((event_id, status))
            if event_id == "local-fail" and status == "pending":
                raise OSError("receipt store unavailable")
            return original_mark(event_id, status, error, claim_token=claim_token)

        archive.mark_outbox = mark_outbox
        dispatched = []

        def dispatch(event):
            dispatched.append(event["event_id"])
            if event["event_id"] == "local-fail":
                raise RuntimeError("projection unavailable")

        store = _local_store(archive)
        store._dispatch_event = dispatch
        with patch("core.state_backends.logger.warning") as warning:
            result = store._replay_outbox_locked()

        assert result == {"completed": 1, "failed": 1}
        assert dispatched == ["local-fail", "local-ok"]
        assert mark_calls == [("local-fail", "pending"), ("local-ok", "completed")]
        warning.assert_called_once_with("failed to persist local conversation outbox failure receipt")
    finally:
        archive.close()


def test_postgres_outbox_failure_receipt_error_does_not_stop_later_events():
    class FakeArchive:
        def pending_outbox(self, limit=100):
            return []

    class FakePostgres:
        def __init__(self):
            self.events = [
                {"event_id": "postgres-fail", "event_type": "test.replay", "payload_json": {}, "claim_token": "claim-a"},
                {"event_id": "postgres-ok", "event_type": "test.replay", "payload_json": {}, "claim_token": "claim-b"},
            ]
            self.mark_calls = []

        def claim_pending_outbox(self, limit=100, *, owner_id=None):
            return list(self.events)

        def mark_outbox(self, event_id, status, error="", *, claim_token=None):
            self.mark_calls.append((event_id, status, claim_token))
            if event_id == "postgres-fail" and status == "pending":
                raise OSError("receipt store unavailable")
            return True

    postgres = FakePostgres()
    store = ProductionSessionStore.__new__(ProductionSessionStore)
    store.archive = FakeArchive()
    store.postgres = postgres
    store._outbox_replay_lock = threading.Lock()
    store._connect_backends = lambda: None
    dispatched = []

    def dispatch(event):
        dispatched.append(event["event_id"])
        if event["event_id"] == "postgres-fail":
            raise RuntimeError("projection unavailable")

    store._dispatch_event = dispatch
    with patch("core.state_backends.logger.warning") as warning:
        result = store._replay_outbox_locked()

    assert result == {"completed": 1, "failed": 1}
    assert dispatched == ["postgres-fail", "postgres-ok"]
    assert postgres.mark_calls == [
        ("postgres-fail", "pending", "claim-a"),
        ("postgres-ok", "completed", "claim-b"),
    ]
    warning.assert_called_once_with("failed to persist PostgreSQL conversation outbox failure receipt")


def test_context_load_keeps_local_history_when_outbox_replay_raises(tmp_path):
    archive = ConversationStore(str(tmp_path / "conversations.sqlite"), b"k" * 32)
    try:
        archive.append("session-1", "user", "已落盘的消息", {})
        store = _local_store(archive)
        store.replay_outbox = lambda limit=20: (_ for _ in ()).throw(OSError("temporary replay failure"))
        with patch("core.state_backends.logger.warning") as warning:
            result = store.load_context("session-1")

        assert result["history"][0]["content"] == "已落盘的消息"
        warning.assert_called_once_with(
            "conversation outbox replay failed during context load",
            exc_info=True,
        )
    finally:
        archive.close()


def test_load_before_keeps_local_history_when_outbox_replay_raises(tmp_path):
    archive = ConversationStore(str(tmp_path / "conversations.sqlite"), b"k" * 32)
    try:
        archive.append("session-1", "user", "更早的消息", {})
        store = _local_store(archive)
        store.replay_outbox = lambda limit=20: (_ for _ in ()).throw(OSError("temporary replay failure"))
        with patch("core.state_backends.logger.warning") as warning:
            result = store.load_before("session-1", 10)

        assert [item["content"] for item in result["records"]] == ["更早的消息"]
        warning.assert_called_once_with(
            "conversation outbox replay failed during history pagination",
            exc_info=True,
        )
    finally:
        archive.close()
