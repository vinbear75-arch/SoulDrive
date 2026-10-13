import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

from core.atomic_file import atomic_write_text
from core.enterprise_security import sanitize_payload
from core.local_file_lock import local_file_lock
from core.paths import local_data_root


logger = logging.getLogger(__name__)


DEFAULT_AUDIT_PATH: str | None = None
DEFAULT_AUDIT_STATE_PATH: str | None = None


class AuditLogger:
    def __init__(self, audit_path: str | os.PathLike[str] | None = None, state_path: str | os.PathLike[str] | None = None):
        self._audit_path = str(audit_path) if audit_path is not None else None
        self._state_path = str(state_path) if state_path is not None else None

    @property
    def audit_path(self) -> str:
        return self._audit_path or default_audit_path()

    @property
    def state_path(self) -> str:
        return self._state_path or default_audit_state_path()

    @classmethod
    def for_workspace(cls, workspace):
        return cls(workspace.audit_log_path, workspace.audit_state_path)

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        sanitized_payload = self._sanitize_payload(payload or {})
        with local_file_lock(f"{self.audit_path}.lock"):
            previous_hash = self._read_last_hash()
            event = {
                "event_id": str(uuid.uuid4()),
                "trace_id": trace_id or sanitized_payload.get("trace_id") or str(uuid.uuid4()),
                "event_type": event_type,
                "timestamp": time.time(),
                "previous_hash": previous_hash,
                "payload": sanitized_payload,
            }
            event["event_hash"] = self._hash_event(event)

            with open(self.audit_path, "a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                file.flush()
                os.fsync(file.fileno())

            self._write_state(event["event_hash"])
            return event

    def _read_last_hash(self) -> str:
        if not os.path.exists(self.audit_path):
            return "GENESIS"

        last_line = ""
        with open(self.audit_path, "r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    last_line = line

        if not last_line:
            return "GENESIS"

        try:
            payload = json.loads(last_line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("audit log tail is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("audit log tail is invalid")
        last_hash = payload.get("event_hash") or payload.get("hash")
        if not last_hash:
            raise ValueError("audit log tail is invalid")
        return str(last_hash)

    def _hash_event(self, event: dict[str, Any]) -> str:
        payload = {key: value for key, value in event.items() if key not in ("hash", "event_hash")}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _write_state(self, last_event_hash: str):
        atomic_write_text(
            self.state_path,
            json.dumps({"last_event_hash": last_event_hash}, ensure_ascii=False, indent=2),
        )

    def _sanitize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_payload(payload)
        if isinstance(sanitized, dict) and "hardware_sn" in sanitized:
            hardware_sn_hash = sanitized.pop("hardware_sn")
            sanitized["hardware_sn_hash"] = hardware_sn_hash
        return sanitized

    def read_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        if not os.path.exists(self.audit_path):
            return []

        records = []
        with open(self.audit_path, "r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue

        return records[-max(1, min(limit, 200)):]

    def verify_chain(self, limit: int | None = None) -> dict[str, Any]:
        if not os.path.exists(self.audit_path):
            return {
                "ready": True,
                "audit_log": _public_audit_log_id(self.audit_path),
                "event_count": 0,
                "checked_events": 0,
                "last_event_hash": "GENESIS",
                "first_timestamp": None,
                "last_timestamp": None,
                "broken_at": None,
                "invalid_lines": [],
            }

        records = []
        invalid_lines = []
        with open(self.audit_path, "r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    records.append((line_number, json.loads(line)))
                except Exception:
                    invalid_lines.append(line_number)

        limited = limit is not None and limit > 0
        if limited:
            records_to_check = records[-limit:]
        else:
            records_to_check = records

        previous_hash = records_to_check[0][1].get("previous_hash") if limited and records_to_check else "GENESIS"
        broken_at = None
        last_hash = "GENESIS"
        first_timestamp = None
        last_timestamp = None

        for line_number, record in records_to_check:
            if first_timestamp is None:
                first_timestamp = record.get("timestamp")
            expected_hash = self._hash_event(record)
            if record.get("event_hash") != expected_hash:
                broken_at = {"line": line_number, "reason": "event_hash_mismatch"}
                break
            if record.get("previous_hash") != previous_hash:
                broken_at = {"line": line_number, "reason": "previous_hash_mismatch"}
                break
            last_hash = str(record.get("event_hash") or "GENESIS")
            previous_hash = last_hash
            last_timestamp = record.get("timestamp")

        return {
            "ready": not invalid_lines and broken_at is None,
            "audit_log": _public_audit_log_id(self.audit_path),
            "event_count": len(records),
            "checked_events": len(records_to_check),
            "last_event_hash": last_hash,
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "broken_at": broken_at,
            "invalid_lines": invalid_lines[:20],
        }


def append_audit_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    audit_path: str | os.PathLike[str] | None = None,
    state_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    return AuditLogger(audit_path, state_path).append_event(event_type, payload, trace_id)


def append_audit_event_best_effort(
    audit_logger: Any,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    trace_id: str | None = None,
) -> bool:
    """Write observability without making the business operation fail.

    The local audit file is a valuable diagnostic sink, but it is not the
    authorization or state-transition mechanism.  Callers must still expose
    their own authorization failures and business errors; this helper only
    prevents an audit I/O failure from masquerading as one of those errors.
    """
    try:
        if trace_id is None:
            audit_logger.append_event(event_type, payload)
        else:
            audit_logger.append_event(event_type, payload, trace_id=trace_id)
    except Exception:
        try:
            from core.observability import runtime_metrics

            runtime_metrics.increment("audit_failures")
        except Exception:
            pass
        # Do not include the exception text or payload: both can contain
        # user-provided query text, local paths, or secrets.
        logger.error("Best-effort audit event write failed: %s", event_type)
        return False
    return True


def default_audit_path() -> str:
    return DEFAULT_AUDIT_PATH or str(local_data_root() / "audit_log.jsonl")


def default_audit_state_path() -> str:
    return DEFAULT_AUDIT_STATE_PATH or str(local_data_root() / "audit_state.json")


def _public_audit_log_id(audit_path: str) -> str:
    digest = hashlib.sha256(str(audit_path).encode("utf-8")).hexdigest()[:12]
    return f"audit_log:{digest}"


default_audit_logger = AuditLogger()
