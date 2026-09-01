import copy
import json
import os
import time
from pathlib import Path
from core.audit_log import AuditLogger, default_audit_logger
from core.paths import local_data_root
from core.workspace import SoulDriveWorkspace

STATE_PATH: str | None = None

DEFAULT_STATE = {
    "locked": True,
    "reason": "storage device required",
    "auth_level": "NONE",
    "hardware_sn": None,
    "active_drive": None,
    "workspace_path": None,
    "hardware_mounted": False,
    "crypto_initialized": False,
    "software_unlocked": False,
    "security_reason": "storage device required",
    "indexing": {
        "status": "idle",
        "run_id": None,
        "total_files": 0,
        "discovered_files": 0,
        "skipped_files": 0,
        "succeeded_files": 0,
        "processed_files": 0,
        "current_file": None,
        "failures": [],
        "failure_summary": {},
        "chunk_count": 0,
        "started_at": None,
        "finished_at": None,
        "disk": {},
        "observability": {
            "schema_version": "indexing_observability_v4",
            "documents": [],
            "totals": {
                "parse_ms": 0,
                "ingest_ms": 0,
                "graph_ms": 0,
                "elapsed_ms": 0,
                "chunk_chars": 0,
                "child_count": 0,
                "embedding_ms": 0,
                "vector_store_ms": 0,
                "keyword_index_ms": 0,
                "parent_store_ms": 0,
                "child_split_ms": 0,
                "raw_items": 0,
                "kept_items": 0,
                "removed_page_artifacts": 0,
                "removed_non_content": 0,
                "removed_duplicates": 0,
                "table_items": 0,
                "image_items": 0,
                "ocr_ms": 0,
                "ocr_region_count": 0,
                "ocr_mean_confidence": 0.0,
                "pages": 0,
                "parent_chunks": 0,
                "conversion_error_count": 0,
                "converted_pages": 0,
                "failed_page_count": 0,
                "formal_window_count": 0,
                "boundary_overlap_opportunity_count": 0,
                "boundary_overlap_count": 0,
                "boundary_overlap_truncated_count": 0,
                "boundary_overlap_token_skip_count": 0,
                "boundary_overlap_coverage_rate": None,
                "max_child_tokens": 0,
                "max_embedding_input_tokens": 0,
                "embedding_token_limit": 0,
                "child_strategy_counts": {},
                "conversion_status_counts": {},
                "modality_counts": {},
            },
        },
    },
    "updated_at": 0,
}


def default_state():
    return copy.deepcopy(DEFAULT_STATE)


def get_runtime_state():
    path = runtime_state_path()
    if not os.path.exists(path):
        return default_state()

    try:
        with open(path, "r", encoding="utf-8") as file:
            state = json.load(file)
    except Exception:
        return default_state()

    merged = default_state()
    merged.update(state)
    indexing = copy.deepcopy(DEFAULT_STATE["indexing"])
    indexing.update(state.get("indexing") or {})
    merged["indexing"] = indexing
    return merged


def set_runtime_state(**updates):
    path = runtime_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = get_runtime_state()
    state.update(updates)
    state["updated_at"] = time.time()

    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return state


def runtime_state_path() -> str:
    return STATE_PATH or str(local_data_root() / "runtime_state.json")


def workspace_runtime_state_path(workspace_path: str) -> str:
    return str(Path(workspace_path) / "runtime" / "runtime_state.json")


def use_workspace_runtime_state(workspace_path: str) -> str:
    global STATE_PATH
    STATE_PATH = workspace_runtime_state_path(workspace_path)
    return STATE_PATH


def unlock_runtime(
    auth_level: str,
    hardware_sn: str | None,
    active_drive: str | None,
    workspace_path: str | None = None,
    reason: str = "workspace mounted",
):
    audit_logger = _audit_logger_for_workspace_path(workspace_path)
    previous = get_runtime_state()
    previous_indexing = previous.get("indexing") or {}
    preserve_completed_index = (
        previous.get("workspace_path") == workspace_path
        and previous_indexing.get("status") == "completed"
        and int(previous_indexing.get("processed_files") or 0) > 0
    )
    updates = dict(
        locked=True,
        reason="workspace passphrase required",
        auth_level="HARDWARE_ONLY",
        hardware_sn=hardware_sn,
        active_drive=active_drive,
        workspace_path=workspace_path,
        hardware_mounted=True,
        crypto_initialized=False,
        software_unlocked=False,
        security_reason="workspace passphrase required",
    )
    if not preserve_completed_index:
        updates["indexing"] = copy.deepcopy(DEFAULT_STATE["indexing"])
    state = set_runtime_state(**updates)
    audit_logger.append_event("runtime.unlock", {
        "auth_level": "HARDWARE_ONLY",
        "hardware_sn_hash": _hash_sensitive(hardware_sn),
        "active_drive": active_drive,
        "reason": "workspace passphrase required",
    })
    return state


def mark_software_unlocked(reason: str = "workspace unlocked"):
    state = set_runtime_state(
        locked=False,
        reason=reason,
        auth_level="HARDWARE_PLUS_PASSWORD",
        hardware_mounted=True,
        software_unlocked=True,
        security_reason=reason,
    )
    _audit_logger_for_workspace_path(state.get("workspace_path")).append_event("security.unlock_succeeded", {})
    return state


def mark_software_locked(reason: str = "workspace passphrase required"):
    previous_state = get_runtime_state()
    state = set_runtime_state(
        locked=True,
        reason=reason,
        auth_level="HARDWARE_ONLY" if previous_state.get("hardware_mounted") else "NONE",
        software_unlocked=False,
        security_reason=reason,
    )
    _audit_logger_for_workspace_path(previous_state.get("workspace_path")).append_event("security.software_locked", {
        "reason": reason,
    })
    return state


def lock_runtime(reason: str = "storage device removed"):
    previous_state = get_runtime_state()
    audit_logger = _audit_logger_for_workspace_path(previous_state.get("workspace_path"))
    state = set_runtime_state(
        locked=True,
        reason=reason,
        auth_level="NONE",
        hardware_sn=None,
        active_drive=None,
        workspace_path=None,
        hardware_mounted=False,
        crypto_initialized=False,
        software_unlocked=False,
        security_reason=reason,
        indexing={
            "status": "locked",
            "run_id": None,
            "total_files": 0,
            "discovered_files": 0,
            "skipped_files": 0,
            "succeeded_files": 0,
            "processed_files": 0,
            "current_file": None,
            "failures": [],
            "failure_summary": {},
            "chunk_count": 0,
            "started_at": None,
            "finished_at": None,
            "disk": {},
            "observability": copy.deepcopy(DEFAULT_STATE["indexing"]["observability"]),
        },
    )
    audit_logger.append_event("runtime.lock", {
        "reason": reason,
    })
    return state


def _hash_sensitive(value: str | None):
    if not value:
        return None
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit_logger_for_workspace_path(workspace_path: str | None):
    if not workspace_path:
        return default_audit_logger
    try:
        return AuditLogger.for_workspace(SoulDriveWorkspace(workspace_path).ensure())
    except Exception:
        return default_audit_logger


def update_indexing_status(**updates):
    state = get_runtime_state()
    indexing = copy.deepcopy(DEFAULT_STATE["indexing"])
    indexing.update(state.get("indexing") or {})
    indexing.update(updates)
    return set_runtime_state(indexing=indexing)
