"""真实 Redis 短期会话与 session_id 恢复验收。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.conversation_store import ConversationStore
from core.state_backends import ProductionSessionStore


def main() -> None:
    os.environ["SOULDRIVE_REDIS_ENABLED"] = "1"
    os.environ["SOULDRIVE_POSTGRES_ENABLED"] = "0"
    test_root = Path("D:/AI_AGENTS/SoulDrive-main/runtime/redis-session-acceptance")
    test_root.mkdir(parents=True, exist_ok=True)
    session_id = f"redis-accept-{uuid.uuid4().hex[:16]}"
    project_id = f"redis-project-{uuid.uuid4().hex[:8]}"
    memory_id = f"redis-vector-test-{uuid.uuid4().hex[:16]}"
    key = b"r" * 32
    result: dict[str, object] = {"session_id": session_id, "project_id": project_id}

    with tempfile.TemporaryDirectory(prefix="case-", dir=test_root) as temp_dir:
        archive_path = str(Path(temp_dir) / "conversations.sqlite")
        archive = ConversationStore(archive_path, key)
        store = ProductionSessionStore(archive)
        if store.redis is None:
            raise RuntimeError("real Redis backend did not connect")
        redis_store = store.redis
        result["redis_health_before"] = redis_store.health()

        store.create_session(session_id, project_id=project_id, title="Redis recovery acceptance")
        store.append_message(session_id, "user", "短期会话用户消息", project_id=project_id, request_id=f"{session_id}-u")
        store.append_message(session_id, "assistant", "短期会话助手消息", project_id=project_id, request_id=f"{session_id}-a")
        before = store.load_context(session_id, limit=8)
        redis_records = redis_store.recent(session_id, limit=8)
        result["before_close"] = {
            "source": before["source"],
            "history": before["history"],
            "redis_record_count": len(redis_records),
            "redis_message_ids": [item["message_id"] for item in redis_records],
        }
        if len(redis_records) != 2:
            raise AssertionError("short-term messages were not written to real Redis")

        store.close()

        reopened = ProductionSessionStore(ConversationStore(archive_path, key))
        if reopened.redis is None:
            raise RuntimeError("real Redis backend did not reconnect")
        redis_store = reopened.redis
        after = reopened.load_context(session_id, limit=8)
        if [item["content"] for item in after["history"]] != ["短期会话用户消息", "短期会话助手消息"]:
            raise AssertionError("session history did not survive store reopen")
        if reopened.session_metadata(session_id)["project_id"] != project_id:
            raise AssertionError("session project binding was not restored")
        with_project_mismatch = False
        try:
            reopened.create_session(session_id, project_id=f"other-{project_id}", title="other project attempt")
        except ValueError:
            with_project_mismatch = True
        if not with_project_mismatch:
            raise AssertionError("session_id can be rebound to another project")
        result["after_reopen"] = {
            "source": after["source"],
            "history": after["history"],
            "redis_count": after["redis_count"],
            "archive_count": after["archive_count"],
            "project_sessions": [item["session_id"] for item in reopened.list_sessions(project_id)],
        }

        redis_only_dir = Path(temp_dir) / "redis-only"
        redis_only_dir.mkdir()
        redis_only_path = str(redis_only_dir / "conversations.sqlite")
        redis_only = ProductionSessionStore(ConversationStore(redis_only_path, key))
        redis_only_context = redis_only.load_context(session_id, limit=8)
        if [item["content"] for item in redis_only_context["history"]] != ["短期会话用户消息", "短期会话助手消息"]:
            raise AssertionError("Redis hot-session recovery did not work without the original archive")
        result["redis_only_recovery"] = {
            "source": redis_only_context["source"],
            "redis_count": redis_only_context["redis_count"],
            "archive_count": redis_only_context["archive_count"],
            "history_count": len(redis_only_context["history"]),
        }
        redis_only.close()

        dimension = int(redis_store._vector_dimension or 2)
        vector = [0.0] * dimension
        vector[0] = 1.0
        vector_written = redis_store.put_vector(
            memory_id=memory_id,
            session_id=session_id,
            memory_type="short_term_test_only",
            vector=vector,
            archive_path="D:/AI_AGENTS/SoulDrive-main/runtime/redis-session-acceptance/test.json.enc",
        )
        vector_hits = redis_store.search_vector(vector, session_id=session_id, top_k=20)
        result["vector_capability"] = {
            "dimension": dimension,
            "backend": redis_store._vector_backend,
            "write_ok": vector_written,
            "hit_memory_ids": [item.get("memory_id") for item in vector_hits],
            "message_vectors_are_automatic": False,
        }
        if not vector_written or memory_id not in {item.get("memory_id") for item in vector_hits}:
            raise AssertionError("real Redis vector write/search did not round-trip")

        result["acceptance"] = {
            "redis_ping_and_health": result["redis_health_before"]["status"] == "ok",
            "short_term_messages_in_redis": len(redis_records) == 2,
            "session_id_project_binding": with_project_mismatch,
            "reopen_history_roundtrip": len(after["history"]) == 2,
            "redis_only_hot_recovery": len(redis_only_context["history"]) == 2,
            "redis_vector_capability": bool(vector_written),
            "short_term_memory_uses_vector_index": False,
        }
        # The test session is unique; remove only its Redis hot keys.
        cleanup = redis_store.client
        redis_store.remove_vector(memory_id)
        cleanup.delete(
            f"souldrive:session:{session_id}:messages",
            f"souldrive:session:{session_id}:message_ids",
            f"souldrive:session:{session_id}:summary",
            f"souldrive:session:{session_id}:metadata",
        )
        reopened.close()

    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
