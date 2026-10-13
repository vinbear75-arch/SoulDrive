import time
import math
import uuid
import sqlite3
import inspect
import os
import json
import re
import hashlib
import hmac
import subprocess
import sys
import threading
import shutil
import secrets
import tempfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI
from fastapi import File, Form, Header, Query, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from core.api_security import TOKEN_HEADER, is_authorized_token
from core.audit_log import AuditLogger, append_audit_event_best_effort, default_audit_logger
from core.cloud_model import (
    CloudModelConfigurationError,
    CloudModelRequestError,
    load_cloud_model_config,
    saved_or_requested_models,
    save_cloud_model_config,
)
from core.diagnostics import product_diagnostics
from core.experiment_ledger import (
    compare_experiment_runs,
    find_previous_month_runs,
    import_metric_source,
    summarize_experiment_repeats,
)
from core.document_types import SUPPORTED_DOCUMENT_EXTENSIONS, SUPPORTED_PAPER_EXTENSIONS, document_media_type
from core.enterprise_security import (
    SlidingWindowRateLimiter,
    allowed_origins,
    clamp_chat_top_k,
    is_loopback_client,
    max_body_bytes,
    rate_limit_per_minute,
)
from core.learning_store import LearningStore
from core.local_file_lock import local_file_lock
from core.conversation_store import ConversationStore
from core.episodic_view import (
    build_episodic_view,
    select_episodic_events_for_query,
)
from core.state_backends import ProductionSessionStore
from core.learning_validator import validate_learning_candidates
from core.interaction_context import (
    VISUAL_EVENT_MODALITIES,
    normalize_interaction_context,
    summarize_interaction_context,
)
from core.index_run_journal import (
    IndexRunInputConflict,
    IndexRunJournal,
    index_run_error_observation,
    index_run_input_digest,
)
from core.multimodal_workflows import (
    get_workflow_spec,
    list_workflow_specs,
    plan_workflow,
    workflow_retrieval_variants,
)
from core.soulalign import decide as soulalign_decide
from core.topic5_runtime import DEFAULT_MAX_NEW_TOKENS, clear_topic5_runtime, get_topic5_runtime
from core.memory_gate import contains_sensitive_memory, contains_untrusted_memory_instruction
from core.memory_runtime import gate_memory_context
from core.temporal_memory import resolve_memory_time_scope
from core.observability import runtime_metrics
from core.paper_importer import file_md5, import_paper_into_workspace
from core.plain_document_parser import PlainDocumentParser
from core.presentation_service import PresentationService
from core.project_catalog import ProjectCatalog
from core.task_journal import TaskJournal
from core.paths import app_root
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.runtime_state import (
    get_runtime_state,
    lock_runtime,
    mark_software_locked,
    mark_software_unlocked,
    unlock_runtime,
    update_indexing_status,
)
from core.security_context import (
    WORKSPACE_DATA_KEY_ENV,
    clear_workspace_keys,
    export_workspace_data_key,
    get_workspace_keys,
    set_workspace_keys,
)
from core.secure_document_store import SecureDocumentStore
from core.secure_graph_store import SecureGraphStore
from core.secure_vector_store import SecureVectorStore
from core.reflection import build_reflection_candidates, build_reflection_prompt, parse_reflection_response
from core.skill_runtime import (
    create_skill_execution_plan,
    execute_skill_script,
    is_skill_visible_in_workflow,
    match_skills,
)
from core.project_skills import discover_project_skills
from core.query_expansion import expand_query_variants
from core.project_skill_installer import (
    SkillInstallError,
    approve_skill_from_inbox,
    activate_skill_from_inbox,
    delete_project_skill_source_path,
    ensure_project_skill_presentation_adapter,
    install_skill_to_inbox,
    list_skill_inbox,
)
from core.email_connector import EmailConfigurationError, EmailDeliveryError, configure_smtp, send_confirmed_email, smtp_status
from core.calendar_export import export_ics, normalize_start_at
from core.tool_registry import build_default_tool_registry
from core.workspace import SoulDriveWorkspace, is_souldrive_workspace, resolve_workspace
from core.workspace_backup import build_backup_preflight
from core.workspace_crypto import (
    IncorrectPassphraseError,
    KeystoreAlreadyInitializedError,
    KeystoreNotInitializedError,
    initialize_keystore,
    is_keystore_initialized,
    unlock_keystore,
)
from core.logging_config import get_logger

logger = get_logger(__name__)

app = FastAPI(title="SoulDrive Edge Server", description="灵枢私有化边缘知识引擎核心服务")
rate_limiter = SlidingWindowRateLimiter(limit=rate_limit_per_minute())
PUBLIC_PATHS = {"/ping", "/health", "/ready", "/runtime/status", "/security/status"}
RATE_LIMIT_EXEMPT_PATHS = PUBLIC_PATHS

# =====================================================================
# --- 跨域策略配置 (CORS) ---
# =====================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def enterprise_boundary_middleware(request: Request, call_next):
    started_at = time.time()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    try:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_body_bytes():
            runtime_metrics.increment("rejected_body_requests")
            response = JSONResponse({"error": "request body too large", "request_id": request_id}, status_code=413)
            response.headers["X-Request-ID"] = request_id
            runtime_metrics.record_request(int((time.time() - started_at) * 1000), response.status_code)
            return response

        client_host = request.client.host if request.client else "local"
        if not is_loopback_client(client_host):
            runtime_metrics.increment("rejected_remote_requests")
            response = JSONResponse({"error": "SoulDrive local API only accepts loopback clients", "request_id": request_id}, status_code=403)
            response.headers["X-Request-ID"] = request_id
            runtime_metrics.record_request(int((time.time() - started_at) * 1000), response.status_code)
            return response

        if request.url.path not in RATE_LIMIT_EXEMPT_PATHS and not rate_limiter.allow(client_host):
            runtime_metrics.increment("rate_limited_requests")
            response = JSONResponse({"error": "rate limit exceeded", "request_id": request_id}, status_code=429)
            response.headers["X-Request-ID"] = request_id
            runtime_metrics.record_request(int((time.time() - started_at) * 1000), response.status_code)
            return response

        if request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
            token = request.headers.get(TOKEN_HEADER)
            if not is_authorized_token(token):
                runtime_metrics.increment("unauthorized_requests")
                response = JSONResponse({"error": "unauthorized SoulDrive local API request", "request_id": request_id}, status_code=401)
                response.headers["X-Request-ID"] = request_id
                runtime_metrics.record_request(int((time.time() - started_at) * 1000), response.status_code)
                return response

        response = await call_next(request)
    except Exception as exc:
        logger.exception("SoulDrive API request failed: %s %s (request_id=%s)", request.method, request.url.path, request_id)
        runtime_metrics.record_error(str(exc))
        response = JSONResponse({"error": "internal SoulDrive API error", "request_id": request_id}, status_code=500)

    response.headers["X-Request-ID"] = request_id
    runtime_metrics.record_request(int((time.time() - started_at) * 1000), response.status_code)
    return response

# =====================================================================
# --- 架构级全局单例注入 (Global Dependency Injection) ---
# =====================================================================
logger.info("[System] SoulDrive sidecar API 已启动，知识引擎将按需懒加载。")

kb = None
rag = None
loaded_workspace_path = None
session_store: ProductionSessionStore | None = None
session_store_workspace_path: str | None = None
session_store_lock = threading.Lock()
indexer_process: subprocess.Popen | None = None
indexer_lock = threading.Lock()
_graphrag_auto_start_attempted: set[str] = set()
_memory_outbox_startup_replayed: set[str] = set()
presentation_threads: dict[str, threading.Thread] = {}
presentation_thread_lock = threading.Lock()
document_delete_authorizations: dict[str, dict[str, float | str]] = {}
document_delete_authorizations_lock = threading.Lock()


def current_workspace():
    state = get_runtime_state()
    if state.get("workspace_path"):
        return SoulDriveWorkspace(state["workspace_path"]).ensure()
    if state.get("active_drive"):
        return resolve_workspace(state.get("active_drive"))
    raise RuntimeError("removable SoulDrive workspace is not mounted")


def require_workspace_mounted():
    state = get_runtime_state()
    if state.get("workspace_path") or state.get("active_drive"):
        return None
    return JSONResponse(
        {
            "error": "SoulDrive removable workspace is not mounted",
            "status": "locked",
            "reason": "waiting for removable SoulDrive workspace",
        },
        status_code=423,
    )


def current_audit_logger():
    state = get_runtime_state()
    if state.get("workspace_path"):
        try:
            return AuditLogger.for_workspace(SoulDriveWorkspace(state["workspace_path"]).ensure())
        except Exception:
            return default_audit_logger
    if state.get("active_drive"):
        try:
            return AuditLogger.for_workspace(resolve_workspace(state.get("active_drive")))
        except Exception:
            return default_audit_logger
    return default_audit_logger


def _append_audit_best_effort(
    event_type: str,
    payload: dict[str, Any],
    *,
    trace_id: str | None = None,
) -> str | None:
    """Keep a local business result truthful when its audit sink is unavailable."""
    if not append_audit_event_best_effort(
        current_audit_logger(),
        event_type,
        payload,
        trace_id=trace_id,
    ):
        return "审计记录写入失败：请稍后检查本地审计状态"
    return None


def _error_observation(exc: BaseException) -> dict[str, Any]:
    """Keep failure correlation without persisting exception contents."""
    return _message_observation(str(exc or ""), type(exc).__name__)


_PUBLIC_EMAIL_FAILURE = "邮件发送失败，未确认已发出；请检查发件配置、收件人和附件后重试。"


def _message_observation(message: str, error_type: str = "UnknownError") -> dict[str, Any]:
    """Convert a legacy/raw diagnostic message to a bounded error fingerprint."""
    message = str(message or "")
    return {
        "error_type": str(error_type or "UnknownError")[:96],
        "error_digest": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(message),
    }


def _sanitize_storage_health(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep storage status useful without returning backend errors or local paths."""
    sanitized = dict(payload or {})
    sections = {
        "file_archive",
        "redis",
        "postgres",
        "memory_vector_rehydrate",
        "outbox",
    }
    error_fields = {
        "error",
        "sqlite_index_error",
        "archive_read_error",
        "vector_index_error",
    }
    for section_name in sections:
        section = sanitized.get(section_name)
        if not isinstance(section, dict):
            continue
        section = dict(section)
        section.pop("root", None)
        section.pop("path", None)
        observations = {}
        for field in error_fields:
            raw = section.pop(field, None)
            if raw not in (None, ""):
                observations[field] = _message_observation(str(raw))
        if observations:
            section["error_observations"] = observations
        sanitized[section_name] = section
    return sanitized


def ensure_knowledge_base_loaded():
    global kb, loaded_workspace_path
    workspace = current_workspace()
    if loaded_workspace_path and loaded_workspace_path != workspace.root_path:
        cleanup_runtime()
    if kb is None:
        from core.knowledge_base import LocalKnowledgeBase

        kb = LocalKnowledgeBase(
            db_path=workspace.chroma_path,
            parent_doc_path=workspace.parent_doc_path,
            keyword_index_path=workspace.keyword_index_path,
            workspace_path=workspace.root_path,
        )
    loaded_workspace_path = workspace.root_path
    _replay_learning_memory_outbox_on_workspace_load(workspace)
    return kb, workspace


def ensure_runtime_loaded():
    global rag
    kb_instance, workspace = ensure_knowledge_base_loaded()
    if rag is None:
        from core.rag_engine import RAGEngine

        rag = RAGEngine(
            kb=kb_instance,
            graph_db_path=workspace.graph_db_path,
            workspace_path=workspace.root_path,
            audit_logger=current_audit_logger(),
        )


def _release_rag_generation_model_for_multimodal():
    """Yield GPU residency to an explicitly requested visual model."""
    release_fn = getattr(rag, "release_generation_model", None) if rag is not None else None
    if callable(release_fn):
        return release_fn()
    return {"released": False, "model_loaded": False}


def _conversation_store(workspace):
    global session_store, session_store_workspace_path
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        return None
    with session_store_lock:
        if session_store is not None and session_store_workspace_path == workspace.root_path:
            return session_store
        if session_store is not None:
            session_store.close()
        session_store = ProductionSessionStore(
            ConversationStore(workspace.conversation_store_path, keys.metadata_key)
        )
        session_store_workspace_path = workspace.root_path
        return session_store


def _close_session_store():
    global session_store, session_store_workspace_path
    with session_store_lock:
        if session_store is not None:
            session_store.close()
        session_store = None
        session_store_workspace_path = None


def _storage_health() -> dict[str, Any]:
    try:
        store = _conversation_store(current_workspace())
        if store is None:
            return {"schema_version": "storage_health_v1", "status": "locked", "degraded": True}
        return _sanitize_storage_health(store.health())
    except Exception as exc:
        return {
            "schema_version": "storage_health_v1",
            "status": "error",
            "degraded": True,
            **_error_observation(exc),
        }


def cleanup_runtime():
    global kb, rag, loaded_workspace_path
    previous_workspace_path = loaded_workspace_path
    if rag is not None:
        close_fn = getattr(rag, "close", None)
        if callable(close_fn):
            close_fn()
        rag = None
    if kb is not None:
        close_fn = getattr(kb, "close", None)
        if callable(close_fn):
            close_fn()
    kb = None
    loaded_workspace_path = None
    if previous_workspace_path:
        _memory_outbox_startup_replayed.discard(previous_workspace_path)
    _close_session_store()
    # Multimodal models are independent of the RAG store but must be released
    # when the local workspace is locked or switched.
    clear_topic5_runtime()


def _cleanup_portable_session(workspace: SoulDriveWorkspace | None = None):
    workspace = workspace or _workspace_from_state()
    _stop_indexer_worker()
    cleanup_runtime()
    _cleanup_workspace_temp_files(workspace)
    if workspace is not None:
        clear_workspace_keys(workspace.root_path)


def _cleanup_workspace_temp_files(workspace: SoulDriveWorkspace | None = None):
    if workspace is None:
        return
    runtime_path = Path(workspace.runtime_path)
    if not runtime_path.exists():
        return
    for path in runtime_path.glob("secure-index-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def _indexer_worker_command(
    workspace: SoulDriveWorkspace,
    auth_level: str,
    *,
    run_id: str | None = None,
    idempotency_key: str | None = None,
) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [
            sys.executable,
            "indexer",
            "--workspace-path",
            workspace.root_path,
            "--auth-level",
            auth_level,
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "core.indexer_worker",
            "--workspace-path",
            workspace.root_path,
            "--auth-level",
            auth_level,
        ]
    if run_id:
        command.extend(["--run-id", str(run_id)])
    if idempotency_key:
        command.extend(["--idempotency-key", str(idempotency_key)])
    return command


def _subprocess_creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _indexer_worker_identity(process) -> dict[str, int | float]:
    try:
        pid = int(getattr(process, "pid"))
    except (TypeError, ValueError, AttributeError):
        return {}
    try:
        import psutil

        started_at = float(psutil.Process(pid).create_time())
    except Exception:
        started_at = None
    identity: dict[str, int | float] = {"worker_pid": pid}
    if started_at is not None:
        identity["worker_started_at"] = started_at
    return identity


def _start_indexer_worker(
    workspace: SoulDriveWorkspace,
    auth_level: str,
    *,
    run_id: str | None = None,
    idempotency_key: str | None = None,
):
    global indexer_process
    command = _indexer_worker_command(
        workspace,
        auth_level,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )
    env = os.environ.copy()
    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        env[WORKSPACE_DATA_KEY_ENV] = export_workspace_data_key(workspace_keys)
    with indexer_lock:
        if indexer_process is not None:
            if indexer_process.poll() is None:
                return None
            indexer_process = None

        indexer_process = subprocess.Popen(
            command,
            cwd=str(app_root()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_subprocess_creation_flags(),
        )
        identity = _indexer_worker_identity(indexer_process)
        if identity:
            state = get_runtime_state()
            indexing = state.get("indexing") or {}
            update_indexing_status(
                status="queued",
                run_id=str(run_id or indexing.get("run_id") or uuid.uuid4()),
                idempotency_key=(
                    idempotency_key
                    if idempotency_key is not None
                    else indexing.get("idempotency_key")
                ),
                started_at=indexing.get("started_at") or time.time(),
                finished_at=None,
                **identity,
            )
        return indexer_process


def _stop_indexer_worker():
    global indexer_process
    with indexer_lock:
        process = indexer_process
        indexer_process = None

    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


_PUBLIC_INDEX_FAILURE_REASONS = {
    "PARTIAL_DOCUMENT_PARSE": "文档解析不完整",
    "NO_PARSEABLE_CHUNKS": "未提取到可用内容",
    "ENCRYPTED_DOCUMENT": "文档受密码保护",
    "FILE_ACCESS_DENIED": "无法读取文档",
    "FILE_NOT_FOUND": "文档不存在",
    "RESOURCE_EXHAUSTED": "索引资源不足",
    "INSUFFICIENT_DISK_SPACE": "磁盘空间不足",
    "INDEXER_WORKER_EXITED": "索引 worker 意外退出",
}


def _public_source_filename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    if re.match(r"^(?:[A-Za-z]:/|/|//)", normalized):
        return normalized.rsplit("/", 1)[-1][:256]
    return normalized[:256]


def _public_indexing_failure(failure: dict[str, Any]) -> dict[str, Any]:
    error_code = str(failure.get("error_code") or "INDEXING_FAILED")
    reason = str(failure.get("reason") or "")
    return {
        "source_filename": _public_source_filename(failure.get("source_filename")),
        **{key: str(failure[key]) for key in ("document_id", "project_id") if failure.get(key)},
        "error_code": error_code,
        "category": str(failure.get("category") or "unknown"),
        "reason": _PUBLIC_INDEX_FAILURE_REASONS.get(error_code, "索引失败"),
        "error_observation": _message_observation(reason),
    }


def _public_indexing_observability(observability: object) -> dict[str, Any]:
    """Project legacy indexing observations without returning raw diagnostics."""
    if not isinstance(observability, dict):
        return {}
    public = dict(observability)
    documents = []
    for item in public.get("documents") or []:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry["source_filename"] = _public_source_filename(entry.get("source_filename"))
        raw_error = entry.pop("error", None) or entry.pop("error_message", None)
        raw_reason = entry.pop("failure_reason", None) or entry.pop("reason", None)
        if raw_error or raw_reason:
            entry["error_observation"] = _message_observation(str(raw_error or raw_reason))
        documents.append(entry)
    public["documents"] = documents

    graph = public.get("graphrag")
    if isinstance(graph, dict):
        graph = dict(graph)
        raw_error = graph.pop("error", None) or graph.pop("error_message", None)
        if raw_error:
            graph["error_observation"] = _message_observation(str(raw_error))
        project_indexes = []
        for item in graph.get("project_indexes") or []:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            raw_error = entry.pop("error", None) or entry.pop("error_message", None)
            if raw_error:
                entry["error_observation"] = _message_observation(str(raw_error))
            project_indexes.append(entry)
        graph["project_indexes"] = project_indexes
        public["graphrag"] = graph
    return public


def public_runtime_state():
    _reconcile_indexer_state()
    state = effective_runtime_state()
    public_state = dict(state)
    public_indexing = dict(public_state.get("indexing") or {})
    public_indexing["failures"] = [
        _public_indexing_failure(item)
        for item in (public_indexing.get("failures") or [])
        if isinstance(item, dict)
    ]
    public_indexing["observability"] = _public_indexing_observability(
        public_indexing.get("observability")
    )
    if public_indexing.get("current_file"):
        public_indexing["current_file"] = _public_source_filename(public_indexing["current_file"])
    public_state["indexing"] = public_indexing
    if public_state.get("active_drive"):
        public_state["active_drive"] = "mounted removable storage"
    if public_state.get("workspace_path"):
        public_state["workspace_path"] = "SoulDrive workspace mounted"
    if public_state.get("hardware_sn"):
        public_state["hardware_sn"] = "HASHED"
    return public_state


def _reconcile_indexer_state():
    """Turn an unexpected worker exit into an observable terminal state."""
    global indexer_process
    with indexer_lock:
        process = indexer_process
        if process is None:
            return
        return_code = process.poll()
        if return_code is None:
            return
        indexer_process = None

    state = get_runtime_state()
    indexing = state.get("indexing") or {}
    if indexing.get("status") not in {"queued", "scanning", "indexing"}:
        return

    failure = {
        "source_filename": indexing.get("current_file") or "indexer",
        "document_id": indexing.get("current_document_id"),
        "project_id": indexing.get("current_project_id"),
        "error_code": "INDEXER_WORKER_EXITED",
        "reason": f"indexer worker exited with code {return_code}",
    }
    failures = list(indexing.get("failures") or [])
    failures.append(failure)
    failure_summary = dict(indexing.get("failure_summary") or {})
    failure_summary["INDEXER_WORKER_EXITED"] = failure_summary.get("INDEXER_WORKER_EXITED", 0) + 1
    update_indexing_status(
        status="failed",
        current_file=None,
        finished_at=time.time(),
        failures=failures,
        failure_summary=failure_summary,
    )


def _persisted_indexer_worker_is_active(state: dict | None = None) -> bool:
    state = state or get_runtime_state()
    indexing = state.get("indexing") or {}
    if str(indexing.get("status") or "") not in {"queued", "scanning", "indexing"}:
        return False
    try:
        pid = int(indexing.get("worker_pid"))
    except (TypeError, ValueError):
        return False
    expected_started_at = indexing.get("worker_started_at")
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running():
            return False
        zombie_status = getattr(psutil, "STATUS_ZOMBIE", "zombie")
        if process.status() == zombie_status:
            return False
        if expected_started_at is not None:
            actual_started_at = float(process.create_time())
            if abs(actual_started_at - float(expected_started_at)) > 1.0:
                return False
        command_line = " ".join(str(item) for item in (process.cmdline() or [])).lower()
        if "indexer_worker" not in command_line:
            return False
        workspace_path = str(state.get("workspace_path") or "").strip()
        if workspace_path and os.path.normcase(workspace_path) not in os.path.normcase(command_line):
            return False
        return True
    except Exception as exc:
        if exc.__class__.__name__ in {"NoSuchProcess", "ZombieProcess"}:
            return False
        # A persisted owner that cannot be verified must block a new worker;
        # otherwise a transient process-inspection failure can duplicate work.
        return True


def _indexer_worker_is_active() -> bool:
    with indexer_lock:
        process = indexer_process
        if process is not None:
            return process.poll() is None
    return _persisted_indexer_worker_is_active()


def effective_runtime_state():
    state = get_runtime_state()
    workspace = _workspace_from_state(state)
    if workspace is None:
        return state
    if state.get("locked") or not state.get("software_unlocked"):
        return state
    if not is_keystore_initialized(workspace):
        return state
    if get_workspace_keys(workspace.root_path) is not None:
        return state

    cleanup_runtime()
    return mark_software_locked()


def _workspace_from_state(state: dict | None = None):
    state = state or get_runtime_state()
    if state.get("workspace_path"):
        return SoulDriveWorkspace(state["workspace_path"]).ensure()
    if state.get("active_drive"):
        return resolve_workspace(state.get("active_drive"))
    return None


def _sync_index_run_receipt(state: dict | None = None):
    """Project the worker's durable terminal state into its request receipt."""
    state = state or get_runtime_state()
    indexing = state.get("indexing") or {}
    idempotency_key = str(indexing.get("idempotency_key") or "").strip()
    runtime_status = str(indexing.get("status") or "").strip()
    # The worker uses ``idle`` for a successful no-op scan.  The request
    # receipt has a terminal vocabulary, so project that state to completed
    # instead of leaving the idempotency key permanently in flight.
    status = (
        "completed"
        if runtime_status == "idle"
        else "failed"
        if runtime_status == "blocked"
        else runtime_status
    )
    if not idempotency_key or status not in {"queued", "scanning", "indexing", "completed", "failed"}:
        return
    workspace = _workspace_from_state(state)
    if workspace is None:
        return
    journal = None
    try:
        journal = IndexRunJournal(workspace)
        journal.update(
            idempotency_key,
            status=status,
            error=(
                str((indexing.get("failures") or [{}])[-1].get("reason") or "")
                if status == "failed" and indexing.get("failures")
                else "indexing blocked before worker execution"
                if runtime_status == "blocked"
                else None
            ),
        )
    except Exception:
        logger.warning("failed to sync index run idempotency receipt", exc_info=True)
    finally:
        if journal is not None:
            journal.close()


def current_learning_store() -> LearningStore:
    workspace = current_workspace()
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        raise RuntimeError("workspace keys are not available")
    return LearningStore(workspace, keys)


def _project_skill_path(workspace, project_id: str | None) -> Path | None:
    """Resolve the explicit project Skill directory without changing global Skills."""
    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id is None:
        return Path(workspace.skills_path).resolve()
    catalog = ProjectCatalog(workspace)
    try:
        row = catalog.conn.execute(
            "SELECT root_path FROM projects WHERE project_id = ? AND status = 'active'",
            (normalized_project_id,),
        ).fetchone()
        if row is None or not str(row["root_path"] or "").strip():
            return None
        return (Path(str(row["root_path"])).expanduser().resolve() / "skills").resolve()
    finally:
        catalog.close()


def _skill_scope_matches(skill: dict[str, Any], project_id: str | None, *, include_global: bool) -> bool:
    requested_project_id = str(project_id or "").strip() or None
    stored_project_id = str(skill.get("project_id") or "").strip() or None
    stored_scope = str(skill.get("scope") or ("project" if stored_project_id else "global")).strip()
    if requested_project_id:
        return (include_global and stored_scope == "global") or (
            stored_scope == "project" and stored_project_id == requested_project_id
        )
    return stored_scope == "global" and stored_project_id is None


def _list_scoped_skills(
    store,
    *,
    project_id: str | None = None,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """Keep legacy test/store adapters safe while the real store enforces scope in SQL."""
    try:
        return list(store.list_skills(project_id=project_id, include_global=include_global))
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return [
            skill for skill in store.list_skills()
            if _skill_scope_matches(skill, project_id, include_global=include_global)
        ]


def _archive_scoped_skill(store, skill_id: str, project_id: str | None) -> bool:
    try:
        return bool(store.archive_skill(skill_id, project_id=project_id, include_global=False))
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return bool(store.archive_skill(skill_id))


def sync_project_skills(
    store: LearningStore,
    workspace,
    *,
    project_id: str | None = None,
) -> dict[str, int]:
    """Import global and explicitly project-local Skills before any consumer reads them."""
    summary = {"imported": 0, "updated": 0, "unchanged": 0, "duplicates_removed": 0}
    roots: list[tuple[Path, str | None]] = [(Path(workspace.skills_path).resolve(), None)]
    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id:
        project_path = _project_skill_path(workspace, normalized_project_id)
        if project_path is not None and project_path != roots[0][0]:
            roots.append((project_path, normalized_project_id))

    for root, scope_project_id in roots:
        discovered = discover_project_skills(str(root), project_id=scope_project_id)
        duplicate_paths = set()
        for skill in discovered:
            duplicate_paths.update(
                (scope_project_id, path)
                for path in skill.pop("duplicate_source_paths", [])
            )
        for existing in _list_scoped_skills(
            store,
            project_id=scope_project_id,
            include_global=False,
        ):
            if (
                (scope_project_id, existing.get("source_path")) in duplicate_paths
                and _archive_scoped_skill(store, existing["skill_id"], scope_project_id)
            ):
                summary["duplicates_removed"] += 1
        for skill in discovered:
            summary[store.sync_project_skill(skill)] += 1
    return summary


def require_software_unlock():
    state = effective_runtime_state()
    if state.get("locked") or not state.get("software_unlocked"):
        _append_audit_best_effort(
            "security.password_required",
            {
                "reason": state.get("security_reason") or state.get("reason") or "workspace passphrase required",
            },
        )
        return JSONResponse(
            {
                "error": "SoulDrive workspace requires passphrase unlock",
                "status": "locked",
                "reason": "workspace passphrase required",
            },
            status_code=423,
        )
    return None

# =====================================================================
# --- 核心业务接口：UI 数据流通道 ---
# =====================================================================
class ChatHistoryMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|ai)$")
    content: str = Field(min_length=1, max_length=4000)
    timestamp_ms: int | None = Field(default=None, ge=0)


class ContextBoundingBox(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)


class ContextPoint(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class PerceptionEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    modality: str = Field(pattern="^(text|document|image|audio|ui|screen|ui_automation|ui_selection|ocr_region|image_region)$")
    timestamp_ms: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    start_ms: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    end_ms: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    event_time_origin_ms: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    project_id: str | None = Field(default=None, max_length=128)
    text: str | None = Field(default=None, max_length=2400)
    selected_text: str | None = Field(default=None, max_length=4000)
    source_name: str | None = Field(default=None, max_length=260)
    source: str | None = Field(default=None, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    page_number: int | None = Field(default=None, ge=1, le=100000)
    bbox: ContextBoundingBox | None = None
    focus_point: ContextPoint | None = None
    target_id: str | None = Field(default=None, max_length=128)
    target_kind: str | None = Field(default=None, max_length=64)
    window_id: str | None = Field(default=None, max_length=128)
    window_title: str | None = Field(default=None, max_length=260)
    is_focused: bool | None = None
    explicit: bool | None = None
    target_candidates: list[dict] = Field(default_factory=list, max_length=12)


class ActiveInteractionContext(BaseModel):
    project_id: str | None = Field(default=None, max_length=128)
    baseline_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    current_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    document_name: str | None = Field(default=None, max_length=260)
    document_names: list[str] = Field(default_factory=list, max_length=12)
    document_ids: list[str] = Field(default_factory=list, max_length=12)
    page_number: int | None = Field(default=None, ge=1, le=100000)
    selected_text: str | None = Field(default=None, max_length=4000)
    selected_region: ContextBoundingBox | None = None
    window_title: str | None = Field(default=None, max_length=260)
    captured_at_ms: int | None = Field(default=None, ge=0)
    skill_id: str | None = Field(default=None, max_length=128)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=DEFAULT_RAG_TOP_K, ge=1, le=20)
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,96}$")
    query_timestamp_ms: int | None = Field(default=None, ge=0)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=12)
    perception_events: list[PerceptionEvent] = Field(default_factory=list, max_length=24)
    active_context: ActiveInteractionContext | None = None


def _perception_event_scope_response(
    project_id: str | None,
    events: list[PerceptionEvent],
) -> JSONResponse | None:
    """Reject explicit perception context that crosses the bound project.

    Missing event scope remains compatible with older clients: the durable
    session/active context is still authoritative.  An explicit event or
    target candidate scope, however, must agree with that authority before
    the event can become visual evidence or enter idempotent chat handling.
    """
    bound_project_id = str(project_id or "").strip() or None
    if not bound_project_id:
        return None
    for event in events or []:
        event_project_id = str(event.project_id or "").strip() or None
        if event_project_id and event_project_id != bound_project_id:
            return JSONResponse(
                {
                    "error": "perception event project scope conflicts with bound project",
                    "event_id": event.event_id,
                    "project_id": event_project_id,
                    "bound_project_id": bound_project_id,
                },
                status_code=409,
            )
        for candidate in event.target_candidates or []:
            if not isinstance(candidate, dict):
                continue
            candidate_project_id = str(candidate.get("project_id") or "").strip() or None
            if candidate_project_id and candidate_project_id != bound_project_id:
                return JSONResponse(
                    {
                        "error": "perception target candidate project scope conflicts with bound project",
                        "event_id": event.event_id,
                        "project_id": candidate_project_id,
                        "bound_project_id": bound_project_id,
                    },
                    status_code=409,
                )
    return None


def _chat_input_digest(
    request: ChatRequest,
    *,
    project_id: str | None,
    workflow_id: str | None,
) -> str:
    """Bind a chat receipt to the effective request context, not only text."""
    payload = {
        "query": request.query,
        "top_k": request.top_k,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "query_timestamp_ms": request.query_timestamp_ms,
        # Server-loaded history changes after the first attempt persists the
        # turn.  Bind only explicitly supplied history so a legitimate retry
        # remains replayable while still distinguishing caller inputs.
        "history": [
            message.model_dump(exclude_none=True)
            for message in request.history
        ],
        "perception_events": [
            event.model_dump(exclude_none=True)
            for event in request.perception_events
        ],
        "active_context": (
            request.active_context.model_dump(exclude_none=True)
            if request.active_context
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reflection_draft_input_digest(
    trigger: str,
    messages: list[dict[str, Any]],
    project_id: str | None,
    session_id: str | None = None,
    workflow_id: str | None = None,
) -> str:
    payload = {
        "trigger": trigger,
        "project_id": project_id,
        "session_id": session_id,
        "workflow_id": workflow_id,
        "messages": messages,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_memory_review_key(session_id: str | None, request_id: str | None) -> str | None:
    """Bind the durable memory review to the same request identity as Chat."""
    session = str(session_id or "").strip()
    request = str(request_id or "").strip()
    if not session or not request:
        return None
    identity = f"{session}\x00{request}".encode("utf-8")
    return f"runtime-memory:{hashlib.sha256(identity).hexdigest()[:64]}"


def _runtime_memory_replay_matches_receipt(
    store: LearningStore,
    *,
    review_key: str | None,
    review_digest: str | None,
    memory_id: str,
    project_id: str | None,
) -> bool:
    """Verify a runtime receipt points to its own active, scoped memory result."""
    if not review_key or not review_digest or not memory_id:
        return False
    review = store.replay_reflection_draft(review_key, review_digest)
    if not isinstance(review, dict) or str(review.get("status") or "") != "completed":
        return False
    candidates = [
        item
        for item in review.get("candidates") or []
        if isinstance(item, dict)
        and str(item.get("asset_type") or "") == "memory"
        and str(item.get("status") or "") == "accepted"
    ]
    accepted_candidate_ids = {
        str(item.get("candidate_id") or "").strip()
        for item in candidates
        if str(item.get("candidate_id") or "").strip()
    }
    deduplicated_memory_ids = {
        str(item.get("deduplicated_memory_id") or "").strip()
        for item in candidates
        if str(item.get("deduplicated_memory_id") or "").strip()
    }
    if not accepted_candidate_ids and not deduplicated_memory_ids:
        return False

    for memory in store.list_memories(
        project_id=project_id,
        include_metadata=True,
        include_global=True,
    ):
        if str(memory.get("memory_id") or "").strip() != memory_id:
            continue
        scope = str(memory.get("scope") or "global").strip().lower() or "global"
        memory_project_id = str(memory.get("project_id") or "").strip() or None
        if scope == "global":
            scope_matches = memory_project_id is None
        else:
            scope_matches = scope == "project" and project_id is not None and memory_project_id == project_id
        if not scope_matches:
            continue
        source_candidate_id = str(memory.get("source_candidate_id") or "").strip()
        if source_candidate_id in accepted_candidate_ids or memory_id in deduplicated_memory_ids:
            return True
    return False


def _bind_reflection_source_messages(
    source_store: ProductionSessionStore,
    session_id: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind client-visible reflection messages to the authoritative session archive."""
    archive = getattr(source_store, "archive", None)
    recent = getattr(archive, "recent", None)
    if not callable(recent):
        raise ValueError("reflection source session history is unavailable")
    records = recent(session_id, limit=200)
    bindings: list[dict[str, Any]] = []
    cursor = 0
    for message in messages:
        target_role = str(message.get("role") or "").strip().lower()
        if target_role == "ai":
            target_role = "assistant"
        target_content = str(message.get("content") or "")
        match = None
        for index in range(cursor, len(records)):
            record = records[index]
            record_role = str(record.get("role") or "").strip().lower()
            if record_role == "ai":
                record_role = "assistant"
            if record_role == target_role and str(record.get("content") or "") == target_content:
                match = record
                cursor = index + 1
                break
        if match is None:
            raise ValueError("reflection messages do not match the source session")
        bindings.append(match)
    return bindings


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=DEFAULT_RAG_TOP_K, ge=1, le=20)
    project_id: str | None = Field(default=None, max_length=128)
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")


class RetrievalFeedbackRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    corrected_query: str = Field(min_length=1, max_length=6000)
    scope: str = Field(default="global", pattern=r"^(global|project)$")
    project_id: str | None = Field(default=None, max_length=128)
    workflow_id: str | None = Field(
        default=None,
        pattern=r"^(paper_review|experiment_compare|meeting_actions)$",
    )


class MultimodalDecisionRequest(BaseModel):
    speech_text: str = Field(min_length=1, max_length=6000)
    events: list[dict] = Field(default_factory=list, max_length=24)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    active_project_id: str | None = Field(default=None, max_length=128)
    target_required: bool = False


class MultimodalWorkflowPlanRequest(BaseModel):
    workflow_id: str = Field(pattern=r"^(paper_review|experiment_compare|meeting_actions)$")
    project_id: str | None = Field(default=None, max_length=128)
    document_names: list[str] = Field(default_factory=list, max_length=12)
    document_ids: list[str] = Field(default_factory=list, max_length=12)
    has_audio: bool = False
    has_image: bool = False
    speech_text: str = Field(default="", max_length=6000)
    text_note: str = Field(default="", max_length=6000)
    selected_text: str = Field(default="", max_length=4000)
    baseline_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    current_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")


class MultimodalWorkflowApprovalRequest(BaseModel):
    playbook_version: str = Field(min_length=1, max_length=32)
    approved: bool = True


class MultimodalRuntimeRequest(BaseModel):
    """Relative media references for one local, side-effect-free episode."""

    speech_text: str | None = Field(default=None, max_length=6000)
    audio_ref: str | None = Field(default=None, max_length=260)
    image_ref: str | None = Field(default=None, max_length=260)
    selected_region: ContextBoundingBox | None = None
    events: list[dict] = Field(default_factory=list, max_length=24)
    target_id: str | None = Field(default=None, max_length=128)
    project_id: str | None = Field(default=None, max_length=128)
    run_ocr: bool = True
    run_vlm: bool = False
    target_grounding: bool = False
    visual_mode: str | None = Field(default=None, pattern="^(auto|fast|slow)$")
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    event_time_origin_ms: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    audio_start_ms: int = Field(default=0, ge=0, le=86_400_000)
    image_start_ms: int = Field(default=0, ge=0, le=86_400_000)
    vlm_prompt: str = Field(
        default="请用一句话说明选区中的界面内容，并指出与当前任务相关的可见信息。",
        max_length=600,
    )
    max_new_tokens: int = Field(default=DEFAULT_MAX_NEW_TOKENS, ge=16, le=256)


def _parse_runtime_events(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("events_json must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("events_json must be a JSON array")
    return [item for item in value[:24] if isinstance(item, dict)]


def _parse_runtime_region(raw: str | None) -> dict[str, float] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("selected_region_json must be valid JSON") from exc
    try:
        region = ContextBoundingBox.model_validate(value)
    except Exception as exc:
        raise ValueError("selected_region_json must contain x, y, width, and height in [0, 1]") from exc
    return region.model_dump()


class ReflectionMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=12000)


class ReflectionDraftRequest(BaseModel):
    trigger: str = Field(pattern="^(manual|close)$")
    messages: list[ReflectionMessage] = Field(default_factory=list, max_length=200)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,96}$")


class ReflectionDecision(BaseModel):
    candidate_id: str = Field(max_length=128)
    action: str = Field(pattern="^(accept|reject)$")
    edited_title: str | None = Field(default=None, max_length=240)
    edited_content: str | None = Field(default=None, max_length=4000)
    valid_from: float | None = Field(default=None, ge=0)
    invalid_at: float | None = Field(default=None, ge=0)


class ReflectionDecideRequest(BaseModel):
    decisions: list[ReflectionDecision] = Field(default_factory=list, max_length=100)
    project_id: str | None = Field(default=None, max_length=128)


class RuntimeMemoryRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    project_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")


class LearningMemoryUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=4000)
    expected_updated_at: float | None = Field(default=None, ge=0)
    valid_from: float | None = Field(default=None, ge=0)
    invalid_at: float | None = Field(default=None, ge=0)


class LearningSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k_memory: int = Field(default=3, ge=0, le=10)
    top_k_skill: int = Field(default=2, ge=0, le=10)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    as_of: float | None = Field(default=None, ge=0)
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")


class SkillMatchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=3, ge=1, le=10)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")


class SkillRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    matched_score: float = Field(default=0.0, ge=0.0, le=1.0)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")
    inputs: dict = Field(default_factory=dict)
    mode: str = Field(default="plan_only", pattern=r"^(plan_only|execute)$")
    script_path: str | None = Field(default=None, max_length=260)
    args: list[str] = Field(default_factory=list, max_length=12)
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=120.0)


class SkillInstallRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    skill_name: str | None = Field(default=None, max_length=64)
    target: str = Field(default="conversation", pattern=r"^(conversation|presentation)$")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")


class RuntimeRequest(BaseModel):
    reason: str = Field(default="manual runtime update", max_length=200)
    auth_level: str = Field(default="DEV", max_length=16)
    hardware_sn: str | None = Field(default=None, max_length=256)
    active_drive: str | None = Field(default=None, max_length=260)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    project_mode: str = Field(default="research", pattern=r"^(research|business)$")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    root_path: str | None = Field(default=None, max_length=520)
    instructions: str = Field(default="", max_length=8000)
    memory_policy: str = Field(default="project_plus_global", pattern=r"^(project_plus_global|project_only)$")


class ExperimentMetricImportRequest(BaseModel):
    source_name: str = Field(min_length=1, max_length=260)
    source_document_id: str | None = Field(default=None, max_length=160)
    source_version_id: str | None = Field(default=None, max_length=160)
    experiment_name: str = Field(min_length=1, max_length=160)
    performed_at: str | None = Field(default=None, max_length=64)
    dataset_version: str = Field(default="", max_length=160)
    config_ref: str = Field(default="", max_length=260)
    evaluation_script_ref: str = Field(default="", max_length=260)
    environment_ref: str = Field(default="", max_length=260)
    unit: str = Field(default="", max_length=64)
    direction: str = Field(default="", max_length=32)
    definition_version: str = Field(default="", max_length=128)
    split: str = Field(default="", max_length=64)
    confirmed: bool = False


class ExperimentCompareRequest(BaseModel):
    baseline_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    current_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    reference_at: str | None = Field(default=None, max_length=64)
    timezone_name: str = Field(default="Asia/Shanghai", max_length=64)
    experiment_name: str | None = Field(default=None, max_length=160)


class ExperimentRepeatSummaryRequest(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=32)


class SessionCreateRequest(BaseModel):
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    title: str = Field(min_length=1, max_length=120)
    workflow_id: str | None = Field(default=None, pattern=r"^(paper_review|experiment_compare|meeting_actions)$")

class PaperImportRequest(BaseModel):
    source_paths: list[str] = Field(min_length=1, max_length=100)
    project_id: str | None = Field(default=None, max_length=128)


class DocumentGlobalizeRequest(BaseModel):
    name: str | None = Field(default=None, max_length=260)
    document_id: str | None = Field(default=None, max_length=256)


class PresentationGenerateRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=128)
    source_filenames: list[str] = Field(default_factory=list, max_length=12)
    source_document_ids: list[str] = Field(default_factory=list, max_length=12)
    project_id: str | None = Field(default=None, max_length=128)
    source_version_ids: list[str] = Field(default_factory=list, max_length=64)
    baseline_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    current_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    title: str = Field(default="", max_length=120)
    audience: str = Field(default="research", pattern="^(research|technical|government)$")
    max_slides: int = Field(default=10, ge=4, le=16)
    open_in_wps: bool = True
    style_skill_id: str | None = Field(default=None, max_length=128)


class PresentationConfirmRequest(BaseModel):
    plan_digest: str = Field(min_length=16, max_length=128)


class ContactCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    project_ids: list[str] = Field(default_factory=list, max_length=24)


class ContactResolveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=240)
    project_id: str | None = Field(default=None, max_length=128)


class EmailDraftRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=128)
    recipient_query: str = Field(min_length=1, max_length=240)
    subject: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=12000)
    source_version_ids: list[str] = Field(default_factory=list, max_length=64)
    attachment_refs: list[str] = Field(default_factory=list, max_length=12)


class ScheduleDraftRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    start_at: str = Field(min_length=1, max_length=64)
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    participant_query: str | None = Field(default=None, max_length=240)
    notes: str = Field(default="", max_length=12000)
    reminder_sound_enabled: bool = True
    source_version_ids: list[str] = Field(default_factory=list, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=128)


class ScheduleDraftUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    start_at: str = Field(min_length=1, max_length=64)
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    participant_query: str | None = Field(default=None, max_length=240)
    notes: str = Field(default="", max_length=12000)
    reminder_sound_enabled: bool = True


class ArtifactStatusRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|cancelled)$")


class EmailSmtpConfigRequest(BaseModel):
    provider: str = Field(pattern="^(qq|netease163)$")
    sender: str = Field(min_length=3, max_length=320)
    authorization_code: str = Field(min_length=4, max_length=128)


class CloudModelConfigRequest(BaseModel):
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=240)
    enabled: bool = False
    models: list[str] = Field(default_factory=list, max_length=100)


class CloudModelModelsRequest(BaseModel):
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=512)


class EmailSendRequest(BaseModel):
    sender: str = Field(min_length=3, max_length=320)
    idempotency_key: str = Field(min_length=16, max_length=128)

class SecurityInitRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=256)
    acknowledge_no_recovery: bool = False

class SecurityUnlockRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=256)


class DocumentDeleteAuthorizationRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=256)


class DocumentDeleteRequest(BaseModel):
    name: str | None = Field(default=None, max_length=260)
    document_id: str | None = Field(default=None, max_length=256)
    authorization: str = Field(min_length=32, max_length=128)


class DocumentDeleteAuthorizationRevokeRequest(BaseModel):
    authorization: str = Field(min_length=32, max_length=128)

class ToolCallRequest(BaseModel):
    name: str = Field(max_length=128)
    arguments: dict = Field(default_factory=dict)


async def _call_shutdown_handler(handler):
    result = handler("api shutdown requested")
    if inspect.isawaitable(result):
        await result


def _chat_workflow_input_plan(
    request: ChatRequest,
    workflow_id: str,
    project_id: str | None,
    workspace: SoulDriveWorkspace | Any,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the same minimum-input contract to every chat entry point.

    The UI performs this preflight before media requests, but plain-text chat
    can also be called directly (or through an older client).  Keep the gate
    deterministic and side-effect free so a role-bound session cannot silently
    fall back to generic RAG when its evidence contract is not satisfied.
    """
    active = request.active_context
    document_names = list(active.document_names if active else [])
    document_ids = list(active.document_ids if active else [])
    active_document_name = str(active.document_name or "").strip() if active else ""
    if active_document_name and active_document_name not in document_names:
        document_names.append(active_document_name)
    events = request.perception_events or []
    modalities = {
        str(event.modality or "").strip().lower()
        for event in events
    }
    selected_text = str(active.selected_text or "").strip() if active else ""
    if not selected_text:
        selected_text = next(
            (
                str(event.selected_text or "").strip()
                for event in events
                if str(event.selected_text or "").strip()
            ),
            "",
        )
    plan = plan_workflow(
        workflow_id,
        project_id=project_id,
        document_names=document_names,
        document_ids=document_ids,
        has_audio="audio" in modalities,
        has_image=bool(active and active.selected_region) or bool(modalities & VISUAL_EVENT_MODALITIES),
        text_note=request.query if workflow_id == "meeting_actions" else "",
        selected_text=selected_text,
        baseline_run_id=active.baseline_run_id if active else None,
        current_run_id=active.current_run_id if active else None,
        approval=approval,
        require_approval=True,
    )
    return _validate_workflow_document_inputs(
        plan,
        workspace=workspace,
        project_id=project_id,
        document_names=document_names,
        document_ids=document_ids,
    )


def _validate_workflow_document_inputs(
    plan: dict[str, Any],
    *,
    workspace: SoulDriveWorkspace | Any,
    project_id: str | None,
    document_names: list[str] | None,
    document_ids: list[str] | None = None,
    allow_global_scope: bool = False,
    allow_label_broadcast: bool = False,
) -> dict[str, Any]:
    """Verify selected workflow documents against the local indexed library.

    ``plan_workflow`` remains a deterministic, side-effect-free contract helper
    and therefore only sees the caller's input shape.  The server boundary must
    additionally verify that each selected source is indexed and either global
    or bound to the active project; otherwise an old client can claim a filename
    that the RAG scope silently cannot read.
    """
    names = [
        " ".join(str(item or "").strip().split())[:260]
        for item in (document_names or [])
        if " ".join(str(item or "").strip().split())
    ]
    ids = []
    for item in document_ids or []:
        normalized_id = " ".join(str(item or "").strip().split())[:256]
        if normalized_id and normalized_id not in ids:
            ids.append(normalized_id)
        if len(ids) >= 12:
            break
    if not names and not ids:
        return plan
    if not str(project_id or "").strip() and not allow_global_scope:
        return plan

    # Unit-test doubles do not represent a mounted workspace.  The real server
    # always supplies SoulDriveWorkspace here; keep the pure plan tests usable.
    if not getattr(workspace, "root_path", None):
        return plan

    try:
        library = _document_library_payload(workspace)
    except Exception:
        logger.exception("failed to verify workflow document scope")
        issues = ["无法核验所选文档的本地索引和项目归属，请刷新文档库后重试"]
    else:
        library_documents = [
            item for item in (library.get("documents") or [])
            if str(item.get("name") or "").strip()
        ]
        entries_by_name: dict[str, list[dict[str, Any]]] = {}
        for item in library_documents:
            raw_name = str(item.get("name") or "").strip()
            name_keys = {
                raw_name.casefold(),
                Path(raw_name).name.casefold(),
            }
            for name_key in name_keys:
                if name_key:
                    entries_by_name.setdefault(name_key, []).append(item)
        entries_by_id = {
            candidate_id: item
            for item in library_documents
            for candidate_id in _document_id_candidates(item.get("document_id"))
            if candidate_id
        }
        active_project_id = str(project_id).strip()
        issues = []
        selected_items = []
        if ids:
            for document_id in ids:
                item = next(
                    (
                        entries_by_id.get(candidate_id)
                        for candidate_id in _document_id_candidates(document_id)
                        if entries_by_id.get(candidate_id) is not None
                    ),
                    None,
                )
                if item is None:
                    issues.append(f"文档 ID“{document_id}”不在本地文档库中，请刷新后重新选择")
                    continue
                selected_items.append((document_id, item))
            if names:
                if len(names) != len(ids):
                    if allow_label_broadcast and len(names) == 1 and len(selected_items) == len(ids):
                        expected_name = Path(names[0]).name.casefold()
                        actual_names = {
                            Path(str(item.get("name") or "")).name.casefold()
                            for _, item in selected_items
                        }
                        if actual_names != {expected_name}:
                            issues.append(
                                f"文档显示标签无法对应所选对象：应为“{names[0]}”，实际为“{'、'.join(sorted(actual_names))}”"
                            )
                    else:
                        issues.append("所选文档名称与文档 ID 数量不一致，无法确认对象对应关系")
                else:
                    for name, document_id in zip(names, ids):
                        item = next(
                            (
                                entries_by_id.get(candidate_id)
                                for candidate_id in _document_id_candidates(document_id)
                                if entries_by_id.get(candidate_id) is not None
                            ),
                            None,
                        )
                        if item is None:
                            continue
                        if Path(name).name.casefold() != Path(str(item.get("name") or "")).name.casefold():
                            issues.append(
                                f"文档 ID 与名称不匹配：{document_id} 对应“{item.get('name') or '未命名文档'}”，而不是“{name}”"
                            )
        else:
            for name in dict.fromkeys(names):
                matches = []
                seen_item_ids: set[int] = set()
                for name_key in (Path(name).name.casefold(), name.casefold()):
                    for item in entries_by_name.get(name_key, []):
                        if id(item) in seen_item_ids:
                            continue
                        seen_item_ids.add(id(item))
                        matches.append(item)
                if len(matches) > 1:
                    issues.append(f"文档“{name}”同名文档不唯一，请使用文档 ID 后重试")
                    continue
                selected_items.append((name, matches[0] if matches else None))
        for selector, item in selected_items:
            if item is None:
                issues.append(f"文档“{selector}”不在本地文档库中，请重新选择")
                continue
            if not bool(item.get("indexed")) or str(item.get("index_status") or "") != "indexed":
                status = str(item.get("index_status") or "pending")
                issues.append(f"文档“{item.get('name') or selector}”尚未完成索引（当前状态：{status}）")
                continue
            project_ids = {
                str(value).strip()
                for value in (item.get("project_ids") or [])
                if str(value).strip()
            }
            if str(item.get("scope") or "") != "global" and (
                not active_project_id or active_project_id not in project_ids
            ):
                issues.append(f"文档“{item.get('name') or selector}”不属于当前项目，不能作为本轮证据")

    if issues:
        plan["ready"] = False
        plan["missing_inputs"] = list(dict.fromkeys([
            *(plan.get("missing_inputs") or []),
            *issues,
        ]))
        plan["next_action"] = "；".join(plan["missing_inputs"])
    return plan


@app.post("/chat")
async def chat_stream(
    request: ChatRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=96),
):
    """
    前端问答主接口。
    接收自然语言提问 -> 执行 RAG 流水线 -> 以 SSE (Server-Sent Events) 流式返回。
    """
    header_request_id = str(idempotency_key or "").strip()
    body_request_id = str(request.request_id or "").strip()
    if header_request_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", header_request_id):
        return JSONResponse(
            {"error": "Idempotency-Key 格式无效"},
            status_code=422,
        )
    if header_request_id and body_request_id and header_request_id != body_request_id:
        return JSONResponse(
            {"error": "Idempotency-Key 与 request_id 不一致"},
            status_code=409,
        )
    if header_request_id and not body_request_id:
        # Keep one effective key throughout the existing request-receipt,
        # message-archive and completed-answer replay path.
        request.request_id = header_request_id
    if request.request_id and not request.session_id:
        return JSONResponse(
            {"error": "session_id is required when request_id is provided"},
            status_code=422,
        )
    _reconcile_indexer_state()
    trace_id = str(uuid.uuid4())
    runtime_metrics.increment("chat_requests")
    indexing_busy = str((get_runtime_state().get("indexing") or {}).get("status") or "idle") in {"queued", "scanning", "indexing"}
    history = [message.model_dump(exclude_none=True) for message in request.history]
    conversation_store = None
    storage_context: dict[str, Any] = {}
    try:
        workspace = current_workspace()
    except RuntimeError as exc:
        # Keep the streaming contract for transient startup/indexer states;
        # do not let workspace discovery turn an observable status into HTTP 500.
        _append_audit_best_effort(
            "chat.workspace_unavailable",
            {"indexing_busy": indexing_busy, **_error_observation(exc)},
            trace_id=trace_id,
        )
        message = (
            "知识索引正在更新，请等待索引完成后再提问。"
            if indexing_busy
            else "本地工作区暂不可用，请检查工作区状态后重试。"
        )
        return StreamingResponse(iter([message]), media_type="text/event-stream")
    project_id = (
        str(request.active_context.project_id or "").strip()
        if request.active_context
        else ""
    ) or None
    session_workflow_id = None
    # A session is the durable source of its project binding.  The UI may omit
    # active_context.project_id for an ordinary new conversation; that must not
    # turn a valid project session into a cross-project error or bypass RAG.
    # Validate the durable session before the request can enter the indexing
    # wait path.  If indexing finishes while this request is being streamed,
    # skipping this lookup would let client-supplied project/workflow fields
    # replace the session's persisted binding.
    if request.session_id:
        try:
            conversation_store = _conversation_store(workspace)
        except Exception:
            # Session binding is a security boundary.  A storage error must
            # not silently fall back to client-provided project/workflow data.
            logger.exception("failed to load session metadata")
            return JSONResponse({"error": "session context unavailable"}, status_code=503)
        if conversation_store is None:
            return JSONResponse({"error": "session context unavailable"}, status_code=503)
        try:
            session = conversation_store.session_metadata(request.session_id)
        except Exception:
            logger.exception("failed to load session metadata")
            return JSONResponse({"error": "session context unavailable"}, status_code=503)
        if session is None:
            return JSONResponse({"error": "session does not exist; create it before chatting"}, status_code=404)
        session_workflow_id = str(session.get("workflow_id") or "").strip() or None
        session_project_id = (
            str(session.get("project_id") or "").strip()
            or None
        )
        if project_id and session_project_id != project_id:
            return JSONResponse({"error": "session belongs to another project"}, status_code=409)
        project_id = project_id or session_project_id
        requested_workflow_id = str(request.workflow_id or "").strip() or None
        if (
            session_workflow_id
            and requested_workflow_id
            and session_workflow_id != requested_workflow_id
        ):
            return JSONResponse(
                {
                    "error": (
                        "session is bound to workflow "
                        f"{session_workflow_id}; create or select another session"
                    )
                },
                status_code=409,
            )
        if requested_workflow_id and not session_workflow_id:
            return JSONResponse(
                {
                    "error": (
                        "session is bound to the generic workflow; create or select a session "
                        f"bound to {requested_workflow_id}"
                    )
                },
                status_code=409,
            )
        if not history:
            try:
                storage_context = conversation_store.load_context(request.session_id, limit=8)
                history = storage_context.get("history") or []
            except Exception:
                # Preserve the existing recoverable-history behavior: the
                # durable session binding is known, but an old archive
                # record may be unreadable, so the request can still use
                # its explicitly supplied history.
                logger.exception("failed to load conversation history")
    perception_scope_response = _perception_event_scope_response(
        project_id,
        request.perception_events,
    )
    if perception_scope_response is not None:
        return perception_scope_response
    workflow_id = session_workflow_id or request.workflow_id
    chat_input_digest = _chat_input_digest(
        request,
        project_id=project_id,
        workflow_id=workflow_id,
    )
    chat_request_id = request.request_id or trace_id
    # A chat retry can re-enter memory/Skill context construction after the
    # request receipt has been released by a failed attempt.  Bind the
    # automatically created plan to the same request key so the durable run
    # is reused instead of appending a second planned record.
    skill_run_idempotency_key = f"chat-skill:{chat_request_id}"
    idempotent_replay: dict[str, Any] | None = None
    request_receipt: dict[str, Any] | None = None
    if request.request_id and request.session_id and conversation_store:
        lookup = getattr(conversation_store, "find_message_by_request_id", None)
        if callable(lookup):
            try:
                existing_user = lookup(
                    request.session_id,
                    request.request_id,
                    role="user",
                )
                if existing_user and str(existing_user.get("content") or "") != request.query:
                    return JSONResponse(
                        {"error": "request_id 已用于其他问题，请更换 request_id"},
                        status_code=409,
                    )
            except Exception:
                logger.warning("chat idempotency lookup degraded", exc_info=True)
    # Keep the user's question as the RAG query.  The selected Playbook is a
    # structured session contract carried in ``interaction_context``; mixing
    # its title/instructions into the retrieval text makes document ranking
    # depend on control prose rather than the user's evidence request.
    rag_query = request.query
    requested_baseline_run_id = (
        str(request.active_context.baseline_run_id or "").strip()
        if request.active_context
        else ""
    )
    requested_current_run_id = (
        str(request.active_context.current_run_id or "").strip()
        if request.active_context
        else ""
    )
    if (
        (requested_baseline_run_id or requested_current_run_id)
        and workflow_id != "experiment_compare"
    ):
        return JSONResponse(
            {
                "error": "实验基线/当前运行只能在 experiment_compare 会话中使用；请切换或新建实验对比会话",
            },
            status_code=409,
        )
    project_mode = _project_mode_for_id(workspace, project_id) if project_id else None
    experiment_comparison = None
    has_explicit_experiment_selection = bool(
        request.active_context
        and (request.active_context.baseline_run_id or request.active_context.current_run_id)
    )
    if has_explicit_experiment_selection:
        locked_response = require_software_unlock()
        if locked_response is not None:
            return locked_response
    try:
        if has_explicit_experiment_selection:
            experiment_comparison = _load_explicit_experiment_comparison(
                workspace,
                project_id,
                request.active_context,
            )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    if experiment_comparison is not None and experiment_comparison.get("status") != "comparable":
        _append_audit_best_effort(
            "chat.experiment_comparison_blocked",
            {
                "project_id": project_id,
                "baseline_run_id": request.active_context.baseline_run_id if request.active_context else None,
                "current_run_id": request.active_context.current_run_id if request.active_context else None,
                "status": experiment_comparison.get("status"),
                "side_effect": "none",
            },
            trace_id=trace_id,
        )
        return JSONResponse(
            {
                "error": "experiment comparison is not ready",
                "experiment_comparison": experiment_comparison,
            },
            status_code=422,
        )
    workflow_plan = None
    if workflow_id and idempotent_replay is None:
        try:
            workflow_plan = _chat_workflow_input_plan(
                request,
                workflow_id,
                project_id,
                workspace,
                _playbook_approval_for_project(workspace, project_id, workflow_id)
                if project_id
                else None,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not workflow_plan["ready"]:
            _append_audit_best_effort(
                "chat.workflow_inputs_blocked",
                {
                    "workflow_id": workflow_id,
                    "project_id": project_id,
                    "missing_inputs": workflow_plan["missing_inputs"],
                    "side_effect": "none",
                },
                trace_id=trace_id,
            )
            return JSONResponse(
                {
                    "error": "当前会话业务模块的输入尚未就绪",
                    "workflow_plan": workflow_plan,
                },
                status_code=422,
            )
    if not workflow_id and request.active_context:
        active_document_names = list(request.active_context.document_names or [])
        active_document_name = str(request.active_context.document_name or "").strip()
        if active_document_name and active_document_name not in active_document_names:
            active_document_names.append(active_document_name)
        active_document_ids = list(request.active_context.document_ids or [])
        if active_document_names or active_document_ids:
            document_context = _validate_workflow_document_inputs(
                {"ready": True, "missing_inputs": []},
                workspace=workspace,
                project_id=project_id,
                document_names=active_document_names,
                document_ids=active_document_ids,
                allow_global_scope=True,
                allow_label_broadcast=True,
            )
            if not document_context["ready"]:
                return JSONResponse(
                    {
                        "error": "当前文档选择无法作为本轮证据",
                        "document_context": {
                            "ready": False,
                            "issues": list(document_context.get("missing_inputs") or []),
                            "next_action": document_context.get("next_action") or "请重新选择文档",
                        },
                    },
                    status_code=409,
                )
    session_summary = str((storage_context.get("summary") or {}).get("summary") or "")
    active_context_payload = (
        request.active_context.model_dump(exclude_none=True)
        if request.active_context
        else {}
    )
    if project_id and not active_context_payload.get("project_id"):
        active_context_payload["project_id"] = project_id
    interaction_context = normalize_interaction_context(
        {
            "session_id": request.session_id,
            "query_timestamp_ms": (
                request.query_timestamp_ms
                if request.query_timestamp_ms is not None
                else int(time.time() * 1000)
            ),
            "history": history,
            "session_summary": session_summary,
            "events": [event.model_dump(exclude_none=True) for event in request.perception_events],
            "active_context": active_context_payload or None,
            "experiment_comparison": experiment_comparison,
            "project_mode": project_mode,
            "workflow_id": workflow_id,
            "workflow_user_query": request.query if workflow_id == "meeting_actions" else None,
        }
    )
    interaction_summary = summarize_interaction_context(interaction_context)

    # A completed assistant message is replayable, but it is too late to use
    # a message lookup as the only guard: two first requests can both observe
    # no message before either generator persists one.  Claim a short-lived
    # local receipt before entering model generation when the store supports it.
    if request.request_id and request.session_id and conversation_store:
        claim = getattr(conversation_store, "claim_request", None)
        if callable(claim):
            try:
                receipt = claim(
                    request.session_id,
                    request.request_id,
                    request.query,
                    owner_id=trace_id,
                    input_digest=chat_input_digest,
                )
            except Exception:
                logger.exception("chat request receipt claim failed")
                return JSONResponse({"error": "chat request receipt unavailable"}, status_code=503)
            receipt_status = str(receipt.get("status") or "")
            if receipt_status == "conflict":
                return JSONResponse(
                    {"error": "request_id 已用于其他问题，请更换 request_id"},
                    status_code=409,
                )
            if receipt_status == "in_flight" and idempotent_replay is None:
                retry_after_ms = max(0, int(receipt.get("retry_after_ms") or 0))
                response = JSONResponse(
                    {
                        "error": "request is already in flight; retry with the same request_id",
                        "request_id": request.request_id,
                        "retry_after_ms": retry_after_ms,
                    },
                    status_code=409,
                )
                if retry_after_ms:
                    response.headers["Retry-After"] = str(max(1, (retry_after_ms + 999) // 1000))
                return response
            if receipt_status == "completed":
                response_message = None
                response_message_id = str(receipt.get("response_message_id") or "").strip()
                receipt_input_digest = str(receipt.get("input_digest") or "").strip()
                find_message = getattr(conversation_store, "find_message", None)
                if response_message_id and callable(find_message):
                    response_message = find_message(response_message_id)
                if isinstance(response_message, dict):
                    response_metadata = response_message.get("metadata")
                    if not isinstance(response_metadata, dict):
                        response_metadata = {}
                    if not (
                        str(response_message.get("session_id") or "") == str(request.session_id or "")
                        and str(response_message.get("request_id") or "") == str(request.request_id or "")
                        and str(response_message.get("role") or "").strip().lower() == "assistant"
                        and str(response_metadata.get("status") or "").strip().lower() == "completed"
                        and (
                            not receipt_input_digest
                            or hmac.compare_digest(
                                str(response_metadata.get("input_digest") or "").strip(),
                                receipt_input_digest,
                            )
                        )
                    ):
                        response_message = None
                if response_message is None and callable(lookup):
                    # Older completed receipts may not have stored the exact
                    # assistant message ID.  The receipt has already matched
                    # the full input digest, so a bounded request lookup is
                    # safe as a compatibility fallback.
                    fallback_message = lookup(
                        request.session_id,
                        request.request_id,
                        role="assistant",
                        status="completed",
                    )
                    if isinstance(fallback_message, dict):
                        fallback_metadata = fallback_message.get("metadata")
                        if not isinstance(fallback_metadata, dict):
                            fallback_metadata = {}
                        if (
                            str(fallback_message.get("session_id") or "") == str(request.session_id or "")
                            and str(fallback_message.get("request_id") or "") == str(request.request_id or "")
                            and str(fallback_message.get("role") or "").strip().lower() == "assistant"
                            and str(fallback_metadata.get("status") or "").strip().lower() == "completed"
                            and (
                                not receipt_input_digest
                                or hmac.compare_digest(
                                    str(fallback_metadata.get("input_digest") or "").strip(),
                                    receipt_input_digest,
                                )
                            )
                        ):
                            response_message = fallback_message
                if not response_message:
                    return JSONResponse(
                        {"error": "request completed but its response archive is unavailable"},
                        status_code=503,
                    )
                idempotent_replay = response_message
            if receipt_status == "claimed":
                request_receipt = receipt
                if callable(lookup):
                    legacy_response = lookup(
                        request.session_id,
                        request.request_id,
                        role="assistant",
                        status="completed",
                    )
                    if legacy_response:
                        stored_digest = str(
                            (legacy_response.get("metadata") or {}).get("input_digest") or ""
                        ).strip()
                        if stored_digest == chat_input_digest:
                            # A receipt can be lost while the archive write
                            # survives.  Only replay that response when the
                            # archive itself carries the same full-input
                            # binding; legacy query-only records fail closed.
                            idempotent_replay = legacy_response
                        else:
                            try:
                                conversation_store.fail_request(
                                    request.session_id,
                                    chat_request_id,
                                    owner_id=trace_id,
                                    lease_token=str(receipt.get("lease_token") or ""),
                                    error="legacy response is not bound to the full chat input",
                                )
                            except Exception:
                                logger.warning("failed to release unbound legacy chat receipt", exc_info=True)
                            return JSONResponse(
                                {
                                    "error": (
                                        "历史回答缺少完整输入摘要，无法安全重放；"
                                        "请更换 request_id 后重试"
                                    )
                                },
                                status_code=409,
                            )

    _append_audit_best_effort(
        "chat.requested",
        {
            "query_chars": len(request.query),
            "top_k": clamp_chat_top_k(request.top_k),
            "workflow_id": workflow_id,
            "project_mode": project_mode,
            "interaction_context": interaction_summary,
        },
        trace_id=trace_id,
    )

    # 工业标准的流式返回闭包
    def event_generator():
        chat_started_at = time.perf_counter()
        first_token_ms = None
        success = False
        output = []
        retrieval_trace: dict[str, Any] = {}
        compaction_status = "not_attempted"
        learning_context = None
        _reconcile_indexer_state()
        indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
        last_lease_renewal = 0.0
        request_receipt_completed: bool | None = None

        def fail_request_receipt(reason: str) -> None:
            if not request_receipt or not conversation_store or not request.session_id:
                return
            fail = getattr(conversation_store, "fail_request", None)
            if not callable(fail):
                return
            try:
                fail(
                    request.session_id,
                    chat_request_id,
                    owner_id=trace_id,
                    lease_token=str(request_receipt.get("lease_token") or ""),
                    error=reason,
                )
            except Exception:
                logger.warning("failed to release chat request receipt", exc_info=True)

        def complete_request_receipt(response_message_id: str | None = None) -> bool:
            nonlocal request_receipt_completed
            if not request_receipt or not conversation_store or not request.session_id:
                return True
            complete = getattr(conversation_store, "complete_request", None)
            if not callable(complete):
                request_receipt_completed = False
                _append_audit_best_effort(
                    "chat.request_receipt_completion_lost",
                    {
                        "request_id": chat_request_id,
                        "response_message_id": response_message_id,
                        "reason": "completion_handler_unavailable",
                    },
                    trace_id=trace_id,
                )
                return False
            try:
                completed = complete(
                    request.session_id,
                    chat_request_id,
                    owner_id=trace_id,
                    lease_token=str(request_receipt.get("lease_token") or ""),
                    response_message_id=response_message_id,
                )
            except Exception:
                logger.warning("failed to complete chat request receipt", exc_info=True)
                request_receipt_completed = False
                _append_audit_best_effort(
                    "chat.request_receipt_completion_lost",
                    {
                        "request_id": chat_request_id,
                        "response_message_id": response_message_id,
                        "reason": "completion_handler_error",
                    },
                    trace_id=trace_id,
                )
                return False
            request_receipt_completed = bool(completed)
            if not request_receipt_completed:
                _append_audit_best_effort(
                    "chat.request_receipt_completion_lost",
                    {
                        "request_id": chat_request_id,
                        "response_message_id": response_message_id,
                        "reason": "lease_fenced_or_receipt_missing",
                    },
                    trace_id=trace_id,
                )
            return request_receipt_completed

        def renew_request_receipt(*, force: bool = False) -> None:
            nonlocal last_lease_renewal
            if not request_receipt or not conversation_store or not request.session_id:
                return
            now = time.monotonic()
            if not force and now - last_lease_renewal < 15.0:
                return
            renew = getattr(conversation_store, "renew_request", None)
            if not callable(renew):
                return
            try:
                renewed = renew(
                    request.session_id,
                    chat_request_id,
                    owner_id=trace_id,
                    lease_token=str(request_receipt.get("lease_token") or ""),
                )
            except Exception:
                logger.warning("failed to renew chat request receipt", exc_info=True)
                raise RuntimeError("chat request receipt renewal failed")
            if not renewed:
                raise RuntimeError("chat request lease was lost")
            last_lease_renewal = now

        locked_response = require_software_unlock()
        if locked_response is not None:
            cleanup_runtime()
            state = get_runtime_state()
            _append_audit_best_effort(
                "chat.rejected_locked",
                {"reason": state.get("reason", "storage device removed")},
                trace_id=trace_id,
            )
            fail_request_receipt("workspace locked before chat started")
            yield (
                f"本地知识引擎已锁定：{state.get('reason', 'workspace locked')}。"
                "请解锁当前私有工作区；若使用便携模式，请确认工作区存储已连接。"
            )
            return

        try:
            renew_request_receipt(force=True)
            if idempotent_replay is not None:
                cached_content = str(idempotent_replay.get("content") or "")
                if cached_content:
                    receipt_completed = complete_request_receipt(
                        str(idempotent_replay.get("message_id") or "") or None
                    )
                    _append_audit_best_effort(
                        "chat.replayed_idempotent",
                        {
                            "request_id": chat_request_id,
                            "status": "completed" if receipt_completed else "response_replayed_receipt_pending",
                        },
                        trace_id=trace_id,
                    )
                    output.append(cached_content)
                    success = True
                    yield cached_content
                    return
            if indexing_status in {"queued", "scanning", "indexing"}:
                fail_request_receipt("indexing in progress")
                yield "知识索引正在更新。为避免回答使用旧知识库，请等待索引完成后再提问。"
                return
            request_id = chat_request_id
            if conversation_store and request.session_id:
                conversation_store.append_message(
                    request.session_id,
                    "user",
                    request.query,
                    metadata={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "query_timestamp_ms": request.query_timestamp_ms,
                        "project_id": project_id,
                        "input_digest": chat_input_digest,
                    },
                    request_id=request_id,
                    project_id=project_id,
                )
            # Visual evidence has already been materialized into the interaction
            # context.  Release the optional VLM before the RAG model is loaded
            # or resumed, otherwise a single 8 GiB GPU can fail model dispatch.
            get_topic5_runtime().release_vlm()
            ensure_runtime_loaded()
            rag_interaction_context = interaction_context
            learning_context = _learning_context_for_query(
                rag_query,
                request.active_context.skill_id if request.active_context else None,
                project_id=project_id,
                workflow_id=workflow_id,
                reference_timestamp_ms=request.query_timestamp_ms,
                skill_run_idempotency_key=skill_run_idempotency_key,
            )
            retrieval_feedback = _retrieval_feedback_for_query(
                request.query,
                project_id=project_id,
                workflow_id=workflow_id,
            )
            if retrieval_feedback:
                rag_interaction_context = {
                    **interaction_context,
                    "retrieval_feedback": [
                        item.get("corrected_query")
                        for item in retrieval_feedback
                        if str(item.get("corrected_query") or "").strip()
                    ][:3],
                }
            active_rag = rag
            rag_signature = inspect.signature(active_rag.generate_response_stream)
            rag_parameters = rag_signature.parameters.values()
            supports_progress_callbacks = (
                "on_retrieval_completed" in rag_signature.parameters
                and "on_generation_chunk" in rag_signature.parameters
            ) or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in rag_parameters)
            # Run both the current engine and strict-signature legacy plugins in
            # a worker.  A plugin may take a long time before its first chunk;
            # keeping the queue in the request thread lets the local receipt
            # lease heartbeat continue instead of depending on plugin callbacks.
            updates: Queue[tuple[str, Any]] = Queue()
            rag_kwargs = {
                "query": rag_query,
                "top_k": clamp_chat_top_k(request.top_k),
                "trace_id": trace_id,
                "learning_context": learning_context,
                "interaction_context": rag_interaction_context,
            }
            if supports_progress_callbacks:
                rag_kwargs["on_retrieval_completed"] = retrieval_trace.update

            def run_rag() -> None:
                try:
                    for chunk in active_rag.generate_response_stream(**rag_kwargs):
                        updates.put(("output", chunk))
                except Exception as exc:
                    updates.put(("error", exc))
                finally:
                    updates.put(("done", None))

            threading.Thread(target=run_rag, name="souldrive-chat-rag", daemon=True).start()
            while True:
                try:
                    kind, value = updates.get(timeout=2.0)
                except Empty:
                    renew_request_receipt(force=True)
                    # A zero-width heartbeat keeps the response alive without
                    # polluting the persisted assistant message.
                    yield "\u200b"
                    continue
                if kind == "output":
                    renew_request_receipt()
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - chat_started_at) * 1000)
                    output.append(value)
                    yield value
                elif kind == "error":
                    raise value
                elif kind == "done":
                    break
            # The RAG engine normally emits this artifact itself. Keep the API
            # contract robust when an older sidecar or a model plugin returns a
            # valid answer without the structured map block.
            from core.rag_engine import build_answer_mindmap, query_requests_mindmap

            streamed_output = "".join(output)
            if (
                query_requests_mindmap(request.query)
                and "```souldrive-mindmap" not in streamed_output.lower()
                and "未找到足够可靠的相关证据" not in streamed_output
            ):
                answer_for_map = streamed_output.split("```souldrive-evidence", 1)[0].strip()
                if answer_for_map:
                    map_block = f"\n\n```souldrive-mindmap\n{build_answer_mindmap(request.query, answer_for_map)}\n```"
                    output.append(map_block)
                    yield map_block
            _finish_skill_execution(learning_context, status="completed", outcome={
                "mode": "chat_context_injection",
                "response_chars": len("".join(output)),
                "response_emitted": True,
            })
            if conversation_store and request.session_id:
                assistant_record = conversation_store.append_message(
                    request.session_id,
                    "assistant",
                    "".join(output),
                    metadata={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "status": "completed",
                        "project_id": project_id,
                        "input_digest": chat_input_digest,
                    },
                    request_id=request_id,
                    project_id=project_id,
                )
                complete_request_receipt(
                    str(assistant_record.get("message_id") or "") or None
                    if isinstance(assistant_record, dict)
                    else None
                )
                try:
                    compaction_result = conversation_store.compact_session(
                        request.session_id,
                        project_id=project_id,
                        threshold=int(os.environ.get("SOULDRIVE_SESSION_COMPACTION_THRESHOLD", "16")),
                    )
                    compaction_status = "completed" if compaction_result else "not_needed"
                except Exception as exc:
                    # The assistant message and request receipt are already
                    # durable business facts.  A summary is derived context;
                    # its local failure must remain observable without
                    # reclassifying the completed answer as a failed chat.
                    compaction_status = "failed"
                    _append_audit_best_effort(
                        "chat.compaction_failed",
                        {
                            "request_id": chat_request_id,
                            "session_id": request.session_id,
                            "project_id": project_id,
                            **_error_observation(exc),
                        },
                        trace_id=trace_id,
                    )
            # Completion follows the durable assistant record; a derived
            # summary failure is reported independently above.
            success = True
        except GeneratorExit:
            # A sync StreamingResponse generator can be closed when the client
            # disconnects.  The RAG worker may still be running, so releasing
            # the request receipt here would allow a retry to enter model
            # generation concurrently.  Keep the lease for normal expiry and
            # make the abandoned connection observable without recording the
            # query or generated text.
            if not success:
                runtime_metrics.increment("chat_abandoned")
                _append_audit_best_effort(
                    "chat.abandoned",
                    {
                        "query_chars": len(request.query),
                        "response_chars": len("".join(output)),
                        "request_receipt_kept_in_flight": bool(request_receipt),
                        "retrieval_trace": retrieval_trace or None,
                        "reason": "client_disconnected",
                    },
                    trace_id=trace_id,
                )
            raise
        except Exception as exc:
            runtime_metrics.increment("chat_failures")
            error_observation = _error_observation(exc)
            runtime_metrics.record_error(
                f"{error_observation['error_type']} [{error_observation['error_digest']}]"
            )
            _finish_skill_execution(
                learning_context,
                status="failed",
                failure_reason=str(exc),
            )
            _append_audit_best_effort(
                    "chat.failed",
                    {
                        "query_chars": len(request.query),
                        **error_observation,
                        "interaction_context": interaction_summary,
                        "retrieval_trace": retrieval_trace or None,
                    },
                trace_id=trace_id,
            )
            if conversation_store and request.session_id:
                try:
                    conversation_store.append_message(
                        request.session_id,
                        "assistant",
                        "本地推理服务暂时不可用，已记录诊断事件。",
                        metadata={"trace_id": trace_id, "request_id": request.request_id or trace_id, "status": "failed"},
                        request_id=request.request_id or trace_id,
                    )
                    fail_request_receipt(str(exc))
                except Exception:
                    logger.exception("failed to persist failed chat turn")
                    fail_request_receipt(str(exc))
            else:
                fail_request_receipt(str(exc))
            yield "本地推理服务暂时不可用，已记录诊断事件。"
        finally:
            total_ms = int((time.perf_counter() - chat_started_at) * 1000)
            runtime_metrics.record_chat_latency(
                first_token_ms if first_token_ms is not None else total_ms,
                total_ms,
                success=success,
            )
            if success:
                _append_audit_best_effort(
                    "chat.completed",
                    {
                        "first_token_ms": first_token_ms if first_token_ms is not None else total_ms,
                        "total_ms": total_ms,
                        "under_5s": total_ms < 5000,
                        "request_receipt_completed": request_receipt_completed,
                        "compaction_status": compaction_status,
                        "interaction_context": interaction_summary,
                        "retrieval_trace": retrieval_trace or None,
                    },
                    trace_id=trace_id,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream" # 声明 SSE 协议
    )


def _finish_skill_execution(
    learning_context: dict[str, Any] | None,
    *,
    status: str,
    outcome: dict[str, Any] | None = None,
    failure_reason: str = "",
) -> None:
    execution = (learning_context or {}).get("skill_execution") or {}
    run_id = str(execution.get("run_id") or "").strip()
    if not run_id:
        return
    store = None
    try:
        store = current_learning_store()
        store.update_skill_run(
            run_id,
            status=status,
            outcome=json.dumps(outcome or {}, ensure_ascii=False, sort_keys=True),
            failure_reason=failure_reason,
        )
    except Exception as exc:
        logger.warning("failed to finalize skill run %s: %s", run_id, exc)
    finally:
        if store is not None:
            store.close()


@app.post("/multimodal/decide")
async def multimodal_decide(request: MultimodalDecisionRequest):
    """Return a local, side-effect-free SoulAlign decision for the workbench."""
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    events = request.events
    decision = soulalign_decide(
        request.speech_text,
        events,
        confidence_threshold=request.confidence_threshold,
        active_project_id=request.active_project_id,
        target_required=request.target_required,
    )
    _append_audit_best_effort(
        "multimodal.decision",
        {
            "intent": decision["intent"],
            "action": decision["action"],
            "event_count": len(events),
            "side_effect": "none",
        },
    )
    return decision


@app.get("/multimodal/workflows")
async def multimodal_workflows(project_id: str | None = Query(default=None, max_length=128)):
    """List business-first task contracts without loading ASR/OCR/VLM models."""
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    requested_project_id = str(project_id or "").strip() or None
    scope_response = _unknown_project_response(workspace, requested_project_id)
    if scope_response is not None:
        return scope_response
    approvals = _playbook_approvals_for_project(workspace, requested_project_id)
    return {"workflows": list_workflow_specs(approvals)}


@app.post("/multimodal/workflows/plan")
async def multimodal_workflow_plan(request: MultimodalWorkflowPlanRequest):
    """Check task readiness before spending time on perception or generation."""
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    requested_project_id = str(request.project_id or "").strip() or None
    scope_response = _unknown_project_response(workspace, requested_project_id)
    if scope_response is not None:
        return scope_response
    try:
        plan = plan_workflow(
            request.workflow_id,
            project_id=requested_project_id,
            document_names=request.document_names,
            document_ids=request.document_ids,
            has_audio=request.has_audio,
            has_image=request.has_image,
            speech_text=request.speech_text,
            text_note=request.text_note,
            selected_text=request.selected_text,
            baseline_run_id=request.baseline_run_id,
            current_run_id=request.current_run_id,
            approval=_playbook_approval_for_project(
                workspace,
                requested_project_id,
                request.workflow_id,
            ),
            require_approval=True,
        )
        if request.document_names or request.document_ids:
            plan = _validate_workflow_document_inputs(
                plan,
                workspace=workspace,
                project_id=requested_project_id,
                document_names=request.document_names,
                document_ids=request.document_ids,
            )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    experiment_comparison = None
    if request.workflow_id == "experiment_compare" and (request.baseline_run_id or request.current_run_id):
        try:
            experiment_comparison = _load_explicit_experiment_comparison(
                workspace,
                requested_project_id,
                ActiveInteractionContext(
                    project_id=requested_project_id,
                    baseline_run_id=request.baseline_run_id,
                    current_run_id=request.current_run_id,
                ),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if experiment_comparison is not None and experiment_comparison.get("status") != "comparable":
            reason = str(experiment_comparison.get("reason") or "所选运行不存在或缺少可比字段")
            plan["ready"] = False
            plan["missing_inputs"].append(f"所选实验运行不可比：{reason}")
            plan["next_action"] = "；".join(plan["missing_inputs"])
    _append_audit_best_effort(
        "multimodal.workflow_planned",
        {
            "workflow_id": request.workflow_id,
            "ready": plan["ready"],
            "document_count": plan["input_state"]["document_count"],
            "has_audio": request.has_audio,
            "has_image": request.has_image,
            "has_experiment_comparison": plan["input_state"].get("has_experiment_comparison", False),
            "experiment_comparison_status": experiment_comparison.get("status") if experiment_comparison else None,
            "side_effect": "none",
        },
    )
    return plan


@app.post("/projects/{project_id}/playbooks/{playbook_id}/approval")
async def project_playbook_approval(
    project_id: str,
    playbook_id: str,
    request: MultimodalWorkflowApprovalRequest,
):
    """Review or revoke one built-in Playbook for exactly one project."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project", "project_id": project_id}, status_code=404)
    try:
        spec = get_workflow_spec(playbook_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if request.playbook_version != spec["playbook_version"]:
        return JSONResponse(
            {
                "error": "playbook version is stale",
                "expected_version": spec["playbook_version"],
            },
            status_code=409,
        )

    catalog = ProjectCatalog(workspace)
    try:
        approval = catalog.set_playbook_approval(
            project_id,
            playbook_id,
            request.playbook_version,
            approved=request.approved,
        )
    finally:
        catalog.close()
    _append_audit_best_effort(
        "multimodal.playbook_reviewed",
        {
            "project_id": project_id,
            "playbook_id": playbook_id,
            "playbook_version": request.playbook_version,
            "status": approval["status"],
            "side_effect": "local_catalog_only",
        },
    )
    return {"approval": approval, "playbook": spec}


@app.get("/multimodal/runtime/status")
async def multimodal_runtime_status():
    """Return redacted readiness for the optional local multimodal models."""
    return get_topic5_runtime().status()


@app.post("/multimodal/runtime/warmup-perception")
async def multimodal_runtime_perception_warmup():
    """Explicitly load local ASR/OCR before a user starts a perception task."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    result = await run_in_threadpool(get_topic5_runtime().warmup_perception)
    _append_audit_best_effort(
        "multimodal.runtime_perception_warmup",
        {
            "status": result.get("status"),
            "stages": {
                name: {"status": value.get("status"), "loaded": value.get("loaded")}
                for name, value in (result.get("stages") or {}).items()
                if isinstance(value, dict)
            },
            "side_effect": "none",
        },
    )
    return result


@app.post("/multimodal/runtime/warmup")
async def multimodal_runtime_warmup():
    """Explicitly load the local VLM before a user starts a slow visual task."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    _release_rag_generation_model_for_multimodal()
    result = await run_in_threadpool(get_topic5_runtime().warmup_vlm)
    _append_audit_best_effort(
        "multimodal.runtime_warmup",
        {
            "status": result.get("status"),
            "already_loaded": result.get("already_loaded"),
            "side_effect": "none",
        },
    )
    return result


@app.post("/multimodal/runtime/infer")
async def multimodal_runtime_infer(request: MultimodalRuntimeRequest):
    """Run one authorized local ASR/OCR/VLM episode without side effects."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    scope_response = _unknown_project_response(workspace, request.project_id)
    if scope_response is not None:
        return scope_response
    payload = request.model_dump(exclude_none=True)
    if request.run_vlm or request.target_grounding or request.visual_mode == "slow":
        _release_rag_generation_model_for_multimodal()
    # Model inference is synchronous inside the model libraries; keep it off
    # FastAPI's event loop while preserving the single local runtime holder.
    result = await run_in_threadpool(
        get_topic5_runtime().infer,
        workspace_root=Path(workspace.root_path),
        **payload,
    )
    _append_audit_best_effort(
        "multimodal.runtime_inference",
        {
            "status": result.get("status"),
            "audio_provided": bool(request.audio_ref),
            "image_provided": bool(request.image_ref),
            "selected_region_provided": request.selected_region is not None,
            "stage_status": {
                name: stage.get("status")
                for name, stage in (result.get("stages") or {}).items()
            },
            "side_effect": "none",
        },
    )
    if result.get("status") == "invalid":
        return JSONResponse(result, status_code=400)
    return result


async def _save_runtime_upload(
    upload: UploadFile,
    directory: Path,
    *,
    allowed_suffixes: set[str],
    max_bytes: int,
) -> Path:
    filename = Path(upload.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_suffixes:
        raise ValueError(f"unsupported upload type: {suffix or 'none'}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"capture_{secrets.token_hex(12)}{suffix}"
    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("uploaded media exceeds size limit")
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


@app.post("/multimodal/runtime/infer-upload")
async def multimodal_runtime_infer_upload(
    audio: UploadFile | None = File(default=None),
    image: UploadFile | None = File(default=None),
    speech_text: str | None = Form(default=None, max_length=6000),
    events_json: str | None = Form(default=None, max_length=12000),
    selected_region_json: str | None = Form(default=None, max_length=1000),
    target_id: str | None = Form(default=None, max_length=128),
    project_id: str | None = Form(default=None, max_length=128),
    confidence_threshold: float = Form(default=0.7, ge=0.0, le=1.0),
    event_time_origin_ms: int | None = Form(default=None, ge=0, le=10_000_000_000_000),
    audio_start_ms: int = Form(default=0, ge=0, le=86_400_000),
    image_start_ms: int = Form(default=0, ge=0, le=86_400_000),
    run_ocr: bool = Form(default=True),
    run_vlm: bool = Form(default=False),
    target_grounding: bool = Form(default=False),
    visual_mode: str | None = Form(default=None, pattern="^(auto|fast|slow)$"),
    vlm_prompt: str = Form(
        default="请用一句话说明选区中的界面内容，并指出与当前任务相关的可见信息。",
        max_length=600,
    ),
    max_new_tokens: int = Form(default=DEFAULT_MAX_NEW_TOKENS, ge=16, le=256),
):
    """Process explicitly selected local media, then remove the temporary copy."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    if audio is None and image is None and not (speech_text or "").strip():
        return JSONResponse({"error": "provide audio, image, or speech_text"}, status_code=422)
    try:
        events = _parse_runtime_events(events_json)
        selected_region = _parse_runtime_region(selected_region_json)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    workspace = current_workspace()
    scope_response = _unknown_project_response(workspace, project_id)
    if scope_response is not None:
        return scope_response
    capture_dir = Path(workspace.runtime_path) / "multimodal-capture"
    saved: list[Path] = []
    try:
        audio_path = None
        image_path = None
        if audio is not None:
            audio_path = await _save_runtime_upload(
                audio, capture_dir, allowed_suffixes={".wav"}, max_bytes=16 * 1024 * 1024
            )
            saved.append(audio_path)
        if image is not None:
            image_path = await _save_runtime_upload(
                image, capture_dir, allowed_suffixes={".png", ".jpg", ".jpeg", ".webp"}, max_bytes=8 * 1024 * 1024
            )
            saved.append(image_path)
        payload = {
            "speech_text": speech_text,
            "audio_ref": str(audio_path.relative_to(Path(workspace.root_path))).replace("\\", "/") if audio_path else None,
            "image_ref": str(image_path.relative_to(Path(workspace.root_path))).replace("\\", "/") if image_path else None,
            "selected_region": selected_region,
            "events": events,
            "target_id": target_id,
            "project_id": project_id,
            "run_ocr": run_ocr,
            "run_vlm": run_vlm,
            "target_grounding": target_grounding,
            "visual_mode": visual_mode,
            "vlm_prompt": vlm_prompt,
            "max_new_tokens": max_new_tokens,
            "confidence_threshold": confidence_threshold,
            "event_time_origin_ms": event_time_origin_ms,
            "audio_start_ms": audio_start_ms,
            "image_start_ms": image_start_ms,
        }
        if run_vlm or target_grounding or visual_mode == "slow":
            _release_rag_generation_model_for_multimodal()
        result = await run_in_threadpool(
            get_topic5_runtime().infer,
            workspace_root=Path(workspace.root_path),
            **payload,
        )
        _append_audit_best_effort(
            "multimodal.runtime_upload_inference",
            {
                "status": result.get("status"),
                "audio_provided": audio is not None,
                "image_provided": image is not None,
                "selected_region_provided": selected_region is not None,
                "side_effect": "none",
            },
        )
        if result.get("status") == "invalid":
            return JSONResponse(result, status_code=400)
        return result
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        for path in saved:
            path.unlink(missing_ok=True)

# =====================================================================
# --- 备用/拓展 MCP 接口 ---
# =====================================================================
@app.get("/ping")
async def ping():
    return {"status": "SoulDrive MCP Server is fully operational."}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "souldrive-sidecar", "storage": _storage_health()}

@app.get("/ready")
async def ready():
    state = effective_runtime_state()
    ready_state = not state.get("locked")
    return JSONResponse(
        {
            "ready": ready_state,
            "locked": state.get("locked"),
            "reason": state.get("reason"),
            "workspace_mounted": bool(state.get("workspace_path")),
        },
        status_code=200 if ready_state else 503,
    )

@app.get("/runtime/status")
async def runtime_status():
    _reconcile_indexer_state()
    state = get_runtime_state()
    _sync_index_run_receipt(state)
    return public_runtime_state()


@app.get("/storage/status")
async def storage_status():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return _storage_health()


@app.get("/sessions/{session_id}/history")
async def session_history(
    session_id: str,
    limit: int = 8,
    project_id: str | None = Query(default=None, max_length=128),
    before_sequence: int | None = Query(default=None, ge=1),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        store = _conversation_store(current_workspace())
        if store is None:
            return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
        session = store.session_metadata(session_id)
        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        session_project_id = str(session.get("project_id") or "").strip() or None
        requested_project_id = str(project_id or "").strip() or None
        if session_project_id != requested_project_id:
            if session_project_id and requested_project_id is None:
                return JSONResponse(
                    {"error": "project_id is required for project session history"},
                    status_code=409,
                )
            return JSONResponse({"error": "session belongs to another project"}, status_code=409)
        if before_sequence is not None:
            payload = store.load_before(session_id, before_sequence, limit=limit)
        else:
            payload = store.load_context(session_id, limit=limit)
        return {
            "session_id": session_id,
            "history": payload.get("history") or [],
            "summary": payload.get("summary"),
            "source": payload.get("source"),
            "records": payload.get("records") or [],
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@app.get("/projects/{project_id}/sessions")
async def project_sessions(project_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    store = _conversation_store(workspace)
    if store is None:
        return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
    return {"project_id": project_id, "sessions": store.list_sessions(project_id)}


@app.get("/projects/{project_id}/artifacts")
async def project_artifacts(project_id: str, artifact_type: str | None = Query(default=None, max_length=64)):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    catalog = ProjectCatalog(workspace)
    try:
        return {"project_id": project_id, "artifacts": catalog.list_artifacts(project_id, artifact_type=artifact_type)}
    finally:
        catalog.close()


@app.get("/sessions")
async def global_sessions():
    """List sessions explicitly created outside any project."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = _conversation_store(current_workspace())
    if store is None:
        return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
    return {"project_id": None, "sessions": store.list_sessions(None)}


@app.post("/sessions")
async def global_session_create(request: SessionCreateRequest):
    """Create a global session; global chat never inherits a project's transcript."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = _conversation_store(current_workspace())
    if store is None:
        return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
    try:
        session_id = request.session_id or f"session_{uuid.uuid4().hex}"
        existing = store.session_metadata(session_id) if request.session_id else None
        session = store.create_session(session_id, project_id=None, title=request.title, workflow_id=request.workflow_id)
        replayed = existing is not None
        warning = _append_audit_best_effort(
            "session.replayed" if replayed else "session.created",
            {"project_id": None, "session_id": session_id, "replayed": replayed},
        )
        payload = {"status": "replayed" if replayed else "created", "replayed": replayed, "session": session}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@app.post("/projects/{project_id}/sessions")
async def project_session_create(project_id: str, request: SessionCreateRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    store = _conversation_store(workspace)
    if store is None:
        return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
    try:
        session_id = request.session_id or f"session_{uuid.uuid4().hex}"
        existing = store.session_metadata(session_id) if request.session_id else None
        session = store.create_session(session_id, project_id=project_id, title=request.title, workflow_id=request.workflow_id)
        replayed = existing is not None
        warning = _append_audit_best_effort(
            "session.replayed" if replayed else "session.created",
            {"project_id": project_id, "session_id": session_id, "replayed": replayed},
        )
        payload = {"status": "replayed" if replayed else "created", "replayed": replayed, "session": session}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@app.delete("/projects/{project_id}/sessions/{session_id}")
async def project_session_delete(project_id: str, session_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    store = _conversation_store(workspace)
    if store is None:
        return JSONResponse({"error": "workspace keys unavailable"}, status_code=423)
    try:
        session = store.session_metadata(session_id)
        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if session.get("project_id") != project_id:
            return JSONResponse({"error": "session belongs to another project"}, status_code=409)
        deleted = store.delete_session(session_id, project_id=project_id)
        warning = _append_audit_best_effort("session.deleted", {"project_id": project_id, "session_id": session_id})
        payload = {"status": "deleted", "session": deleted}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

@app.get("/security/status")
async def security_status():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    workspace = current_workspace()
    state = effective_runtime_state()
    return {
        "crypto_initialized": is_keystore_initialized(workspace),
        "software_unlocked": bool(state.get("software_unlocked")),
        "hardware_mounted": bool(state.get("hardware_mounted") or state.get("workspace_path")),
        "reason": state.get("security_reason") or state.get("reason"),
        "no_recovery": True,
        "development_bypass": os.environ.get("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", "0") == "1",
    }

@app.post("/security/init")
async def security_init(request: SecurityInitRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    if not request.acknowledge_no_recovery:
        return JSONResponse({"error": "no recovery acknowledgement required"}, status_code=400)

    workspace = current_workspace()
    try:
        initialize_keystore(workspace, request.passphrase)
        keys = unlock_keystore(workspace, request.passphrase)
    except KeystoreAlreadyInitializedError:
        return JSONResponse({"error": "workspace keystore already initialized"}, status_code=409)

    set_workspace_keys(workspace.root_path, keys)
    mark_software_unlocked()
    warning = _append_audit_best_effort("security.keystore_initialized", {"no_recovery": True})
    payload = {"initialized": True, "software_unlocked": True, "state": public_runtime_state()}
    if warning:
        payload["warning"] = warning
    return payload

@app.post("/security/unlock")
async def security_unlock(request: SecurityUnlockRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    workspace = current_workspace()
    try:
        keys = unlock_keystore(workspace, request.passphrase)
    except KeystoreNotInitializedError:
        return JSONResponse({"error": "workspace keystore is not initialized"}, status_code=409)
    except IncorrectPassphraseError:
        warning = _append_audit_best_effort("security.unlock_failed", {"reason": "incorrect passphrase"})
        payload = {"error": "incorrect passphrase"}
        if warning:
            payload["warning"] = warning
        return JSONResponse(payload, status_code=403)

    set_workspace_keys(workspace.root_path, keys)
    mark_software_unlocked()
    return {"software_unlocked": True, "state": public_runtime_state()}

@app.post("/security/lock")
async def security_lock():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    workspace = current_workspace()
    _cleanup_portable_session(workspace)
    mark_software_locked()
    return {"software_unlocked": False, "state": public_runtime_state()}

@app.get("/documents/list")
async def documents_list():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return _document_library_payload(current_workspace())


def _document_id_candidates(document_id: str | None) -> set[str]:
    normalized = str(document_id or "").strip()
    if not normalized:
        return set()
    if normalized.startswith("secure_"):
        return {normalized, normalized.removeprefix("secure_")}
    return {normalized, f"secure_{normalized}"}


def _normalize_document_selector(
    *,
    name: str | None = None,
    document_id: str | None = None,
) -> tuple[str | None, str | None]:
    normalized_name = str(name or "").strip() or None
    normalized_id = str(document_id or "").strip() or None
    return normalized_name, normalized_id


def _document_evidence_for_payload(
    catalog: ProjectCatalog, project_id: str, name: str, payload: bytes,
) -> dict | None:
    for content_hash in (hashlib.md5(payload).hexdigest(), hashlib.sha256(payload).hexdigest()):
        evidence = catalog.find_asset_version_for_file(name, content_hash=content_hash, project_id=project_id)
        if evidence is not None:
            return evidence
    return None


def _document_payload_by_selector(
    workspace: SoulDriveWorkspace,
    *,
    name: str | None = None,
    document_id: str | None = None,
    project_id: str | None = None,
) -> tuple[bytes | None, str | None, str | None, str | None]:
    """Read one document by opaque ID, or by a unique display name.

    The display name is a human label, not an identity.  When an ID is
    supplied, never fall back to a name lookup if that ID is missing.
    """
    normalized_name, normalized_id = _normalize_document_selector(
        name=name,
        document_id=document_id,
    )
    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        store = SecureDocumentStore(workspace, workspace_keys)
        try:
            documents = store.iter_documents()
            if normalized_id:
                id_candidates = _document_id_candidates(normalized_id)
                matches = [item for item in documents if str(item["object_id"]) in id_candidates]
            else:
                matches = [item for item in documents if item["name"] == normalized_name]
            payloads = {}
            if project_id and matches:
                catalog = ProjectCatalog(workspace)
                try:
                    scoped = []
                    for item in matches:
                        payload = store.read_document_bytes(item["object_id"])
                        if _document_evidence_for_payload(catalog, project_id, str(item["name"]), payload):
                            scoped.append(item)
                            payloads[item["object_id"]] = payload
                    matches = scoped
                finally:
                    catalog.close()
                if not matches:
                    return None, "文档来源与当前项目的内容版本不匹配", None, None
            if len(matches) > 1:
                return (
                    None,
                    "文档 ID 不唯一，请刷新文档库后重试"
                    if normalized_id
                    else "同名文档不唯一，请使用文档 ID 明确选择",
                    None,
                    None,
                )
            if matches:
                selected = matches[0]
                if normalized_name and selected["name"] != normalized_name:
                    return None, "文档 ID 与名称不匹配，请刷新文档库后重试", None, None
                return (
                    payloads[selected["object_id"]] if selected["object_id"] in payloads
                    else store.read_document_bytes(selected["object_id"]),
                    None,
                    str(selected["name"]),
                    str(selected["object_id"]),
                )
        finally:
            store.close()

    # An explicit ID is authoritative, including when managed keys are absent.
    if normalized_id:
        return None, None, None, None

    papers_root = Path(workspace.papers_path).resolve()
    source = (papers_root / str(normalized_name or "")).resolve()
    if not source.is_relative_to(papers_root):
        return None, "文档路径超出工作区资料范围", None, None
    if source.exists() and source.is_file() and source.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
        return source.read_bytes(), None, str(normalized_name), None
    return None, None, None, None


def _document_payload_by_name(workspace: SoulDriveWorkspace, name: str) -> tuple[bytes | None, str | None]:
    """读取一个文档本体；同名多文档时拒绝猜测。"""
    payload, error, _, _ = _document_payload_by_selector(workspace, name=name)
    return payload, error


@app.post("/documents/globalize")
async def documents_globalize(request: DocumentGlobalizeRequest):
    """解除导入文档的项目绑定，保留本体并要求重建派生索引。"""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    _reconcile_indexer_state()
    indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
    if indexing_status in {"queued", "scanning", "indexing"}:
        return JSONResponse({"error": "知识索引正在更新，请完成后再归类文档"}, status_code=409)

    workspace = current_workspace()
    name, document_id = _normalize_document_selector(
        name=request.name,
        document_id=request.document_id,
    )
    if not name and not document_id:
        return JSONResponse({"error": "name 或 document_id 至少提供一个"}, status_code=422)
    payload, payload_error, resolved_name, resolved_document_id = _document_payload_by_selector(
        workspace,
        name=name,
        document_id=document_id,
    )
    if payload_error:
        return JSONResponse({"error": payload_error}, status_code=409)
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)
    name = resolved_name or name or ""
    document_id = resolved_document_id or document_id

    catalog = ProjectCatalog(workspace)
    try:
        result = catalog.unbind_imported_document(name, hashlib.md5(payload).hexdigest())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        catalog.close()

    warning = None
    if result["status"] == "globalized":
        warning = _append_audit_best_effort("document.globalized", {
            "source_filename": name,
            "document_id": document_id,
            "previous_project_ids": result["project_ids"],
            "reindex_required": True,
        })
    payload = {
        "status": result["status"],
        "scope": "global",
        "name": name,
        "document_id": document_id,
        "previous_project_ids": result["project_ids"],
        "reindex_required": result["status"] == "globalized",
    }
    if warning:
        payload["warning"] = warning
    return payload


@app.get("/documents/preview")
async def documents_preview(
    name: str | None = Query(default=None, max_length=260),
    document_id: str | None = Query(default=None, max_length=256),
    project_id: str | None = Query(default=None, max_length=128),
):
    """Return a bounded, read-only preview for the in-app document viewer."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    name, document_id = _normalize_document_selector(name=name, document_id=document_id)
    if not name and not document_id:
        return JSONResponse({"error": "name 或 document_id 至少提供一个"}, status_code=422)
    payload, payload_error, resolved_name, resolved_document_id = _document_payload_by_selector(
        workspace,
        name=name,
        document_id=document_id,
    )
    if payload_error:
        return JSONResponse({"error": payload_error}, status_code=409)
    name = resolved_name or name or ""
    document_id = resolved_document_id or document_id
    media_type = document_media_type(name)
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)

    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id and not _project_exists(workspace, normalized_project_id):
        return JSONResponse({"error": "unknown project", "project_id": normalized_project_id}, status_code=404)
    indexed_preview = _indexed_document_preview(
        workspace,
        name,
        project_id=normalized_project_id,
        document_id=document_id,
    )
    if indexed_preview.get("scope_error"):
        return JSONResponse({"error": indexed_preview["scope_error"]}, status_code=409)
    if indexed_preview["chunks"]:
        return {
            "name": name,
            "document_id": document_id,
            "media_type": media_type,
            "text": indexed_preview["text"],
            "chunks": indexed_preview["chunks"],
            "preview_source": "indexed_clean_text",
            "truncated": indexed_preview["truncated"],
        }

    text = ""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            text = "\n\n".join((page.extract_text() or "") for page in PdfReader(BytesIO(payload)).pages)
        except Exception:
            text = "该 PDF 暂未提取到可预览文本，请通过证据链查看索引内容。"
    elif suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temp_path = handle.name
        try:
            chunks = PlainDocumentParser().parse_and_chunk(temp_path)
            text = "\n\n".join(chunk.page_content for chunk in chunks)
        finally:
            Path(temp_path).unlink(missing_ok=True)
    return {
        "name": name,
        "document_id": document_id,
        "media_type": media_type,
        "text": text[:30000],
        "chunks": [],
        "preview_source": "source_extraction",
        "truncated": len(text) > 30000,
    }


@app.post("/documents/delete-authorize")
async def documents_delete_authorize(request: DocumentDeleteAuthorizationRequest):
    """Verify the workspace passphrase once for the currently open document library."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    try:
        unlock_keystore(workspace, request.passphrase)
    except KeystoreNotInitializedError:
        return JSONResponse({"error": "workspace keystore is not initialized"}, status_code=409)
    except IncorrectPassphraseError:
        warning = _append_audit_best_effort(
            "document.delete_authorization_failed",
            {"reason": "incorrect passphrase"},
        )
        payload = {"error": "incorrect passphrase"}
        if warning:
            payload["warning"] = warning
        return JSONResponse(payload, status_code=403)

    authorization = secrets.token_urlsafe(32)
    with document_delete_authorizations_lock:
        document_delete_authorizations[authorization] = {
            "workspace_path": workspace.root_path,
            "expires_at": time.time() + 15 * 60,
        }
    return {"authorization": authorization, "expires_in_seconds": 900}


@app.post("/documents/delete-authorize/revoke")
async def documents_delete_authorize_revoke(request: DocumentDeleteAuthorizationRevokeRequest):
    with document_delete_authorizations_lock:
        document_delete_authorizations.pop(request.authorization, None)
    return {"revoked": True}


def _document_delete_authorized(workspace: SoulDriveWorkspace, authorization: str) -> bool:
    with document_delete_authorizations_lock:
        grant = document_delete_authorizations.get(authorization)
    return bool(
        grant
        and grant.get("workspace_path") == workspace.root_path
        and float(grant.get("expires_at") or 0) >= time.time()
    )


def _delete_indexed_document(
    workspace: SoulDriveWorkspace,
    name: str,
    payload: bytes | None,
    *,
    document_id: str | None = None,
) -> None:
    ensure_runtime_loaded()
    selected_document_ids = _document_id_candidates(document_id)
    child_ids: list[str] = []
    metadatas: list[dict[str, Any]] = []
    if selected_document_ids:
        for selected_id in sorted(selected_document_ids):
            existing = kb.collection.get(where={"document_id": selected_id}, include=["metadatas"])
            child_ids.extend(str(child_id) for child_id in (existing.get("ids") or []))
            metadatas.extend(dict(metadata or {}) for metadata in (existing.get("metadatas") or []))
    else:
        existing = kb.collection.get(where={"source_filename": name}, include=["metadatas"])
        child_ids = [str(child_id) for child_id in (existing.get("ids") or [])]
        metadatas = [dict(metadata or {}) for metadata in (existing.get("metadatas") or [])]
    project_ids = {
        str(metadata.get("project_id") or "")
        for metadata in metadatas
        if metadata and str(metadata.get("project_id") or "")
    }
    if len(project_ids) > 1 and not selected_document_ids:
        raise ValueError("同名文档属于多个项目，请使用文档 ID 删除，系统未执行删除")
    parent_ids = sorted({
        str(metadata.get("parent_id") or "")
        for metadata in metadatas
        if metadata and metadata.get("parent_id")
    })
    child_ids = list(dict.fromkeys(child_ids))
    has_explicit_identity = bool(
        selected_document_ids
        and any(
            str(metadata.get("document_id") or "").strip() in selected_document_ids
            for metadata in metadatas
        )
    )
    if payload is not None and (not selected_document_ids or not has_explicit_identity):
        kb.delete_document_indexes(hashlib.md5(payload).hexdigest())
    kb.delete_index_records(child_ids=child_ids, parent_ids=parent_ids)
    if Path(workspace.parent_doc_path).exists():
        with sqlite3.connect(str(workspace.parent_doc_path)) as conn:
            rows = conn.execute(
                "SELECT parent_id, metadata_json FROM parent_documents WHERE json_extract(metadata_json, '$.source_filename') = ?",
                (name,),
            ).fetchall()
            durable_parent_ids = []
            for row in rows:
                if selected_document_ids:
                    try:
                        metadata = json.loads(row[1] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if str(metadata.get("document_id") or "").strip() not in selected_document_ids:
                        continue
                durable_parent_ids.append(str(row[0]))
            if durable_parent_ids:
                placeholders = ",".join("?" for _ in durable_parent_ids)
                conn.execute(f"DELETE FROM parent_documents WHERE parent_id IN ({placeholders})", durable_parent_ids)
                conn.commit()
    from core.document_usage import forget_document
    forget_document(workspace.root_path, name, document_id=document_id)


@app.post("/documents/delete")
async def documents_delete(
    request: DocumentDeleteRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _document_delete_authorized(workspace, request.authorization):
        return JSONResponse({"error": "document delete authorization expired"}, status_code=403)

    name, document_id = _normalize_document_selector(
        name=request.name,
        document_id=request.document_id,
    )
    if not name and not document_id:
        return JSONResponse({"error": "name 或 document_id 至少提供一个"}, status_code=422)
    normalized_key = str(idempotency_key or "").strip() or None
    if normalized_key and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", normalized_key):
        return JSONResponse({"error": "Idempotency-Key 格式无效"}, status_code=422)
    deletion_owner = f"document-delete:{os.getpid()}:{uuid.uuid4().hex}"
    deletion_catalog = ProjectCatalog(workspace) if normalized_key else None
    keys = get_workspace_keys(workspace.root_path)

    def close_deletion_catalog() -> None:
        nonlocal deletion_catalog
        if deletion_catalog is not None:
            deletion_catalog.close()
            deletion_catalog = None

    payload, payload_error, resolved_name, resolved_document_id = _document_payload_by_selector(
        workspace,
        name=name,
        document_id=document_id,
    )
    if payload_error:
        close_deletion_catalog()
        return JSONResponse({"error": payload_error}, status_code=409)
    name = resolved_name or name or ""
    document_id = resolved_document_id or document_id
    secure_document = bool(resolved_document_id)
    secure_document_object_id = resolved_document_id
    source = Path(workspace.papers_path) / name
    if payload is None:
        receipt = (
            deletion_catalog.document_deletion_receipt(normalized_key)
            if deletion_catalog is not None and normalized_key
            else None
        )
        close_deletion_catalog()
        receipt_name = str(receipt.get("name") or "") if receipt else ""
        receipt_document_id = str(receipt.get("document_id") or "") if receipt else ""
        receipt_matches = bool(receipt) and (
            (not name or receipt_name == name)
            and (not document_id or receipt_document_id == document_id)
        )
        if receipt_matches and receipt and receipt.get("status") == "completed":
            return {
                "deleted": True,
                "name": receipt_name,
                "document_id": receipt_document_id or document_id,
                "replayed": True,
                "idempotency_key": normalized_key,
            }
        if receipt_matches and receipt and receipt.get("status") in {"in_progress", "uncertain"}:
            return JSONResponse(
                {
                    "error": "文档删除结果尚未确定，请使用原幂等键重试或检查文档库。",
                    "deletion_status": receipt.get("status"),
                    "idempotency_key": normalized_key,
                },
                status_code=409,
            )
        return JSONResponse({"error": "document not found"}, status_code=404)

    deletion_claim = None
    if deletion_catalog is not None and normalized_key:
        content_digest = hashlib.sha256(payload).hexdigest()
        try:
            deletion_claim = deletion_catalog.claim_document_deletion(
                name,
                content_digest,
                normalized_key,
                owner_id=deletion_owner,
                document_id=document_id,
            )
        except ValueError as exc:
            close_deletion_catalog()
            return JSONResponse({"error": str(exc)}, status_code=409)
        if not deletion_claim.get("claimed"):
            close_deletion_catalog()
            if deletion_claim.get("status") == "completed":
                return JSONResponse(
                    {
                        "error": "幂等键已绑定到已完成的旧删除，当前同名文档可能是重新导入的文件，请使用新键。",
                        "deletion_status": "completed",
                        "idempotency_key": normalized_key,
                    },
                    status_code=409,
                )
            return JSONResponse(
                {
                    "error": "文档删除正在处理中，请稍后使用同一幂等键重试。",
                    "deletion_status": "in_progress",
                    "idempotency_key": normalized_key,
                },
                status_code=409,
            )

    try:
        _delete_indexed_document(workspace, name, payload, document_id=document_id)
    except Exception as exc:
        logger.exception("[Documents] Refusing source deletion because index cleanup failed for %s", request.name)
        if deletion_catalog is not None and normalized_key:
            deletion_catalog.mark_document_deletion_failed(normalized_key, deletion_owner, exc)
            close_deletion_catalog()
        return JSONResponse(
            {
                "error": "文档索引清理失败，源文件未删除，请修复后重试。",
                **_error_observation(exc),
            },
            status_code=500,
        )

    try:
        if secure_document:
            store = SecureDocumentStore(workspace, keys)
            try:
                if secure_document_object_id:
                    if store.delete_document_object(secure_document_object_id) is None:
                        raise FileNotFoundError("managed document disappeared before deletion commit")
            finally:
                store.close()
        else:
            source.unlink()
    except Exception as exc:
        if deletion_catalog is not None and normalized_key:
            deletion_catalog.mark_document_deletion_uncertain(normalized_key, deletion_owner, exc)
            close_deletion_catalog()
        return JSONResponse(
            {
                "error": "文档删除结果不确定，请使用原幂等键重试或检查文档库。",
                "deletion_status": "uncertain",
                **_error_observation(exc),
            },
            status_code=409,
        )
    if deletion_catalog is not None and normalized_key:
        try:
            deletion_catalog.complete_document_deletion(normalized_key, deletion_owner)
        except Exception as exc:
            try:
                deletion_catalog.mark_document_deletion_uncertain(normalized_key, deletion_owner, exc)
            except Exception:
                logger.exception("[Documents] Unable to persist uncertain deletion receipt for %s", name)
            close_deletion_catalog()
            return JSONResponse(
                {
                    "error": "文档已执行删除，但完成收据未写入，请使用原幂等键核对。",
                    "deletion_status": "uncertain",
                    **_error_observation(exc),
                },
                status_code=500,
            )
        close_deletion_catalog()
    warning = _append_audit_best_effort(
        "document.deleted",
        {"source_filename": name, "document_id": document_id},
    )
    payload = {"deleted": True, "name": name, "document_id": document_id}
    if normalized_key:
        payload["idempotency_key"] = normalized_key
        payload["replayed"] = False
    if warning:
        payload["warning"] = warning
    return payload


@app.get("/projects")
async def projects_list():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return _project_catalog_payload(current_workspace())


@app.post("/projects")
async def project_create(
    request: ProjectCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    """在本地项目目录创建项目，不产生云端或外部副作用。"""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        normalized_key = str(idempotency_key or "").strip() or None
        existing = catalog.get_project_by_idempotency_key(normalized_key) if normalized_key else None
        if existing and request.project_id and str(request.project_id).strip() != str(existing["project_id"]):
            return JSONResponse(
                {"error": "idempotency_key is already bound to a different project identity"},
                status_code=409,
            )
        project_id = (
            str(existing["project_id"])
            if existing
            else request.project_id
            or (
                f"project_{hashlib.sha256(f'project-create:{normalized_key}'.encode('utf-8')).hexdigest()[:32]}"
                if normalized_key
                else f"project_{uuid.uuid4().hex}"
            )
        )
        root_path = _validated_project_root(current_workspace(), request.root_path, project_id)
        root_path.mkdir(parents=True, exist_ok=True)
        project_result = catalog.create_project(
            request.name.strip(), project_id=project_id, project_mode=request.project_mode,
            root_path=str(root_path), instructions=request.instructions,
            memory_policy=request.memory_policy,
            idempotency_key=normalized_key,
            return_status=True,
        )
        project_id = str(project_result["project_id"] if isinstance(project_result, dict) else project_result)
        replayed = bool(project_result.get("replayed")) if isinstance(project_result, dict) else bool(existing is not None)
        warning = None
        if not replayed:
            warning = _append_audit_best_effort(
                "project.created", {"project_id": project_id, "project_mode": request.project_mode, "root_path": str(root_path)}
            )
        payload = {
            "project_id": project_id,
            "status": "replayed" if replayed else "created",
            "replayed": replayed,
            "root_path": str(root_path),
        }
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        status_code = 409 if "idempotency_key" in str(exc) else 400
        return JSONResponse({"error": str(exc)}, status_code=status_code)
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "项目标识已存在，请更换名称。"}, status_code=409)
    finally:
        catalog.close()


def _project_index_identity(workspace: SoulDriveWorkspace, project_id: str) -> dict[str, set[str]]:
    catalog = ProjectCatalog(workspace)
    try:
        rows = catalog.conn.execute(
            """
            SELECT a.asset_id, v.version_id, v.file_ref
            FROM assets a
            LEFT JOIN asset_versions v ON v.asset_id = a.asset_id
            WHERE a.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    finally:
        catalog.close()
    return {
        "asset_ids": {str(row[0]) for row in rows if row[0]},
        "version_ids": {str(row[1]) for row in rows if row[1]},
        "file_refs": {
            str(row[2]).replace("\\", "/").strip()
            for row in rows
            if row[2]
        },
    }


def _project_index_record_owned(
    metadata: dict[str, Any],
    project_id: str,
    identity: dict[str, set[str]],
) -> bool:
    metadata_project_id = str(metadata.get("project_id") or "").strip()
    if metadata_project_id:
        return metadata_project_id == project_id
    if str(metadata.get("asset_id") or "") in identity["asset_ids"]:
        return True
    if str(metadata.get("asset_version_id") or "") in identity["version_ids"]:
        return True
    file_ref = str(metadata.get("catalog_file_ref") or "").replace("\\", "/").strip()
    return bool(file_ref and file_ref in identity["file_refs"])


def _delete_project_indexes(
    workspace: SoulDriveWorkspace,
    project_id: str,
    identity: dict[str, set[str]],
) -> dict[str, int]:
    """删除项目派生索引；原始文件和项目文件夹不在此处删除。"""
    keys = get_workspace_keys(workspace.root_path)
    child_ids: list[str] = []
    parent_ids: set[str] = set()

    if keys is not None:
        store = SecureVectorStore(workspace.secure_vector_store_path, keys)
        try:
            payload = store.collection.get(include=["metadatas"])
            for child_id, metadata in zip(payload.get("ids") or [], payload.get("metadatas") or []):
                metadata = dict(metadata or {})
                if not _project_index_record_owned(metadata, project_id, identity):
                    continue
                child_ids.append(str(child_id))
                if metadata.get("parent_id"):
                    parent_ids.add(str(metadata["parent_id"]))
            store.delete(child_ids)
            store.delete_parent_ids(sorted(parent_ids))
        finally:
            store.close()
    else:
        from core.knowledge_base import _persistent_client
        from core.keyword_index import KeywordIndex
        from core.parent_doc_store import ParentDocumentStore

        client = _persistent_client(path=workspace.chroma_path)
        try:
            collection = client.get_or_create_collection(name="souldrive_papers")
            payload = collection.get(include=["metadatas"])
            for child_id, metadata in zip(payload.get("ids") or [], payload.get("metadatas") or []):
                metadata = dict(metadata or {})
                if not _project_index_record_owned(metadata, project_id, identity):
                    continue
                child_ids.append(str(child_id))
                if metadata.get("parent_id"):
                    parent_ids.add(str(metadata["parent_id"]))
            collection.delete(ids=child_ids)
        finally:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()

        parent_store = ParentDocumentStore(workspace.parent_doc_path)
        keyword_index = KeywordIndex(workspace.keyword_index_path)
        try:
            parent_store.delete(sorted(parent_ids))
            keyword_index.delete(child_ids)
        finally:
            parent_store.close()
            keyword_index.close()

    graph_store = None
    graph_chunk_ids: set[str] = set()
    try:
        if keys is not None:
            graph_store = SecureGraphStore(workspace.secure_graph_store_path, keys)
        else:
            from core.graph_db import LocalGraphDB

            graph_store = LocalGraphDB(workspace.graph_db_path)
        snapshot = graph_store.get_graph_snapshot()
        for document in snapshot.get("documents") or []:
            document_project_id = str(document.get("project_id") or "").strip()
            if document_project_id == project_id:
                graph_chunk_ids.update(str(item) for item in document.get("chunk_ids") or [] if str(item))
        all_chunk_ids = sorted(set(child_ids) | graph_chunk_ids)
        graph_store.remove_project(project_id, all_chunk_ids)
    finally:
        if graph_store is not None:
            graph_store.close()

    return {
        "index_child_count": len(child_ids),
        "index_parent_count": len(parent_ids),
        "graph_chunk_count": len(graph_chunk_ids),
    }


def _delete_project_root(workspace: SoulDriveWorkspace, root_path: str | None) -> dict[str, Any]:
    """Delete the user-confirmed project folder without allowing workspace deletion."""
    raw = str(root_path or "").strip()
    if not raw:
        return {"root_deleted": False, "root_path": "", "root_warning": "项目未登记原始文件夹"}
    root = Path(raw).expanduser().resolve()
    workspace_root = Path(workspace.root_path).resolve()
    if root == workspace_root or workspace_root in root.parents:
        raise ValueError("项目原始目录不能是 SoulDrive 工作区或其父目录")
    if not root.exists():
        return {"root_deleted": True, "root_path": str(root), "root_warning": "原始项目文件夹已不存在"}
    if not root.is_dir():
        raise ValueError("项目原始路径不是文件夹")
    shutil.rmtree(root)
    return {"root_deleted": True, "root_path": str(root), "root_warning": ""}


def _delete_project_learning_scope(
    workspace: SoulDriveWorkspace,
    project_id: str,
    conversation_store: Any,
) -> dict[str, Any]:
    """Delete project learning records after fencing their durable projections."""
    learning_path = Path(workspace.learning_store_path)
    if not learning_path.exists():
        return {
            "status": "not_present",
            "project_id": project_id,
            "memory_count": 0,
            "skill_count": 0,
            "skill_run_count": 0,
            "learning_chunk_count": 0,
            "memory_projection_count": 0,
        }
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        raise RuntimeError("workspace keys are unavailable for project learning cleanup")

    learning_store = LearningStore(workspace, keys)
    try:
        snapshot = learning_store.project_scope_snapshot(project_id)
        delete_projections = getattr(conversation_store, "delete_project_memories", None)
        if callable(delete_projections):
            projection_report = delete_projections(
                project_id,
                memory_ids=snapshot.get("memory_ids") or [],
            )
        elif snapshot.get("memory_ids"):
            raise RuntimeError("memory projection store cannot clean project memories")
        else:
            projection_report = {"memory_count": 0, "archive_deleted_count": 0}
        local_report = learning_store.delete_project_scope(project_id)
    finally:
        learning_store.close()
    return {
        "status": "deleted",
        "project_id": project_id,
        "memory_count": int(local_report.get("memory_count") or 0),
        "skill_count": int(local_report.get("skill_count") or 0),
        "skill_run_count": int(local_report.get("skill_run_count") or 0),
        "learning_chunk_count": int(local_report.get("learning_chunk_count") or 0),
        "memory_version_count": int(local_report.get("memory_version_count") or 0),
        "mutation_receipt_count": int(local_report.get("mutation_receipt_count") or 0),
        "sync_outbox_count": int(local_report.get("sync_outbox_count") or 0),
        "candidate_count": int(local_report.get("candidate_count") or 0),
        "review_count": int(local_report.get("review_count") or 0),
        "feedback_count": int(local_report.get("feedback_count") or 0),
        "memory_projection_count": int(projection_report.get("memory_count") or 0),
        "archive_deleted_count": int(projection_report.get("archive_deleted_count") or 0),
    }


@app.delete("/projects/{project_id}")
async def project_delete(project_id: str):
    """删除项目数据、派生索引及用户确认的原始项目文件夹。"""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    project_id = str(project_id or "").strip()
    workspace = current_workspace()
    catalog = ProjectCatalog(workspace)
    try:
        deletion = catalog.begin_project_deletion(project_id)
    finally:
        catalog.close()
    if deletion is None:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    if deletion["status"] == "completed":
        replay = dict(deletion.get("result") or {})
        replay.setdefault("status", "deleted")
        replay["idempotent_replay"] = True
        return replay

    _reconcile_indexer_state()
    indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
    if indexing_status in {"queued", "scanning", "indexing"}:
        return JSONResponse({"error": "知识索引正在更新，请完成后再删除项目"}, status_code=409)

    snapshot = dict(deletion.get("snapshot") or {})
    root_path = deletion.get("root_path") or snapshot.get("root_path")
    try:
        identity = _project_index_identity(workspace, project_id)
        # Close open vector/session handles before mutating their backing stores.
        cleanup_runtime()
        index_report = _delete_project_indexes(workspace, project_id, identity)
        store = _conversation_store(workspace)
        if store is None:
            raise RuntimeError("workspace keys unavailable")
        learning_report = _delete_project_learning_scope(workspace, project_id, store)
        sessions = store.list_sessions(project_id)
        for session in sessions:
            store.delete_session(str(session["session_id"]), project_id=project_id)

        # Remove the user project folder before the catalog row.  If this
        # fails, the durable receipt and project row both remain retryable.
        root_report = _delete_project_root(workspace, root_path)
        catalog = ProjectCatalog(workspace)
        try:
            deleted = catalog.delete_project(project_id)
            result = {
                "status": "deleted",
                **(deleted or snapshot),
                **root_report,
                **index_report,
                "session_count": len(sessions),
                "learning": learning_report,
            }
            # The tombstone is deliberately committed after the project row;
            # if the process dies before this write, the next request resumes
            # from the same receipt and treats already-finished steps as safe.
            catalog.complete_project_deletion(project_id, result)
        finally:
            catalog.close()

        warning = _append_audit_best_effort("project.deleted", {
            "project_id": project_id,
            "index_child_count": result["index_child_count"],
            "index_parent_count": result["index_parent_count"],
            "session_count": result["session_count"],
            "root_deleted": result["root_deleted"],
            "learning_memory_count": result["learning"].get("memory_count", 0),
            "learning_skill_count": result["learning"].get("skill_count", 0),
        })
        if warning:
            result = {**result, "warning": warning}
        return result
    except Exception as exc:
        try:
            catalog = ProjectCatalog(workspace)
            try:
                catalog.mark_project_deletion_failed(project_id, str(exc))
            finally:
                catalog.close()
        except Exception:
            logger.exception("failed to persist project deletion failure for %s", project_id)
        logger.exception("project deletion failed for %s", project_id)
        _append_audit_best_effort("project.delete_failed", {
            "project_id": project_id,
            "error": "项目删除失败，请查看运行诊断。",
            "error_observation": _error_observation(exc),
        })
        return JSONResponse(
            {"error": "项目数据清理失败，项目目录记录已保留，请修复后重试"},
            status_code=500,
        )


@app.get("/projects/{project_id}/overview")
async def project_overview(project_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        return _project_overview_payload(current_workspace(), project_id)
    except KeyError:
        return JSONResponse({"error": "unknown project"}, status_code=404)


@app.get("/projects/{project_id}/experiments/runs")
async def project_experiment_runs(
    project_id: str,
    performed_from: str | None = Query(default=None, max_length=64),
    performed_to: str | None = Query(default=None, max_length=64),
    experiment_name: str | None = Query(default=None, max_length=160),
):
    """Return versioned experiment runs without loading the language model."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        if catalog.conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        return {
            "project_id": project_id,
            "runs": catalog.list_experiment_runs(
                project_id,
                performed_from=performed_from,
                performed_to=performed_to,
                experiment_name=experiment_name,
            ),
        }
    finally:
        catalog.close()


@app.get("/projects/{project_id}/experiments/runs/{run_id}/measurements/{metric_id}/versions")
async def project_experiment_measurement_versions(
    project_id: str,
    run_id: str,
    metric_id: str,
    split: str | None = Query(default=None, max_length=64),
):
    """Return immutable revisions for one run/metric/split measurement."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        run = catalog.get_experiment_run(run_id)
        if run is None or str(run.get("project_id") or "") != project_id:
            return JSONResponse({"error": "unknown experiment run"}, status_code=404)
        revisions = catalog.list_measurement_revisions(run_id, metric_id, split=split)
        current_keys = {
            (str(item.get("metric_id") or ""), str(item.get("split") or ""))
            for item in run.get("measurements") or []
        }
        if not revisions and not any(
            key == (str(metric_id), str(split)) if split is not None else key[0] == str(metric_id)
            for key in current_keys
        ):
            return JSONResponse({"error": "unknown measurement"}, status_code=404)
        current_revision = max((int(item["revision"]) for item in revisions), default=None)
        return {
            "schema_version": "souldrive_measurement_versions_v0.1",
            "project_id": project_id,
            "run_id": run_id,
            "metric_id": metric_id,
            "split": split,
            "immutable": True,
            "current_revision": current_revision,
            "revisions": revisions,
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        catalog.close()


@app.post("/projects/{project_id}/experiments/repeats/summary")
async def project_experiment_repeat_summary(project_id: str, request: ExperimentRepeatSummaryRequest):
    """Summarize explicitly selected same-experiment repeats, read-only."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        if catalog.conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        result = summarize_experiment_repeats(catalog, request.run_ids, project_id=project_id)
        warning = _append_audit_best_effort(
            "experiment.repeats_summarized",
            {"project_id": project_id, "run_count": len(request.run_ids), "status": result.get("status"), "side_effect": "none"},
        )
        if warning:
            result = {**result, "warning": warning}
        return result
    finally:
        catalog.close()


@app.post("/projects/{project_id}/experiments/metrics")
async def project_experiment_metrics(project_id: str, request: ExperimentMetricImportRequest):
    """Plan or confirm a local metrics import; unconfirmed requests never write."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    source_name = request.source_name.strip()
    payload, payload_error, source_name, source_document_id = _document_payload_by_selector(
        workspace, name=source_name, document_id=request.source_document_id, project_id=project_id,
    )
    if payload_error:
        return JSONResponse({"error": payload_error}, status_code=409)
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)
    catalog = ProjectCatalog(workspace)
    temp_path: Path | None = None
    try:
        evidence = _document_evidence_for_payload(catalog, project_id, source_name, payload)
        if evidence is None:
            return JSONResponse({"error": "metrics source is not uniquely cataloged in this project"}, status_code=409)
        if request.source_version_id and request.source_version_id != str(evidence["version_id"]):
            return JSONResponse({"error": "metrics source version changed; preview and confirm the selected source again"}, status_code=409)
        suffix = Path(source_name).suffix.lower() or ".txt"
        with tempfile.NamedTemporaryFile(prefix="souldrive-metrics-", suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        result = import_metric_source(
            catalog,
            project_id=project_id,
            evidence_version_id=str(evidence["version_id"]),
            path=temp_path,
            experiment_name=request.experiment_name,
            confirmed=request.confirmed,
            source_name=source_name,
            performed_at=request.performed_at,
            dataset_version=request.dataset_version,
            config_ref=request.config_ref,
            evaluation_script_ref=request.evaluation_script_ref,
            environment_ref=request.environment_ref,
            unit=request.unit,
            direction=request.direction,
            definition_version=request.definition_version,
            split=request.split,
        )
        warning = _append_audit_best_effort(
            "experiment.metrics_imported" if result["status"] == "imported" else "experiment.metrics_import_planned",
            {
                "project_id": project_id,
                "source_name": source_name,
                "source_document_id": source_document_id,
                "source_version_id": evidence["version_id"],
                "experiment_name": request.experiment_name,
                "status": result["status"],
                "confirmed": request.confirmed,
                "side_effect": "catalog_write" if result["status"] == "imported" else "none",
            },
        )
        response = {**result, "source_version_id": evidence["version_id"], "source_document_id": source_document_id}
        if warning:
            response["warning"] = warning
        return response
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        catalog.close()


@app.post("/projects/{project_id}/experiments/compare")
async def project_experiment_compare(project_id: str, request: ExperimentCompareRequest):
    """Compare two explicitly selected runs or return a last-month selection card."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        if catalog.conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        if request.baseline_run_id and request.current_run_id:
            result = compare_experiment_runs(
                catalog,
                request.baseline_run_id,
                request.current_run_id,
                project_id=project_id,
            )
            warning = _append_audit_best_effort(
                "experiment.compared",
                {
                    "project_id": project_id,
                    "baseline_run_id": request.baseline_run_id,
                    "current_run_id": request.current_run_id,
                    "status": result.get("status"),
                    "side_effect": "none",
                },
            )
            if warning:
                result = {**result, "warning": warning}
            return result
        previous = find_previous_month_runs(
            catalog,
            project_id,
            reference_at=request.reference_at,
            timezone_name=request.timezone_name,
            experiment_name=request.experiment_name,
        )
        current_runs = catalog.list_experiment_runs(project_id, experiment_name=request.experiment_name)
        return {
            "status": "needs_confirmation",
            "reason": "请选择当前运行和上个月运行；存在多个候选时不默认取第一条。",
            "previous_month": previous,
            "current_candidates": current_runs[:8],
            "requested": request.model_dump(exclude_none=True),
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        catalog.close()


@app.post("/contacts")
async def contacts_create(request: ContactCreateRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        contact_result = catalog.create_contact(**request.model_dump(), return_status=True)
        contact_id = str(contact_result["contact_id"] if isinstance(contact_result, dict) else contact_result)
        replayed = bool(contact_result.get("replayed")) if isinstance(contact_result, dict) else False
        warning = None
        if not replayed:
            warning = _append_audit_best_effort(
                "contact.created",
                {"contact_id": contact_id, "project_count": len(request.project_ids)},
            )
        payload = {"contact_id": contact_id, "status": "replayed" if replayed else "created", "replayed": replayed}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.delete("/contacts/{contact_id}")
async def contacts_delete(contact_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        if not catalog.delete_contact(contact_id):
            return JSONResponse({"error": "contact_not_found"}, status_code=404)
        warning = _append_audit_best_effort("contact.deleted", {"contact_id": contact_id})
        payload = {"status": "deleted", "contact_id": contact_id}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.get("/contacts")
async def contacts_list(project_id: str | None = Query(default=None, max_length=128)):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        return {"contacts": catalog.list_contacts(project_id=project_id)}
    finally:
        catalog.close()


@app.post("/contacts/resolve")
async def contacts_resolve(request: ContactResolveRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        return {"contacts": catalog.resolve_contacts(request.query, project_id=request.project_id)}
    finally:
        catalog.close()


def _draft_contact_or_response(catalog: ProjectCatalog, query: str, project_id: str | None):
    matches = catalog.resolve_contacts(query, project_id=project_id)
    if not matches:
        return None, JSONResponse({"error": "contact_not_found", "query": query}, status_code=404)
    highest = matches[0]["match_score"]
    best = [item for item in matches if item["match_score"] == highest]
    if len(best) != 1:
        return None, JSONResponse(
            {"error": "contact_ambiguous", "query": query, "candidates": best},
            status_code=409,
        )
    return best[0], None


def _artifact_source_refs(
    catalog: ProjectCatalog,
    project_id: str | None,
    source_names: list[str] | None = None,
    source_version_ids: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Resolve catalog-backed source versions and expose unresolved names explicitly."""
    verified_ids = _verified_source_version_ids(catalog, project_id, source_version_ids or [])
    resolved_ids = list(verified_ids)
    refs: list[dict[str, str]] = [
        {"version_id": version_id, "status": "resolved"}
        for version_id in verified_ids
    ]
    for raw_name in source_names or []:
        name = str(raw_name or "").strip()
        if not name:
            continue
        evidence = catalog.find_asset_version_for_file(name, project_id=project_id)
        if evidence is not None and (project_id is None or str(evidence.get("project_id") or "") == project_id):
            version_id = str(evidence.get("version_id") or "").strip()
            if version_id and version_id not in resolved_ids:
                resolved_ids.append(version_id)
            refs.append({"source_filename": name[:260], "version_id": version_id, "status": "resolved"})
        else:
            refs.append({"source_filename": name[:260], "version_id": "", "status": "unresolved"})
    return resolved_ids[:64], refs[:64]


def _project_scope_response(
    resource: dict[str, Any],
    requested_project_id: str | None,
    *,
    resource_label: str,
) -> JSONResponse | None:
    """Require an explicit, matching project for project-bound resources.

    Workspace-wide artifacts (currently schedules) intentionally remain
    addressable without a project query.  A project-bound artifact, such as an
    email draft, must not be readable or mutated by its globally unique ID
    alone, and a project query must not widen a workspace-wide artifact into a
    project scope.
    """
    error = _project_scope_error(
        resource,
        requested_project_id,
        resource_label=resource_label,
    )
    if error is not None:
        return JSONResponse({"error": error}, status_code=409)
    return None


def _project_scope_error(
    resource: dict[str, Any],
    requested_project_id: str | None,
    *,
    resource_label: str,
) -> str | None:
    stored_project_id = str(resource.get("project_id") or "").strip() or None
    requested = str(requested_project_id or "").strip() or None
    if stored_project_id is None:
        if requested is not None:
            return f"{resource_label} is workspace-scoped; omit project_id"
        return None
    if requested is None:
        return f"project_id is required for a project-bound {resource_label}"
    if requested != stored_project_id:
        return f"{resource_label} belongs to another project"
    return None


def _artifact_project_scope_response(
    artifact: dict[str, Any],
    requested_project_id: str | None,
) -> JSONResponse | None:
    return _project_scope_response(
        artifact,
        requested_project_id,
        resource_label="artifact",
    )


def _presentation_task_scope_response(
    task: dict[str, Any],
    requested_project_id: str | None,
) -> JSONResponse | None:
    return _project_scope_response(
        task,
        requested_project_id,
        resource_label="presentation task",
    )


def _email_attachment_bindings(
    catalog: ProjectCatalog,
    workspace: SoulDriveWorkspace,
    project_id: str,
    source_names: list[str] | None,
) -> list[dict[str, str]]:
    """Bind email attachments to one project asset and, when available, one immutable store object."""
    keys = get_workspace_keys(workspace.root_path)
    store = SecureDocumentStore(workspace, keys) if keys is not None else None
    try:
        secure_documents = store.iter_documents() if store is not None else []
        bindings: list[dict[str, str]] = []
        for raw_name in source_names or []:
            name = str(raw_name or "").strip()
            if not name:
                continue
            evidence = catalog.find_asset_version_for_file(name, project_id=project_id)
            if evidence is None:
                bindings.append({"source_filename": name[:260], "version_id": "", "content_hash": "", "status": "unresolved"})
                continue

            binding = {
                "source_filename": name[:260],
                "version_id": str(evidence.get("version_id") or ""),
                "content_hash": str(evidence.get("content_hash") or ""),
                "status": "resolved_catalog",
            }
            if store is not None:
                for document in secure_documents:
                    if document.get("name") != name:
                        continue
                    try:
                        payload = store.read_document_bytes(document["object_id"])
                    except (OSError, ValueError):
                        continue
                    if hashlib.md5(payload).hexdigest() == binding["content_hash"]:
                        binding["status"] = "resolved_secure"
                        binding["object_id"] = str(document["object_id"])
                        break
            bindings.append(binding)
        return bindings[:64]
    finally:
        if store is not None:
            store.close()


@app.post("/drafts/email")
async def email_draft_create(request: EmailDraftRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        contact, response = _draft_contact_or_response(catalog, request.recipient_query, request.project_id)
        if response is not None:
            return response
        plan = {
            "recipient": {
                "contact_id": contact["contact_id"],
                "display_name": contact["display_name"],
                "email": contact["email"],
            },
            "subject": request.subject,
            "body": request.body,
            "attachment_refs": request.attachment_refs,
            "source_refs": [],
            "side_effect": "none",
            "requires_user_confirmation": True,
        }
        attachment_bindings = _email_attachment_bindings(
            catalog,
            current_workspace(),
            request.project_id,
            request.attachment_refs,
        )
        unresolved_attachments = [
            binding["source_filename"]
            for binding in attachment_bindings
            if binding.get("status") == "unresolved"
        ]
        if unresolved_attachments:
            return JSONResponse(
                {
                    "error": "email attachment is not uniquely bound to the requested project",
                    "attachments": unresolved_attachments,
                },
                status_code=409,
            )
        plan["attachment_bindings"] = attachment_bindings
        source_version_ids, source_refs = _artifact_source_refs(
            catalog,
            request.project_id,
            request.attachment_refs,
            request.source_version_ids,
        )
        plan["source_refs"] = source_refs
        artifact_result = catalog.create_artifact(
            request.project_id,
            "email_draft",
            plan,
            idempotency_key=request.idempotency_key,
            source_version_ids=source_version_ids,
            confirmation_record={
                "status": "draft",
                "mode": "draft_created",
                "required": True,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            change_summary="创建邮件草稿；未执行外部发送，等待用户确认。",
            return_status=True,
        )
        artifact_id = str(artifact_result["artifact_id"] if isinstance(artifact_result, dict) else artifact_result)
        replayed = bool(artifact_result.get("replayed")) if isinstance(artifact_result, dict) else False
        artifact = catalog.get_artifact(artifact_id)
        warning = None
        if not replayed:
            warning = _append_audit_best_effort(
                "artifact.email_draft_created",
                {"artifact_id": artifact_id, "project_id": request.project_id, "body_chars": len(request.body)},
            )
        payload = {"artifact": artifact, "replayed": replayed}
        if warning:
            payload["warning"] = warning
        return payload
    except (ValueError, sqlite3.IntegrityError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.post("/drafts/email/attachments")
async def email_draft_upload_attachments(project_id: str = Form(default="", max_length=128), attachments: list[UploadFile] = File(default_factory=list)):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not str(project_id or "").strip() or not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        return JSONResponse({"error": "workspace passphrase required"}, status_code=423)
    if not attachments:
        return JSONResponse({"error": "no attachments provided"}, status_code=422)
    store = SecureDocumentStore(workspace, keys)
    catalog = ProjectCatalog(workspace)
    try:
        attachment_refs: list[str] = []
        for attachment in attachments[:12]:
            filename = Path(attachment.filename or "").name
            if not filename:
                return JSONResponse({"error": "attachment filename is required"}, status_code=422)
            payload = await attachment.read()
            if not payload:
                return JSONResponse({"error": f"empty attachment: {filename}"}, status_code=422)
            if len(payload) > 25 * 1024 * 1024:
                return JSONResponse({"error": f"attachment too large: {filename}"}, status_code=413)
            result = store.import_document_bytes(filename, payload)
            if result.get("status") == "rejected":
                return JSONResponse({"error": f"unsupported attachment type: {filename}"}, status_code=422)
            attachment_name = str(result.get("name") or filename)
            catalog.register_imported_document(
                project_id,
                Path(attachment_name).stem,
                hashlib.md5(payload).hexdigest(),
                attachment_name,
                asset_type="document",
                metadata={
                    "media_type": result.get("media_type") or document_media_type(attachment_name),
                    "storage": "secure_document_store",
                },
            )
            attachment_refs.append(attachment_name)
        return {"status": "ok", "attachment_refs": attachment_refs}
    finally:
        catalog.close()
        store.close()


@app.post("/drafts/schedule")
async def schedule_draft_create(request: ScheduleDraftRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        context_project_id = str(request.project_id or "").strip() or None
        if context_project_id and not _project_exists(current_workspace(), context_project_id):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        participant = None
        if request.participant_query:
            participant, response = _draft_contact_or_response(catalog, request.participant_query, context_project_id)
            if response is not None:
                return response
        plan = {
            "title": request.title,
            "start_at": normalize_start_at(request.start_at),
            "duration_minutes": request.duration_minutes,
            "participant": ({
                "contact_id": participant["contact_id"],
                "display_name": participant["display_name"],
                "email": participant["email"],
            } if participant else None),
            "notes": request.notes,
            "reminder_sound_enabled": request.reminder_sound_enabled,
            "source_refs": [],
            "side_effect": "none",
            "requires_user_confirmation": True,
        }
        source_version_ids, source_refs = _artifact_source_refs(
            catalog,
            context_project_id,
            source_version_ids=request.source_version_ids,
        )
        plan["source_refs"] = source_refs
        artifact_result = catalog.create_artifact(
            None,
            "schedule_draft",
            plan,
            idempotency_key=request.idempotency_key,
            source_version_ids=source_version_ids,
            confirmation_record={
                "status": "draft",
                "mode": "draft_created",
                "required": True,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            change_summary="创建日程草稿；未写入外部日历，等待用户确认。",
            return_status=True,
        )
        artifact_id = str(artifact_result["artifact_id"] if isinstance(artifact_result, dict) else artifact_result)
        replayed = bool(artifact_result.get("replayed")) if isinstance(artifact_result, dict) else False
        artifact = catalog.get_artifact(artifact_id)
        warning = None
        if not replayed:
            warning = _append_audit_best_effort(
                "artifact.schedule_draft_created",
                {"artifact_id": artifact_id, "project_id": None, "context_project_id": context_project_id},
            )
        payload = {"artifact": artifact, "replayed": replayed}
        if warning:
            payload["warning"] = warning
        return payload
    except (ValueError, sqlite3.IntegrityError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.get("/schedules")
async def global_schedules():
    """Return all schedules; calendar is workspace-wide, not project-scoped."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        return {"artifacts": catalog.list_artifacts(None, artifact_type="schedule_draft")}
    finally:
        catalog.close()


@app.get("/schedules/history")
async def schedule_history(
    days: int = Query(default=7, ge=1, le=31),
    limit: int = Query(default=100, ge=1, le=200),
):
    """Return a bounded recent schedule history without removing old records."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        return {
            "days": days,
            "artifacts": catalog.list_schedule_history(days=days, limit=limit),
        }
    finally:
        catalog.close()


@app.patch("/drafts/{artifact_id}")
async def schedule_draft_update(
    artifact_id: str,
    request: ScheduleDraftUpdateRequest,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        current = catalog.get_artifact(artifact_id)
        if current is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        scope_response = _artifact_project_scope_response(current, project_id)
        if scope_response is not None:
            return scope_response
        if current["artifact_type"] != "schedule_draft":
            return JSONResponse({"error": "only schedule drafts can be updated here"}, status_code=409)
        if current["confirmation_status"] == "cancelled":
            return JSONResponse({"error": "cancelled schedule cannot be updated"}, status_code=409)
        participant = None
        if request.participant_query:
            participant, response = _draft_contact_or_response(catalog, request.participant_query, current["project_id"])
            if response is not None:
                return response
        previous_status = current["confirmation_status"]
        if previous_status == "confirmed":
            catalog.set_artifact_status(artifact_id, "draft")
        artifact = catalog.update_artifact_provenance(
            artifact_id,
            plan_patch={
                "title": request.title,
                "start_at": normalize_start_at(request.start_at),
                "duration_minutes": request.duration_minutes,
                "participant": ({
                    "contact_id": participant["contact_id"],
                    "display_name": participant["display_name"],
                    "email": participant["email"],
                } if participant else None),
                "notes": request.notes,
                "reminder_sound_enabled": request.reminder_sound_enabled,
            },
            confirmation_record_patch={
                "mode": "schedule_edited",
                "previous_confirmation_status": previous_status,
                "edited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            change_summary="日程已修改；确认前不会写入外部日历。",
        )
        if artifact is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        warning = _append_audit_best_effort(
            "artifact.schedule_draft_updated",
            {"artifact_id": artifact_id, "project_id": current["project_id"], "previous_status": previous_status},
        )
        payload = {"artifact": artifact}
        if warning:
            payload["warning"] = warning
        return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.delete("/drafts/{artifact_id}")
async def draft_delete(
    artifact_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        current = catalog.get_artifact(artifact_id)
        if current is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        scope_response = _artifact_project_scope_response(current, project_id)
        if scope_response is not None:
            return scope_response
        if current["artifact_type"] != "schedule_draft":
            return JSONResponse({"error": "only schedule drafts can be deleted here"}, status_code=409)
        artifact = catalog.set_artifact_status(artifact_id, "cancelled")
        if artifact is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        catalog.update_artifact_provenance(
            artifact_id,
            confirmation_record_patch={
                "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "deleted_reason": "user_deleted",
            },
            change_summary="日程草稿已由用户删除，不再出现在我的日程中。",
        )
        warning = _append_audit_best_effort(
            "artifact.schedule_draft_deleted",
            {"artifact_id": artifact_id, "project_id": current["project_id"]},
        )
        payload = {"artifact": catalog.get_artifact(artifact_id)}
        if warning:
            payload["warning"] = warning
        return payload
    finally:
        catalog.close()


@app.get("/drafts/{artifact_id}")
async def draft_get(
    artifact_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        artifact = catalog.get_artifact(artifact_id)
        if artifact is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        scope_response = _artifact_project_scope_response(artifact, project_id)
        if scope_response is not None:
            return scope_response
        return {"artifact": artifact}
    finally:
        catalog.close()


@app.post("/drafts/{artifact_id}/status")
async def draft_set_status(
    artifact_id: str,
    request: ArtifactStatusRequest,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        current = catalog.get_artifact(artifact_id)
        if current is None:
            return JSONResponse({"error": "unknown artifact"}, status_code=404)
        scope_response = _artifact_project_scope_response(current, project_id)
        if scope_response is not None:
            return scope_response
        if current["confirmation_status"] != "draft":
            return JSONResponse({"error": "artifact is already finalized", "artifact": current}, status_code=409)
        artifact = catalog.set_artifact_status(artifact_id, request.status)
        warning = _append_audit_best_effort(
            "artifact.status_changed",
            {"artifact_id": artifact_id, "artifact_type": current["artifact_type"], "status": request.status},
        )
        payload = {"artifact": artifact}
        if warning:
            payload["warning"] = warning
        return payload
    finally:
        catalog.close()


@app.post("/email/smtp-config")
async def email_smtp_config(request: EmailSmtpConfigRequest):
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        result = configure_smtp(request.provider, request.sender, request.authorization_code)
        warning = _append_audit_best_effort(
            "email.smtp_configured", {"provider": result["provider"], "sender": result["sender"]}
        )
        payload = dict(result)
        if warning:
            payload["warning"] = warning
        return payload
    except EmailConfigurationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/email/smtp-status")
async def email_smtp_config_status(sender: str = Query(default="", max_length=320)):
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return JSONResponse(smtp_status(sender), headers={"Cache-Control": "no-store"})


@app.get("/cloud-model/config")
async def cloud_model_config_status():
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    config = load_cloud_model_config()
    payload = config.public_dict() if config is not None else {
        "configured": False,
        "enabled": False,
        "base_url": "",
        "model": "",
        "models": [],
        "api_key_configured": False,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/cloud-model/models")
async def cloud_model_models(request: CloudModelModelsRequest):
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        models = saved_or_requested_models(request.base_url, request.api_key)
        return {"models": models}
    except CloudModelConfigurationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except CloudModelRequestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/cloud-model/config")
async def cloud_model_configure(request: CloudModelConfigRequest):
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        config = save_cloud_model_config(
            request.base_url,
            request.api_key,
            request.model,
            request.enabled,
            request.models,
        )
    except CloudModelConfigurationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if rag is not None:
        release_fn = getattr(rag, "release_generation_model", None)
        if callable(release_fn):
            release_fn()
    warning = _append_audit_best_effort(
        "cloud_model.configured",
        {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "model": config.model,
            "model_count": len(config.models),
        },
    )
    payload = config.public_dict()
    if warning:
        payload["warning"] = warning
    return payload


@app.post("/drafts/{artifact_id}/send")
async def email_draft_send(
    artifact_id: str,
    request: EmailSendRequest,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    catalog = ProjectCatalog(workspace)
    delivery_attempt_started = False
    try:
        artifact = catalog.get_artifact(artifact_id)
        if artifact is None or artifact["artifact_type"] != "email_draft":
            return JSONResponse({"error": "unknown email draft"}, status_code=404)
        scope_response = _artifact_project_scope_response(artifact, project_id)
        if scope_response is not None:
            return scope_response
        if artifact["confirmation_status"] != "confirmed":
            return JSONResponse({"error": "email draft requires confirmation"}, status_code=409)
        plan = dict(artifact["plan"])
        if plan.get("sender") and plan["sender"] != request.sender:
            return JSONResponse({"error": "sender differs from the confirmed draft"}, status_code=409)
        keys = get_workspace_keys(workspace.root_path)
        if keys is None:
            return JSONResponse({"error": "workspace passphrase required"}, status_code=423)
        plan["sender"] = request.sender
        plan["idempotency_key"] = request.idempotency_key
        # Reserve before reading attachments so a completed idempotent replay
        # does not depend on the current document store contents.
        request_digest = hashlib.sha256(request.sender.strip().casefold().encode("utf-8")).hexdigest()
        existing = catalog.reserve_email_delivery(
            artifact_id,
            request.idempotency_key,
            request_digest=request_digest,
        )
        if existing == "sent":
            return {"status": "sent", "idempotent": True}
        if existing == "uncertain":
            return JSONResponse(
                {
                    "error": "email delivery outcome is uncertain; verify the provider before retrying",
                    "delivery_status": "uncertain",
                },
                status_code=409,
            )
        if existing == "conflict":
            return JSONResponse(
                {"error": "idempotency key is already bound to another email delivery or this draft"},
                status_code=409,
            )
        if existing != "reserved":
            return JSONResponse({"error": "email delivery already attempted; create a new confirmed draft to retry"}, status_code=409)
        attachment_names = [str(name) for name in plan.get("attachment_refs") or []]
        attachment_bindings = list(plan.get("attachment_bindings") or [])
        if len(attachment_bindings) != len(attachment_names):
            attachment_bindings = _email_attachment_bindings(
                catalog,
                workspace,
                str(artifact.get("project_id") or ""),
                attachment_names,
            )
        if len(attachment_bindings) != len(attachment_names):
            raise EmailDeliveryError("confirmed attachment binding is incomplete")

        store = SecureDocumentStore(workspace, keys)
        try:
            attachments = []
            for name, binding in zip(attachment_names, attachment_bindings):
                if binding.get("status") == "unresolved" or not binding.get("version_id"):
                    raise EmailDeliveryError(f"confirmed attachment is not bound to this project: {name}")
                payload = None
                object_id = str(binding.get("object_id") or "").strip()
                if object_id:
                    try:
                        payload = store.read_document_bytes(object_id)
                    except (OSError, ValueError):
                        payload = None
                if payload is None:
                    payload, payload_error = _document_payload_by_name(workspace, name)
                    if payload_error:
                        raise EmailDeliveryError(f"confirmed attachment is ambiguous: {name}")
                if payload is None:
                    raise EmailDeliveryError(f"confirmed attachment is unavailable: {name}")
                expected_hash = str(binding.get("content_hash") or "")
                if not re.fullmatch(r"[0-9a-fA-F]{32}", expected_hash) or hashlib.md5(payload).hexdigest() != expected_hash.lower():
                    raise EmailDeliveryError(f"confirmed attachment changed or lacks an immutable content binding: {name}")
                attachments.append((name, payload))
        finally:
            store.close()
        delivery_attempt_started = True
        result = send_confirmed_email(plan, attachments)
        catalog.finish_email_delivery(artifact_id, "sent", result)
        warning = _append_audit_best_effort(
            "email.sent", {"artifact_id": artifact_id, "provider": result["provider"], "attachment_count": result["attachment_count"]}
        )
        payload = {"status": "sent", "idempotent": False}
        if warning:
            payload["warning"] = warning
        return payload
    except (EmailConfigurationError, EmailDeliveryError) as exc:
        # EmailDeliveryError from the SMTP connector can mean that the
        # provider accepted the message but the response was lost.  Once the
        # external call boundary has been crossed, preserve the uncertain
        # state so a retry cannot create a duplicate message.
        delivery_status = "uncertain" if delivery_attempt_started and isinstance(exc, EmailDeliveryError) else "failed"
        error_observation = _error_observation(exc)
        error_detail = (
            {**error_observation, "delivery_status": delivery_status}
            if delivery_status == "uncertain"
            else {
                "error": _PUBLIC_EMAIL_FAILURE,
                "error_observation": error_observation,
                "delivery_status": delivery_status,
            }
        )
        try:
            catalog.finish_email_delivery(artifact_id, delivery_status, error_detail)
        except Exception:
            # The provider outcome is already classified.  A local receipt
            # write failure must not replace that truthful uncertainty with a
            # generic server error or allow the caller to assume a safe retry.
            logger.exception("failed to persist email delivery exception outcome")
        event_name = "email.uncertain" if delivery_status == "uncertain" else "email.failed"
        audit_error = (
            error_observation
            if delivery_status == "uncertain"
            else {"error": _PUBLIC_EMAIL_FAILURE, "error_observation": error_observation}
        )
        warning = _append_audit_best_effort(
            event_name,
            {"artifact_id": artifact_id, **audit_error, "delivery_status": delivery_status},
        )
        if delivery_status == "uncertain":
            payload = {
                "error": "email delivery outcome is uncertain; verify the provider before retrying",
                "delivery_status": "uncertain",
            }
        else:
            payload = {
                "error": _PUBLIC_EMAIL_FAILURE,
                "error_observation": error_observation,
                "delivery_status": "failed",
            }
        if warning:
            payload["warning"] = warning
        return JSONResponse(payload, status_code=502)
    except Exception as exc:
        delivery_status = "uncertain" if delivery_attempt_started else "failed"
        error_observation = _error_observation(exc)
        try:
            error_detail = (
                {**error_observation, "delivery_status": delivery_status}
                if delivery_status == "uncertain"
                else {
                    "error": _PUBLIC_EMAIL_FAILURE,
                    "error_observation": error_observation,
                    "delivery_status": delivery_status,
                }
            )
            catalog.finish_email_delivery(artifact_id, delivery_status, error_detail)
        except Exception:
            logger.exception("failed to persist email delivery outcome")
        event_name = "email.uncertain" if delivery_status == "uncertain" else "email.failed"
        audit_error = (
            error_observation
            if delivery_status == "uncertain"
            else {"error": _PUBLIC_EMAIL_FAILURE, "error_observation": error_observation}
        )
        warning = _append_audit_best_effort(
            event_name,
            {"artifact_id": artifact_id, **audit_error, "delivery_status": delivery_status},
        )
        if delivery_status == "uncertain":
            payload = {
                "error": "email delivery outcome is uncertain; verify the provider before retrying",
                "delivery_status": "uncertain",
            }
        else:
            payload = {
                "error": _PUBLIC_EMAIL_FAILURE,
                "error_observation": error_observation,
                "delivery_status": "failed",
            }
        if warning:
            payload["warning"] = warning
        return JSONResponse(payload, status_code=502)
    finally:
        catalog.close()


@app.post("/drafts/{artifact_id}/calendar-ics")
async def schedule_draft_export_ics(
    artifact_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        workspace = current_workspace()
        lock_id = hashlib.sha256(str(artifact_id).encode("utf-8")).hexdigest()[:32]
        lock_path = Path(workspace.runtime_path) / "locks" / f"calendar-export-{lock_id}.lock"
        with local_file_lock(lock_path):
            # Re-read the artifact after acquiring the local cross-process lock
            # so the receipt check, file publication, provenance update, and
            # audit event form one replay-safe critical section.
            artifact = catalog.get_artifact(artifact_id)
            if artifact is None or artifact["artifact_type"] != "schedule_draft":
                return JSONResponse({"error": "unknown schedule draft"}, status_code=404)
            scope_response = _artifact_project_scope_response(artifact, project_id)
            if scope_response is not None:
                return scope_response
            if artifact["confirmation_status"] != "confirmed":
                return JSONResponse({"error": "schedule draft requires confirmation"}, status_code=409)
            plan_digest = hashlib.sha256(
                json.dumps(artifact["plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            expected_output_path = str(Path(workspace.calendars_path) / f"{artifact_id}.ics")
            expected_output = Path(expected_output_path)
            export_receipt = (artifact.get("confirmation_record") or {}).get("calendar_export")
            if (
                isinstance(export_receipt, dict)
                and export_receipt.get("plan_digest") == plan_digest
                and expected_output.is_file()
            ):
                recorded_content_sha256 = str(export_receipt.get("content_sha256") or "").strip().lower()
                actual_content_sha256 = hashlib.sha256(expected_output.read_bytes()).hexdigest()
                if recorded_content_sha256 and actual_content_sha256 == recorded_content_sha256:
                    return {
                        "status": "exported",
                        "output_path": expected_output_path,
                        "replayed": True,
                    }
                return JSONResponse(
                    {
                        "error": "本地 .ics 文件内容已被修改，不能把它当作原始导出结果回放；请重新确认后再导出",
                        "error_code": "calendar_export_output_conflict",
                        "output_path": expected_output_path,
                        "replayed": False,
                    },
                    status_code=409,
                )
            output_path = export_ics(
                workspace,
                artifact_id,
                artifact["plan"],
                stamp_at=artifact.get("created_at"),
            )
            content_sha256 = hashlib.sha256(Path(output_path).read_bytes()).hexdigest()
            catalog.update_artifact_provenance(
                artifact_id,
                confirmation_record_patch={
                    "calendar_export": {
                        "output_path": output_path,
                        "plan_digest": plan_digest,
                        "content_sha256": content_sha256,
                        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                },
                change_summary="已导出确认后的本地 .ics 文件；相同日程重试将回放该导出结果。",
            )
            warning = _append_audit_best_effort("schedule.ics_exported", {"artifact_id": artifact_id})
            payload = {"status": "exported", "output_path": output_path, "replayed": False}
            if warning:
                payload["warning"] = warning
            return payload
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.post("/documents/import")
async def documents_import(request: PaperImportRequest):
    return _import_documents(request)


def _run_presentation_task(task_id: str, suppress_wps: bool = False) -> None:
    try:
        service = current_presentation_service()
        result = service.run_task(task_id, suppress_wps=suppress_wps)
        if result.get("status") == "completed":
            runtime_metrics.increment("presentation_tasks_completed")
        elif result.get("status") == "cancelled":
            runtime_metrics.increment("presentation_tasks_cancelled")
        else:
            runtime_metrics.increment("presentation_tasks_failed")
    except Exception as exc:
        runtime_metrics.increment("presentation_tasks_failed")
        runtime_metrics.record_error(f"presentation task failed: {exc}")
    finally:
        with presentation_thread_lock:
            presentation_threads.pop(task_id, None)


def _start_presentation_thread(task_id: str, *, suppress_wps: bool = False) -> bool:
    with presentation_thread_lock:
        existing = presentation_threads.get(task_id)
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(
            target=_run_presentation_task,
            args=(task_id, suppress_wps),
            daemon=True,
            name=f"presentation-{task_id[-8:]}",
        )
        presentation_threads[task_id] = thread
        thread.start()
        return True


@app.get("/presentations/sources")
async def presentation_sources():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    library = _document_library_payload(current_workspace())
    recent_days = 7
    cutoff = time.time() - recent_days * 86400
    return {
        "recent_days": recent_days,
        "sources": [
            {**document, "recent": float(document.get("modified_at") or 0) >= cutoff}
            for document in library["documents"]
            if document.get("indexed")
        ]
    }


@app.post("/presentations/generate")
async def presentation_generate(request: PresentationGenerateRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        service = current_presentation_service()
        payload = request.model_dump()
        project_id = str(payload.get("project_id") or "").strip() or None
        if project_id:
            workspace = current_workspace()
            catalog = ProjectCatalog(workspace)
            try:
                if catalog.conn.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone() is None:
                    return JSONResponse({"error": "unknown project"}, status_code=404)
                payload["source_version_ids"] = _verified_source_version_ids(
                    catalog,
                    project_id,
                    payload.get("source_version_ids") or [],
                )
            finally:
                catalog.close()
        task = service.create_plan(payload)
        replayed = bool(task.get("replayed"))
        runtime_metrics.increment("presentation_tasks_started")
        public_task = service.get_task(task["task_id"])
        started = (
            False
            if public_task.get("status") in {"completed", "cancelled"}
            else _start_presentation_thread(task["task_id"])
        )
        return JSONResponse(
            {**public_task, "replayed": replayed, "worker_started": started},
            status_code=202,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/presentations/tasks/{task_id}/confirm")
async def presentation_confirm(
    task_id: str,
    request: PresentationConfirmRequest,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        service = current_presentation_service()
        existing = service.get_task(task_id)
        scope_response = _presentation_task_scope_response(existing, project_id)
        if scope_response is not None:
            return scope_response
        task = service.confirm_plan(task_id, request.plan_digest)
        started = (
            False
            if task.get("status") in {"completed", "cancelled"}
            else _start_presentation_thread(task["task_id"])
        )
        return JSONResponse({**task, "worker_started": started}, status_code=202)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.get("/presentations/tasks")
async def presentation_tasks(
    limit: int = 30,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    service = current_presentation_service()
    normalized_project_id = str(project_id or "").strip() or None
    tasks = service.list_tasks(
        limit=max(1, min(limit, 100)),
        project_id=normalized_project_id,
        global_only=normalized_project_id is None,
    )
    return {"tasks": tasks}


@app.get("/presentations/tasks/{task_id}")
async def presentation_task(
    task_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        task = current_presentation_service().get_task(task_id)
        scope_response = _presentation_task_scope_response(task, project_id)
        if scope_response is not None:
            return scope_response
        return JSONResponse(task, headers={"Cache-Control": "no-store"})
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/presentations/tasks/{task_id}/resume")
async def presentation_resume(
    task_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        service = current_presentation_service()
        task = service.get_task(task_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    scope_response = _presentation_task_scope_response(task, project_id)
    if scope_response is not None:
        return scope_response
    if task["status"] == "completed":
        return task
    if task["status"] == "cancelled":
        return JSONResponse({"error": "cancelled task cannot be resumed", **task}, status_code=409)
    if task.get("kind") == "presentation.generate" and not task.get("plan_task_id"):
        return JSONResponse({"error": "未确认的旧 PPT 任务不能继续，请先生成并确认新的大纲"}, status_code=409)
    started = _start_presentation_thread(task_id, suppress_wps=True)
    return JSONResponse({**task, "worker_started": started}, status_code=202 if started else 409)


@app.post("/presentations/tasks/{task_id}/cancel")
async def presentation_cancel(
    task_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        service = current_presentation_service()
        task = service.get_task(task_id)
        scope_response = _presentation_task_scope_response(task, project_id)
        if scope_response is not None:
            return scope_response
        return service.request_cancel(task_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/papers/list")
async def papers_list():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    payload = _document_library_payload(current_workspace())
    payload["paper_count"] = payload["document_count"]
    payload["papers"] = payload["documents"]
    return payload


@app.post("/papers/import")
async def papers_import(request: PaperImportRequest):
    return _import_documents(request, allowed_extensions=SUPPORTED_PAPER_EXTENSIONS)


@app.post("/index/run")
async def index_run(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    _reconcile_indexer_state()
    state = get_runtime_state()
    workspace = current_workspace()
    auth_level = str(state.get("auth_level") or "PRO")
    normalized_key = str(idempotency_key or "").strip() or None
    if normalized_key and len(normalized_key) > 256:
        return JSONResponse({"error": "idempotency key exceeds 256 characters"}, status_code=422)

    journal = None
    run_id = str(uuid.uuid4())
    receipt = None
    if normalized_key:
        journal = IndexRunJournal(workspace)
        _sync_index_run_receipt(state)
        try:
            receipt = journal.create_or_get(
                normalized_key,
                index_run_input_digest(workspace.root_path, auth_level),
                run_id,
            )
        except IndexRunInputConflict as exc:
            journal.close()
            return JSONResponse({"error": str(exc)}, status_code=409)
        run_id = str(receipt["run_id"])
        receipt_status = str(receipt.get("status") or "")
        if receipt_status in {"completed", "failed"}:
            journal.close()
            replay_payload = {
                "started": False,
                "replayed": True,
                "status": receipt_status,
                "run_id": run_id,
            }
            if receipt_status == "failed":
                replay_payload.update(
                    {
                        "error": "索引任务失败，请查看运行诊断。",
                        "error_observation": index_run_error_observation(receipt.get("error")),
                    }
                )
            return JSONResponse(
                replay_payload,
                status_code=200,
            )
        if _indexer_worker_is_active() and str((state.get("indexing") or {}).get("run_id") or "") == run_id:
            journal.close()
            return JSONResponse(
                {
                    "started": False,
                    "replayed": True,
                    "status": receipt_status or "in_flight",
                    "run_id": run_id,
                },
                status_code=202,
            )

    if _indexer_worker_is_active():
        if journal is not None:
            journal.close()
        return JSONResponse(
            {"started": False, "status": "already_running"},
            status_code=409,
        )

    try:
        process = _start_indexer_worker(
            workspace,
            auth_level,
            run_id=run_id,
            idempotency_key=normalized_key,
        )
    except Exception as exc:
        if journal is not None and normalized_key:
            journal.update(normalized_key, status="failed", error="indexer worker failed to start")
            journal.close()
        update_indexing_status(
            status="failed",
            run_id=run_id,
            idempotency_key=normalized_key,
            finished_at=time.time(),
        )
        runtime_metrics.record_error(f"indexer start failed: {exc}")
        return JSONResponse({"error": "indexer worker failed to start"}, status_code=500)

    if process is None:
        if journal is not None:
            journal.close()
        return JSONResponse(
            {"started": False, "status": "already_running"},
            status_code=409,
        )

    update_indexing_status(
        status="queued",
        run_id=run_id,
        idempotency_key=normalized_key,
        **_indexer_worker_identity(process),
        current_file=None,
        started_at=time.time(),
        finished_at=None,
    )
    if journal is not None and normalized_key:
        journal.update(normalized_key, status="queued")
        journal.close()
    return JSONResponse(
        {
            "started": True,
            "replayed": False,
            "status": "queued",
            "run_id": run_id,
            "workspace": "SoulDrive workspace mounted",
        },
        status_code=202,
    )


@app.get("/index/preview")
async def index_preview():
    """Return a read-only local source/index manifest diff."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    _reconcile_indexer_state()
    return _index_preview_payload(current_workspace())


@app.get("/diagnostics/backup")
async def diagnostics_backup():
    """Return a read-only backup/restore preflight for the active workspace."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return build_backup_preflight(
        current_workspace(),
        runtime_state=get_runtime_state(),
        worker_active=_indexer_worker_is_active(),
    )

@app.get("/metrics")
async def metrics():
    return runtime_metrics.snapshot()

@app.get("/diagnostics/product")
async def diagnostics_product():
    return product_diagnostics()


@app.get("/diagnostics/graphrag")
async def diagnostics_graphrag(project_id: str | None = Query(default=None, max_length=128)):
    """只读取 GraphRAG 派生索引，不加载 embedding、聊天模型或用户记忆。"""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    project_id = str(project_id or "").strip() or None
    if project_id and not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        graph_store = SecureGraphStore(workspace.secure_graph_store_path, workspace_keys)
    else:
        graph_store = LocalGraphDB(workspace.graph_db_path)
    try:
        from core.graph_rag import GraphRAGIndex

        scope_metadata_provider = None
        if kb is not None and str(loaded_workspace_path or "") == str(workspace.root_path):
            candidate_provider = getattr(kb, "metadata_for_chunk_ids", None)
            if callable(candidate_provider):
                scope_metadata_provider = candidate_provider
        graph_index = GraphRAGIndex(
            graph_store,
            scope_metadata_provider=scope_metadata_provider,
        )
        diagnostics = graph_index.diagnostics(project_id=project_id)
        state = get_runtime_state()
        indexing = state.get("indexing") or {}
        indexing_status = str(indexing.get("status") or "idle")
        graph_observation = (indexing.get("observability") or {}).get("graphrag") or {}
        if not project_id:
            # A test double or legacy adapter may return a shared dict object
            # for repeated diagnostics calls; isolate it before nesting it in
            # the primary response to avoid self-referential JSON.
            all_projects_diagnostics = dict(
                graph_index.diagnostics(index_name="all_projects")
            )
            all_projects_observation = graph_observation.get("all_projects") or {}
            all_projects_diagnostics["build_observation"] = dict(all_projects_observation)
            all_projects_diagnostics["indexing_status"] = indexing_status
            all_projects_observed_status = str(all_projects_observation.get("status") or "")
            if indexing_status in {"queued", "scanning", "indexing"}:
                all_projects_diagnostics.update(
                    ready=False,
                    status="building",
                    reason="graphrag_index_building",
                )
            elif all_projects_observed_status == "degraded":
                all_projects_diagnostics.update(
                    status="degraded",
                    reason="graphrag_index_build_degraded",
                )
            elif all_projects_observed_status == "failed" and not all_projects_diagnostics.get("ready"):
                all_projects_diagnostics.update(
                    status="failed",
                    reason="graphrag_index_build_failed",
                )
            elif all_projects_observed_status == "skipped" and not all_projects_diagnostics.get("ready"):
                all_projects_diagnostics.update(
                    status="disabled",
                    reason=str(all_projects_observation.get("reason") or "all_projects_index_skipped"),
                )
            elif all_projects_observed_status == "completed" and not all_projects_diagnostics.get("ready"):
                all_projects_diagnostics.update(
                    status="not_built",
                    reason="graphrag_index_missing_runtime_state_stale",
                    observation_stale=True,
                )
            diagnostics["all_projects"] = all_projects_diagnostics
        if indexing_status in {"queued", "scanning", "indexing"}:
            diagnostics.update(
                ready=False,
                status="building",
                reason="graphrag_index_building",
            )
        else:
            project_observation = next(
                (
                    item
                    for item in graph_observation.get("project_indexes") or []
                    if str(item.get("project_id") or "") == str(project_id or "")
                ),
                None,
            ) if project_id else None
            observed = project_observation or graph_observation
            observed_status = str(observed.get("status") or "")
            if observed_status == "degraded":
                diagnostics.update(
                    status="degraded",
                    reason="graphrag_index_build_degraded",
                    failed_project_ids=list(graph_observation.get("failed_project_ids") or []),
                )
            elif observed_status == "failed" and not diagnostics.get("ready"):
                observed_error = str(observed.get("error") or "")
                error_observation = {
                    "error_type": str(observed.get("error_type") or "UnknownError"),
                    "error_digest": str(observed.get("error_digest") or ""),
                    "error_chars": observed.get("error_chars"),
                }
                if observed_error and not error_observation["error_digest"]:
                    error_observation = _message_observation(
                        observed_error,
                        str(observed.get("error_type") or "UnknownError"),
                    )
                diagnostics.update(
                    status="failed",
                    reason="graphrag_index_build_failed",
                    **error_observation,
                )
            elif observed_status == "skipped" and not diagnostics.get("ready"):
                diagnostics.update(
                    status="disabled",
                    reason=str(observed.get("reason") or "auth_level_not_graph_enabled"),
                )
            elif diagnostics.get("status") == "unavailable":
                diagnostics["reason"] = diagnostics.get("reason") or (
                    "graphrag_diagnostic_unavailable"
                )
            elif diagnostics.get("status") == "unverified":
                diagnostics["reason"] = diagnostics.get("reason") or (
                    "graphrag_scope_metadata_unverified"
                )
            elif observed_status == "completed" and not diagnostics.get("ready"):
                diagnostics.update(
                    status="not_built",
                    reason="graphrag_index_missing_runtime_state_stale",
                    observation_stale=True,
                )
            elif diagnostics.get("ready"):
                diagnostics["status"] = "ready"
            else:
                diagnostics["status"] = "not_built"
            if not diagnostics.get("ready") and diagnostics.get("status") == "not_built":
                workspace_key = str(workspace.root_path)
                discovered_files = int(indexing.get("discovered_files") or 0)
                if discovered_files > 0 and workspace_key not in _graphrag_auto_start_attempted:
                    _graphrag_auto_start_attempted.add(workspace_key)
                    try:
                        process = _start_indexer_worker(workspace, str(state.get("auth_level") or "PRO"))
                        if process is not None:
                            diagnostics.update(status="building", reason="graphrag_auto_build_started")
                            indexing_status = "queued"
                    except Exception as exc:
                        logger.warning("[GraphRAG] automatic build start failed: %s", exc)
        diagnostics["indexing_status"] = indexing_status
        return diagnostics
    finally:
        graph_store.close()


def _retrieval_test_chunks(result: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = list(result.get("evidence") or [])
    documents = list(result.get("documents") or [])
    metadatas = list(result.get("metadatas") or [])
    chunks = []
    for index, raw_item in enumerate(evidence):
        item = dict(raw_item)
        metadata = dict(metadatas[index]) if index < len(metadatas) else {}
        content = str(
            documents[index]
            if index < len(documents) and documents[index]
            else item.get("context") or item.get("snippet") or ""
        ).strip()
        chunks.append({
            "id": str(item.get("id") or f"E{index + 1}"),
            "candidate_id": item.get("candidate_id") or metadata.get("child_id"),
            "source_filename": item.get("source_filename") or metadata.get("source_filename") or "未知文件",
            "document_title": item.get("document_title") or metadata.get("document_title"),
            "page": item.get("page") or metadata.get("page_number") or metadata.get("page"),
            "section": item.get("section") or metadata.get("section") or "未标注章节",
            "section_path": item.get("section_path") or metadata.get("section_path") or [],
            "chunk_index": item.get("chunk_index") or metadata.get("chunk_index"),
            "content_kind": item.get("content_kind") or metadata.get("content_kind") or "prose",
            "score": item.get("score"),
            "breakdown": item.get("breakdown") or {},
            "support_score": item.get("support_score"),
            "support_summary": item.get("support_summary") or "",
            "support_terms": item.get("support_terms") or [],
            "section_route_mode": item.get("section_route_mode"),
            "section_route_coverage": item.get("section_route_coverage"),
            "section_route_source": item.get("section_route_source") or metadata.get("_section_route_source"),
            "section_node_id": metadata.get("section_node_id"),
            "section_parent_node_id": metadata.get("section_parent_node_id"),
            "section_node_depth": metadata.get("section_node_depth"),
            "section_node_title": metadata.get("section_node_title"),
            "section_node_summary": metadata.get("section_node_summary"),
            "section_node_child_count": metadata.get("section_node_child_count"),
            "content": content[:2400],
            "truncated": len(content) > 2400,
        })
    return chunks


def _retrieval_feedback_for_query(
    query: str,
    project_id: str | None = None,
    workflow_id: str | None = None,
) -> list[dict[str, Any]]:
    try:
        store = current_learning_store()
        try:
            return store.list_retrieval_feedback(
                query,
                project_id=project_id,
                workflow_id=workflow_id,
            )
        finally:
            store.close()
    except Exception as exc:
        runtime_metrics.record_error(f"retrieval feedback unavailable: {exc}")
        return []


def _run_retrieval_test(
    query: str,
    top_k: int,
    project_id: str | None,
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    knowledge_base, _workspace = ensure_knowledge_base_loaded()
    started_at = time.perf_counter()
    feedback = _retrieval_feedback_for_query(
        query,
        project_id=project_id,
        workflow_id=workflow_id,
    )
    search_kwargs = {
        "query": query,
        "top_k": top_k,
        "project_id": project_id,
        # An unbound diagnostic is a workspace-global read, not an implicit
        # all-project read.  Keep the scope explicit so legacy backends that
        # cannot enforce it fail closed at this boundary.
        "global_only": not bool(project_id),
    }
    feedback_variants = [
        str(item.get("corrected_query") or "").strip()
        for item in feedback
        if str(item.get("corrected_query") or "").strip()
    ][:3]
    workflow_variants = [
        " ".join(str(item or "").split())[:6000]
        for item in (workflow_retrieval_variants(workflow_id, query) if workflow_id else [])
        if " ".join(str(item or "").split())
    ]
    extra_query_variants: list[str] = []
    for variant in [*feedback_variants, *workflow_variants]:
        if variant and variant not in extra_query_variants:
            extra_query_variants.append(variant)
        if len(extra_query_variants) >= 5:
            break
    applied_workflow_variant_count = sum(
        1 for variant in workflow_variants if variant in extra_query_variants
    )
    if extra_query_variants:
        search_kwargs["extra_query_variants"] = extra_query_variants
    try:
        result = knowledge_base.search_with_evidence(**search_kwargs)
    except TypeError as exc:
        if "extra_query_variants" not in str(exc) or not (feedback_variants or workflow_variants):
            raise
        search_kwargs.pop("extra_query_variants", None)
        applied_workflow_variant_count = 0
        result = knowledge_base.search_with_evidence(**search_kwargs)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    observability = dict(result.get("observability") or {})
    project_focus = dict(observability.get("project_focus") or {})
    return {
        "schema_version": "retrieval_test_v1",
        "query": query,
        "project_id": project_id,
        "workflow_id": workflow_id,
        "top_k": top_k,
        "elapsed_ms": elapsed_ms,
        "retrieval_mode": result.get("retrieval_mode"),
        "project_route": project_focus.get("route") or "global",
        "query_variants": expand_query_variants(query, limit=5),
        "workflow_retrieval_variant_count": applied_workflow_variant_count,
        "feedback": [
            {
                "feedback_id": item.get("feedback_id"),
                "scope": item.get("scope"),
                "project_id": item.get("project_id"),
                "workflow_id": item.get("workflow_id"),
                "corrected_query": item.get("corrected_query"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in feedback
        ],
        "chunks": _retrieval_test_chunks(result),
        "observability": {
            "stage_counts": observability.get("stage_counts") or {},
            "document_scope": observability.get("document_scope") or {},
            "ranking": observability.get("ranking") or {},
            "section_route": observability.get("section_route") or [],
            "project_focus": project_focus,
            "quality_signals": observability.get("quality_signals") or {},
            "sufficiency": observability.get("sufficiency") or {},
            "timings_ms": observability.get("timings_ms") or {},
        },
    }


@app.post("/diagnostics/retrieval")
async def diagnostics_retrieval(request: RetrievalTestRequest):
    """Run a bounded, read-only retrieval test without loading the chat model."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    _reconcile_indexer_state()
    indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
    if indexing_status in {"queued", "scanning", "indexing"}:
        return JSONResponse({"error": "知识索引正在更新，请完成后再测试检索"}, status_code=409)

    workspace = current_workspace()
    project_id = str(request.project_id or "").strip() or None
    if project_id and not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)

    query = request.query.strip()
    if not query:
        return JSONResponse({"error": "query must not be blank"}, status_code=422)
    try:
        payload = await run_in_threadpool(
            _run_retrieval_test,
            query,
            int(request.top_k),
            project_id,
            workflow_id=request.workflow_id,
        )
    except Exception as exc:
        runtime_metrics.record_error("retrieval diagnostic failed")
        warning = _append_audit_best_effort(
            "retrieval.diagnostics_failed",
            {"query_chars": len(query), "project_id": project_id, "error_type": type(exc).__name__},
        )
        error_payload = {"error": "检索测试暂不可用，请检查本地索引和模型"}
        if warning:
            error_payload["warning"] = warning
        return JSONResponse(error_payload, status_code=503)

    warning = _append_audit_best_effort(
        "retrieval.diagnostics_test",
        {
            "query_chars": len(query),
            "project_id": project_id,
            "workflow_id": request.workflow_id,
            "top_k": int(request.top_k),
            "chunk_count": len(payload["chunks"]),
            "project_route": payload["project_route"],
        },
    )
    if warning:
        payload["warning"] = warning
    return payload


@app.post("/diagnostics/retrieval/feedback")
async def diagnostics_retrieval_feedback(request: RetrievalFeedbackRequest):
    """Save one user-confirmed retrieval correction; it never writes the document index."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    requested_project_id = str(request.project_id or "").strip() or None
    project_id = requested_project_id if request.scope == "project" else None
    if project_id and not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)
    try:
        store = current_learning_store()
        try:
            feedback = store.save_retrieval_feedback(
                request.query,
                request.corrected_query,
                scope=request.scope,
                project_id=project_id,
                workflow_id=request.workflow_id,
            )
        finally:
            store.close()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    warning = _append_audit_best_effort(
        "retrieval.feedback_saved",
        {
            "feedback_id": feedback["feedback_id"],
            "query_chars": len(request.query.strip()),
            "corrected_query_chars": len(request.corrected_query.strip()),
            "scope": feedback["scope"],
            "project_id": feedback["project_id"],
            "workflow_id": feedback["workflow_id"],
        },
    )
    payload = {"status": "saved", "feedback": feedback}
    if warning:
        payload["warning"] = warning
    return payload


@app.delete("/diagnostics/retrieval/feedback/{feedback_id}")
async def diagnostics_retrieval_feedback_revoke(
    feedback_id: str,
    confirm: bool = Query(default=False),
    project_id: str | None = Query(default=None, max_length=128),
):
    """Revoke one confirmed correction so it is no longer used by retrieval."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    if not confirm:
        return JSONResponse({"error": "explicit confirm=true is required to revoke feedback"}, status_code=400)

    requested_project_id = str(project_id or "").strip() or None
    if requested_project_id and not _project_exists(current_workspace(), requested_project_id):
        return JSONResponse({"error": "unknown project"}, status_code=404)

    store = current_learning_store()
    try:
        try:
            feedback = store.revoke_retrieval_feedback(
                feedback_id,
                project_id=requested_project_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        store.close()
    if feedback is None:
        return JSONResponse({"error": "feedback not found or already revoked"}, status_code=404)
    warning = _append_audit_best_effort(
        "retrieval.feedback_revoked",
        {
            "feedback_id": feedback["feedback_id"],
            "scope": feedback["scope"],
            "project_id": feedback["project_id"],
            "workflow_id": feedback["workflow_id"],
        },
    )
    payload = {"status": "revoked", "feedback": feedback}
    if warning:
        payload["warning"] = warning
    return payload


def workspace_diagnostics():
    state = get_runtime_state()
    if state.get("workspace_path"):
        workspace = SoulDriveWorkspace(state["workspace_path"]).ensure()
    elif state.get("active_drive"):
        workspace = resolve_workspace(state.get("active_drive"))
    else:
        return {
            "ready": False,
            "reason": "waiting for removable SoulDrive workspace",
        }
    return workspace.diagnose()


def audit_verify_report(limit: int | None = None):
    safe_limit = None
    if limit is not None:
        safe_limit = max(1, min(int(limit), 10000))
    return current_audit_logger().verify_chain(limit=safe_limit)


def _indexed_source_filenames(workspace: SoulDriveWorkspace) -> set[str]:
    sources = set()
    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None and Path(workspace.secure_vector_store_path).exists():
        try:
            store = SecureVectorStore(workspace.secure_vector_store_path, workspace_keys)
            try:
                payload = store.collection.get(include=["metadatas"])
                for metadata in payload.get("metadatas") or []:
                    if metadata and metadata.get("source_filename"):
                        sources.add(str(metadata["source_filename"]))
            finally:
                store.close()
        except Exception:
            pass

    db_path = Path(workspace.parent_doc_path)
    if not db_path.exists():
        return sources

    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("""
                SELECT DISTINCT json_extract(metadata_json, '$.source_filename')
                FROM parent_documents
                WHERE json_extract(metadata_json, '$.source_filename') IS NOT NULL
            """).fetchall()
    except Exception:
        return sources

    sources.update(str(row[0]) for row in rows if row and row[0])
    return sources


def _indexed_document_ids(workspace: SoulDriveWorkspace) -> set[str]:
    """Return explicit document identities persisted in derived indexes."""
    document_ids: set[str] = set()

    def add_metadata(metadata: dict[str, Any] | None) -> None:
        document_id = str((metadata or {}).get("document_id") or "").strip()
        if document_id:
            document_ids.add(document_id)

    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None and Path(workspace.secure_vector_store_path).exists():
        try:
            store = SecureVectorStore(workspace.secure_vector_store_path, workspace_keys)
            try:
                payload = store.collection.get(include=["metadatas"])
                for metadata in payload.get("metadatas") or []:
                    add_metadata(metadata)
            finally:
                store.close()
        except Exception:
            pass

    db_path = Path(workspace.parent_doc_path)
    if not db_path.exists():
        return document_ids
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT metadata_json FROM parent_documents WHERE metadata_json IS NOT NULL"
            ).fetchall()
    except Exception:
        return document_ids
    for row in rows:
        try:
            add_metadata(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return document_ids


def _indexed_source_hashes(workspace: SoulDriveWorkspace) -> dict[str | tuple[str, str], set[str]]:
    """Return persisted per-source content hashes for read-only freshness checks."""
    source_hashes: dict[str | tuple[str, str], set[str]] = {}

    def add_metadata(metadata: dict[str, Any] | None) -> None:
        metadata = metadata or {}
        source_filename = str(metadata.get("source_filename") or "").strip()
        document_id = str(metadata.get("document_id") or "").strip()
        document_hash = str(metadata.get("document_hash") or "").strip()
        if source_filename and document_hash:
            key = ("id", document_id) if document_id else source_filename
            source_hashes.setdefault(key, set()).add(document_hash)

    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None and Path(workspace.secure_vector_store_path).exists():
        try:
            store = SecureVectorStore(workspace.secure_vector_store_path, workspace_keys)
            try:
                payload = store.collection.get(include=["metadatas"])
                for metadata in payload.get("metadatas") or []:
                    add_metadata(metadata)
            finally:
                store.close()
        except Exception:
            pass

    db_path = Path(workspace.parent_doc_path)
    if not db_path.exists():
        return source_hashes
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT metadata_json FROM parent_documents WHERE metadata_json IS NOT NULL"
            ).fetchall()
    except Exception:
        return source_hashes
    for row in rows:
        try:
            add_metadata(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError):
            continue
    return source_hashes


def _current_source_hash(workspace: SoulDriveWorkspace, document: dict[str, Any]) -> str | None:
    """Hash an editable workspace source without changing an index or catalog."""
    # SecureDocumentStore entries are content-addressed and cannot be edited
    # in place.  Their index metadata may also predate the current hash
    # contract, so an arbitrary mismatch is not evidence of source drift.
    if document.get("_secure_object_id"):
        return None
    name = str(document.get("name") or "").strip()

    documents_dir = Path(workspace.papers_path).resolve()
    relative_path = str(document.get("relative_path") or name).strip()
    source_path = (documents_dir / relative_path).resolve()
    try:
        source_path.relative_to(documents_dir)
    except ValueError:
        return None
    if not source_path.is_file() or source_path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
        return None
    try:
        return file_md5(source_path)
    except OSError:
        return None


def _indexed_document_preview(
    workspace: SoulDriveWorkspace,
    source_filename: str,
    *,
    project_id: str | None = None,
    document_id: str | None = None,
) -> dict:
    """Read cleaned parent chunks only inside the requested document scope."""
    records: list[dict] = []
    scope_error = False
    foreign_scope_seen = False
    scoped_project_id = str(project_id or "").strip()
    selected_document_ids = _document_id_candidates(document_id)

    def is_selected_document(metadata: dict[str, Any]) -> bool:
        metadata_source = str(metadata.get("source_filename") or "").strip()
        if metadata_source != source_filename:
            return False
        if not selected_document_ids:
            return True
        return str(metadata.get("document_id") or "").strip() in selected_document_ids

    def add_if_in_scope(record: dict) -> None:
        nonlocal foreign_scope_seen, scope_error
        metadata = record.get("metadata") or {}
        metadata_project_id = str(metadata.get("project_id") or "").strip()
        if metadata_project_id and not scoped_project_id:
            scope_error = True
            return
        if scoped_project_id and metadata_project_id and metadata_project_id != scoped_project_id:
            foreign_scope_seen = True
            return
        records.append(record)

    keys = get_workspace_keys(workspace.root_path)
    if keys is not None and Path(workspace.secure_vector_store_path).exists():
        store = SecureVectorStore(workspace.secure_vector_store_path, keys)
        try:
            parent_ids = [row[0] for row in store.conn.execute(
                "SELECT parent_id FROM parent_documents ORDER BY parent_id"
            ).fetchall()]
            for parent_id in parent_ids:
                parent = store.get_parent(parent_id)
                if parent and is_selected_document(parent["metadata"]):
                    add_if_in_scope({"parent_id": parent_id, **parent})
        finally:
            store.close()
    if not records:
        db_path = Path(workspace.parent_doc_path)
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT parent_id, content, metadata_json
                    FROM parent_documents
                    WHERE json_extract(metadata_json, '$.source_filename') = ?
                    ORDER BY CAST(json_extract(metadata_json, '$.parent_index') AS INTEGER), parent_id
                    """,
                    (source_filename,),
                ).fetchall()
            for row in rows:
                metadata = json.loads(row[2])
                if is_selected_document(metadata):
                    add_if_in_scope({"parent_id": row[0], "content": row[1], "metadata": metadata})

    if foreign_scope_seen and not records:
        scope_error = True
    records.sort(key=lambda record: int(record["metadata"].get("parent_index") or 0))
    chunks = []
    remaining_chars = 30000
    for index, record in enumerate(records, start=1):
        metadata = record["metadata"]
        content = record["content"]
        if remaining_chars <= 0:
            break
        displayed_content = content[:remaining_chars]
        remaining_chars -= len(displayed_content)
        chunks.append({
            "id": record["parent_id"],
            "index": int(metadata.get("parent_index") or index - 1) + 1,
            "page": metadata.get("page_number") or metadata.get("page"),
            "section": metadata.get("section") or "未标注章节",
            "content_kind": metadata.get("content_kind") or "prose",
            "child_count": len(metadata.get("child_ids") or []),
            "text": displayed_content,
        })

    full_text = "\n\n".join(
        f"## 切片 {chunk['index']}" + (f" · 第 {chunk['page']} 页" if chunk["page"] else "")
        + f"\n\n{chunk['text']}"
        for chunk in chunks
    )
    result = {
        "text": full_text[:30000],
        "chunks": chunks,
        "truncated": len(chunks) < len(records) or any(
            len(chunk["text"]) < len(record["content"])
            for chunk, record in zip(chunks, records)
        ),
    }
    if scope_error:
        result["scope_error"] = "文档存在项目绑定内容，请先选择对应项目后再预览"
    return result


def _indexing_failure_phase(failure: dict) -> str:
    category = str(failure.get("category") or "unknown")
    if category == "parser":
        return "parse"
    if category == "filesystem":
        return "read"
    if category == "resource":
        return "runtime"
    return "index"


def _bounded_indexing_reason(reason: object) -> str:
    bounded = str(reason or "索引失败")[:500]
    return re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n'\"<>]+", "<local-path>", bounded)


def _indexing_diagnostics_by_source() -> dict[tuple[str, str, str], dict]:
    """Bind diagnostics to document/project identity; retain only unambiguous legacy names."""
    indexing = get_runtime_state().get("indexing") or {}
    diagnostics: dict[tuple[str, str, str], dict] = {}

    def identity_key(item):
        document_id = str(item.get("document_id") or "").strip()
        project_id = str(item.get("project_id") or "").strip()
        if document_id:
            return "id", document_id, project_id
        source_filename = _public_source_filename(item.get("source_filename"))
        return ("name", source_filename, project_id) if source_filename else None

    legacy_observations = Counter()
    legacy_failures = Counter()
    observation_fields = (
        "status",
        "elapsed_ms",
        "parse_ms",
        "ingest_ms",
        "graph_ms",
        "chunk_count",
        "chunk_chars",
        "modality",
        "pages",
        "ocr_ms",
        "ocr_region_count",
        "ocr_mean_confidence",
        "conversion_status",
        "conversion_error_count",
        "converted_pages",
        "failed_page_count",
    )
    observations = ((indexing.get("observability") or {}).get("documents") or [])
    for item in observations:
        key = identity_key(item)
        source_filename = _public_source_filename(item.get("source_filename"))
        if key is None:
            continue
        if key[0] == "name":
            legacy_observations[key] += 1
        diagnostics[key] = {
            "source_filename": source_filename,
            "index_status": str(item.get("status") or "").strip() or None,
            "index_failure": None,
            "index_observation": {
                key: item[key]
                for key in observation_fields
                if key in item
            },
        }

    for failure in indexing.get("failures") or []:
        key = identity_key(failure)
        source_filename = _public_source_filename(failure.get("source_filename"))
        if key is None:
            continue
        if key[0] == "name":
            legacy_failures[key] += 1
        diagnostics.setdefault(key, {"source_filename": source_filename})
        diagnostics[key].update({
            "index_status": "failed",
            "index_failure": {
                "error_code": str(failure.get("error_code") or "INDEXING_FAILED"),
                "category": str(failure.get("category") or "unknown"),
                "phase": _indexing_failure_phase(failure),
                "reason": _PUBLIC_INDEX_FAILURE_REASONS.get(
                    str(failure.get("error_code") or "INDEXING_FAILED"),
                    "索引失败",
                ),
                "error_observation": _message_observation(str(failure.get("reason") or "")),
            },
        })

    current_file = str(indexing.get("current_file") or "").strip()
    if current_file and str(indexing.get("status") or "") in {"queued", "scanning", "indexing"}:
        key = identity_key({
            "source_filename": current_file,
            "document_id": indexing.get("current_document_id"),
            "project_id": indexing.get("current_project_id"),
        })
        diagnostics.setdefault(key, {"source_filename": _public_source_filename(current_file)})["index_status"] = "processing"
    return {
        key: value for key, value in diagnostics.items()
        if legacy_observations[key] <= 1 and legacy_failures[key] <= 1
    }


def _document_library_payload(workspace: SoulDriveWorkspace) -> dict:
    from core.indexer import DriveIndexer

    indexed_sources = _indexed_source_filenames(workspace)
    indexed_document_ids = _indexed_document_ids(workspace)
    indexed_source_hashes = _indexed_source_hashes(workspace)
    indexing_diagnostics = _indexing_diagnostics_by_source()
    identified_sources = {
        value["source_filename"] for key, value in indexing_diagnostics.items() if key[0] == "id"
    }
    documents = []
    catalog = ProjectCatalog(workspace)
    # Reuse the indexer's read-only source/catalog identity rules; no model or parser is created.
    identity_indexer = DriveIndexer()
    identity_indexer.workspace = workspace
    identity_indexer.project_catalog = catalog

    def scope_metadata(name: str, *, content_hash: str | None = None) -> dict[str, Any]:
        bindings = catalog.find_imported_document_bindings(name, content_hash=content_hash)
        project_ids = sorted({str(item["project_id"]) for item in bindings if item.get("project_id")})
        if not project_ids:
            return {"scope": "global", "project_ids": []}
        return {
            "scope": "project" if len(project_ids) == 1 else "ambiguous",
            "project_id": project_ids[0] if len(project_ids) == 1 else None,
            "project_ids": project_ids,
        }

    def index_metadata(name: str, document: dict[str, Any]) -> dict[str, Any]:
        secure_object_id = str(document.get("_secure_object_id") or "").strip()
        identifiers = [str(document["_diagnostic_document_id"])]
        if secure_object_id:
            identifiers.append(secure_object_id)
        if indexed_document_ids:
            index_present = bool(set(identifiers) & indexed_document_ids)
        else:
            # Legacy indexes may have only carried source_filename.
            index_present = source_counts[name] == 1 and name in indexed_sources
        project_id = str(document.get("project_id") or "")
        diagnostic = {}
        for identity in identifiers:
            diagnostic = indexing_diagnostics.get(("id", identity, project_id)) or indexing_diagnostics.get(("id", identity, "")) or {}
            if diagnostic:
                break
        if not diagnostic and source_counts[name] == 1 and name not in identified_sources:
            diagnostic = indexing_diagnostics.get(("name", name, project_id)) or indexing_diagnostics.get(("name", name, "")) or {}
        status = str(diagnostic.get("index_status") or "")
        current_hash = _current_source_hash(workspace, document) if index_present else None
        known_hashes = set()
        for identity in identifiers:
            known_hashes.update(indexed_source_hashes.get(("id", identity), set()))
        if not indexed_document_ids and source_counts[name] == 1:
            known_hashes.update(indexed_source_hashes.get(name) or set())
        source_changed = bool(current_hash and known_hashes and current_hash not in known_hashes)
        if not index_present:
            index_freshness = "unindexed"
        elif source_changed:
            index_freshness = "changed"
        elif current_hash and known_hashes:
            index_freshness = "current"
        else:
            index_freshness = "unverified"
        if status == "failed":
            index_status = "failed"
        elif status == "processing":
            index_status = "processing"
        elif source_changed:
            index_status = "changed"
        else:
            index_status = "indexed" if index_present else "pending"
        indexed = index_present and not source_changed
        return {
            "indexed": indexed,
            "index_present": index_present,
            "index_status": index_status,
            "index_failure": diagnostic.get("index_failure"),
            "index_observation": diagnostic.get("index_observation"),
            "source_changed": source_changed,
            "index_freshness": index_freshness,
        }

    try:
        workspace_keys = get_workspace_keys(workspace.root_path)
        if workspace_keys is not None:
            store = SecureDocumentStore(workspace, workspace_keys)
            try:
                for item in store.iter_documents():
                    object_id = str(item["object_id"])
                    try:
                        content_hash = hashlib.md5(store.read_document_bytes(object_id)).hexdigest()
                    except (OSError, ValueError):
                        content_hash = None
                    public_item = {
                        key: value
                        for key, value in item.items()
                        if key not in {"object_id", "content_digest"}
                    }
                    documents.append({
                        **public_item,
                        "document_id": object_id,
                        "_secure_object_id": object_id,
                        "_diagnostic_document_id": f"secure_{object_id}",
                        **scope_metadata(item["name"], content_hash=content_hash),
                    })
            finally:
                store.close()

        documents_dir = Path(workspace.papers_path)

        for path in sorted(documents_dir.rglob("*"), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
                continue
            stat = path.stat()
            try:
                content_hash = file_md5(path)
            except OSError:
                content_hash = None
            diagnostic_id, project_id, catalog_item, _ = identity_indexer._document_identity_for_source(
                str(path), path.name, content_hash
            )
            source_ref = identity_indexer._catalog_ref_for_document(str(path), path.name)
            scope = scope_metadata(source_ref, content_hash=catalog_item["content_hash"] if catalog_item else None)
            if scope["scope"] == "global" and project_id:
                scope = {"scope": "project", "project_id": project_id, "project_ids": [project_id]}
            documents.append({
                "name": path.name,
                "relative_path": path.relative_to(documents_dir).as_posix(),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "media_type": document_media_type(path.name),
                "_diagnostic_document_id": diagnostic_id,
                **scope,
            })

        source_counts = Counter(document["name"] for document in documents)
        for document in documents:
            document.update(index_metadata(document["name"], document))
            document.pop("_secure_object_id", None)
            document.pop("_diagnostic_document_id", None)

        return {
            "ready": True,
            "document_count": len(documents),
            "indexed_count": sum(1 for document in documents if document["indexed"]),
            "workspace": "SoulDrive workspace mounted",
            "documents": documents,
        }
    finally:
        catalog.close()


def _index_preview_payload(workspace: SoulDriveWorkspace) -> dict:
    """Build a bounded, read-only local manifest diff for the index UI."""
    library = _document_library_payload(workspace)
    indexed_names = _indexed_source_filenames(workspace)
    documents = list(library.get("documents") or [])
    current_names = {str(item.get("name") or "").strip() for item in documents}

    def entry(document: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(document.get("name") or ""),
            "relative_path": document.get("relative_path"),
            "index_status": document.get("index_status"),
            "index_freshness": document.get("index_freshness"),
            "index_present": bool(document.get("index_present")),
            "source_changed": bool(document.get("source_changed")),
        }

    added = []
    modified = []
    unchanged = []
    unverified = []
    blocked = []
    for document in sorted(documents, key=lambda item: str(item.get("name") or "").casefold()):
        status = str(document.get("index_status") or "")
        freshness = str(document.get("index_freshness") or "")
        if not document.get("index_present"):
            added.append(entry(document))
        elif document.get("source_changed"):
            modified.append(entry(document))
        elif status in {"failed", "processing"}:
            blocked.append(entry(document))
        elif freshness == "unverified":
            unverified.append(entry(document))
        else:
            unchanged.append(entry(document))

    deleted = [
        {"name": name, "index_status": "deleted", "index_present": True}
        for name in sorted(indexed_names - current_names, key=str.casefold)
    ]
    return {
        "schema_version": "souldrive_index_manifest_diff_v0.1",
        "read_only": True,
        "indexing_status": str((get_runtime_state().get("indexing") or {}).get("status") or "idle"),
        "counts": {
            "added": len(added),
            "modified": len(modified),
            "deleted": len(deleted),
            "unchanged": len(unchanged),
            "unverified": len(unverified),
            "blocked": len(blocked),
        },
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged,
        "unverified": unverified,
        "blocked": blocked,
    }


def _project_catalog_payload(workspace: SoulDriveWorkspace) -> dict:
    catalog = ProjectCatalog(workspace)
    try:
        rows = catalog.conn.execute(
            """
            SELECT p.project_id, p.name, p.status, p.project_mode, p.root_path,
                   p.instructions, p.memory_policy, p.created_at,
                   COUNT(DISTINCT a.asset_id) AS asset_count,
                   COUNT(DISTINCT e.experiment_id) AS experiment_count,
                   COUNT(DISTINCT d.decision_id) AS decision_count,
                   COUNT(DISTINCT t.task_id) AS task_count
            FROM projects p
            LEFT JOIN assets a ON a.project_id = p.project_id
            LEFT JOIN experiments e ON e.project_id = p.project_id
            LEFT JOIN decisions d ON d.project_id = p.project_id
            LEFT JOIN project_tasks t ON t.project_id = p.project_id
            GROUP BY p.project_id
             ORDER BY p.created_at DESC, p.name COLLATE NOCASE, p.project_id ASC
            """
        ).fetchall()
        return {"projects": [dict(row) for row in rows]}
    finally:
        catalog.close()


def _project_exists(workspace: SoulDriveWorkspace, project_id: str) -> bool:
    catalog = ProjectCatalog(workspace)
    try:
        return catalog.conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone() is not None
    finally:
        catalog.close()


def _unknown_project_response(
    workspace: SoulDriveWorkspace,
    project_id: str | None,
) -> JSONResponse | None:
    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id and not _project_exists(workspace, normalized_project_id):
        return JSONResponse(
            {"error": "unknown project", "project_id": normalized_project_id},
            status_code=404,
        )
    return None


def _verified_source_version_ids(
    catalog: ProjectCatalog,
    project_id: str | None,
    source_version_ids: list[str],
) -> list[str]:
    """Keep explicitly supplied source versions inside the selected project."""
    cleaned = []
    for value in source_version_ids or []:
        version_id = str(value or "").strip()
        if version_id and version_id not in cleaned:
            cleaned.append(version_id[:160])
    if not cleaned:
        return []
    placeholders = ",".join("?" for _ in cleaned)
    if project_id:
        rows = catalog.conn.execute(
            f"""
            SELECT v.version_id
            FROM asset_versions v
            JOIN assets a ON a.asset_id = v.asset_id
            WHERE a.project_id = ? AND v.version_id IN ({placeholders})
            """,
            [project_id, *cleaned],
        ).fetchall()
    else:
        rows = catalog.conn.execute(
            f"SELECT version_id FROM asset_versions WHERE version_id IN ({placeholders})",
            cleaned,
        ).fetchall()
    verified = {str(row[0]) for row in rows}
    if verified != set(cleaned):
        raise ValueError(
            "source_version_ids must belong to the selected project"
            if project_id else "source_version_ids contain unknown versions"
        )
    return cleaned


def _playbook_approval_for_project(
    workspace: SoulDriveWorkspace,
    project_id: str | None,
    playbook_id: str,
) -> dict[str, Any] | None:
    project_id = str(project_id or "").strip()
    if not project_id:
        return None
    catalog = ProjectCatalog(workspace)
    try:
        return catalog.get_playbook_approval(project_id, playbook_id)
    finally:
        catalog.close()


def _playbook_approvals_for_project(
    workspace: SoulDriveWorkspace,
    project_id: str | None,
) -> dict[str, dict[str, Any]]:
    project_id = str(project_id or "").strip()
    if not project_id:
        return {}
    catalog = ProjectCatalog(workspace)
    try:
        return {
            str(item["playbook_id"]): item
            for item in catalog.list_playbook_approvals(project_id)
        }
    finally:
        catalog.close()


def _validated_project_root(workspace: SoulDriveWorkspace, requested: str | None, project_id: str) -> Path:
    """Resolve a project folder without allowing the workspace itself to become a project."""
    candidate = Path(str(requested or "").strip()) if str(requested or "").strip() else Path(workspace.root_path) / "projects" / project_id
    candidate = candidate.expanduser().resolve()
    workspace_root = Path(workspace.root_path).resolve()
    if candidate == workspace_root:
        raise ValueError("项目目录不能是 SoulDrive 工作区根目录")
    try:
        workspace_root.relative_to(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("项目目录不能包含 SoulDrive 工作区")
    reserved = {
        (Path(workspace.root_path) / "data").resolve(),
        Path(workspace.runtime_path).resolve(),
        Path(workspace.index_path).resolve(),
    }
    if candidate in reserved:
        raise ValueError("项目目录不能使用 SoulDrive 的内部数据目录")
    return candidate


def _project_mode_for_id(workspace: SoulDriveWorkspace, project_id: str) -> str | None:
    catalog = ProjectCatalog(workspace)
    try:
        row = catalog.conn.execute(
            "SELECT project_mode FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return str(row["project_mode"]) if row else None
    finally:
        catalog.close()


def _load_explicit_experiment_comparison(
    workspace: SoulDriveWorkspace,
    project_id: str | None,
    active_context: ActiveInteractionContext | None,
) -> dict[str, Any] | None:
    """Resolve only an explicit run pair; never infer a baseline in /chat."""
    if active_context is None:
        return None
    baseline_run_id = str(active_context.baseline_run_id or "").strip()
    current_run_id = str(active_context.current_run_id or "").strip()
    if not baseline_run_id and not current_run_id:
        return None
    if not baseline_run_id or not current_run_id:
        return {
            "status": "needs_confirmation",
            "reason": "请同时选择基线运行和当前运行；系统不会默认猜测上个月运行。",
            "baseline_run_id": baseline_run_id or None,
            "current_run_id": current_run_id or None,
        }
    if not project_id:
        return {
            "status": "rejected",
            "reason": "实验比较必须绑定当前项目空间。",
            "baseline_run_id": baseline_run_id,
            "current_run_id": current_run_id,
        }

    catalog = ProjectCatalog(workspace)
    try:
        return compare_experiment_runs(
            catalog,
            baseline_run_id,
            current_run_id,
            project_id=project_id,
        )
    finally:
        catalog.close()


def _project_overview_payload(workspace: SoulDriveWorkspace, project_id: str) -> dict:
    catalog = ProjectCatalog(workspace)
    try:
        project = catalog.conn.execute(
            "SELECT project_id, name, status, project_mode, root_path, instructions, memory_policy, created_at FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        recent_runs = catalog.conn.execute(
            """
            SELECT r.run_id, e.project_id, e.name AS experiment_name, r.performed_at, r.dataset_version,
                   a.title AS evidence_title
            FROM experiment_runs r
            JOIN experiments e ON e.experiment_id = r.experiment_id
            LEFT JOIN asset_versions v ON v.version_id = r.evidence_version_id
            LEFT JOIN assets a ON a.asset_id = v.asset_id
            WHERE e.project_id = ?
            ORDER BY r.performed_at DESC, r.run_id ASC
            LIMIT 6
            """,
            (project_id,),
        ).fetchall()
        tasks = catalog.conn.execute(
            """
            SELECT task_id, project_id, title, due_at, status,
                   created_at, updated_at, started_at, completed_at
            FROM project_tasks
            WHERE project_id = ?
            ORDER BY COALESCE(due_at, '9999-12-31T00:00:00+00:00'), title, task_id
            LIMIT 8
            """,
            (project_id,),
        ).fetchall()
        decisions = catalog.conn.execute(
            """
            SELECT decision_id, project_id, statement, decided_at FROM decisions
            WHERE project_id = ? ORDER BY decided_at DESC, decision_id ASC LIMIT 4
            """,
            (project_id,),
        ).fetchall()
        asset_count = catalog.conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        asset_version_count = catalog.conn.execute(
            """
            SELECT COUNT(*) FROM asset_versions v
            JOIN assets a ON a.asset_id = v.asset_id
            WHERE a.project_id = ?
            """,
            (project_id,),
        ).fetchone()[0]
        unversioned_asset_count = catalog.conn.execute(
            """
            SELECT COUNT(*) FROM assets a
            LEFT JOIN asset_versions v ON v.asset_id = a.asset_id
            WHERE a.project_id = ? AND v.version_id IS NULL
            """,
            (project_id,),
        ).fetchone()[0]
        open_task_count = catalog.conn.execute(
            "SELECT COUNT(*) FROM project_tasks WHERE project_id = ? AND status NOT IN ('done', 'cancelled')",
            (project_id,),
        ).fetchone()[0]
        overdue_task_count = catalog.conn.execute(
            "SELECT COUNT(*) FROM project_tasks WHERE project_id = ? AND status NOT IN ('done', 'cancelled') AND due_at IS NOT NULL AND due_at < ?",
            (project_id, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
        ).fetchone()[0]
        session_records = []
        session_store = _conversation_store(workspace)
        if session_store is not None:
            session_records = session_store.list_sessions(project_id)
        durable_tasks = []
        workspace_keys = get_workspace_keys(workspace.root_path)
        if workspace_keys is not None:
            journal = TaskJournal(workspace, workspace_keys)
            try:
                durable_tasks = journal.list_tasks(project_id=project_id, limit=100)
            finally:
                journal.close()
        episodic_view = build_episodic_view(
            project_id=project_id,
            sessions=session_records,
            experiment_runs=[dict(row) for row in recent_runs],
            decisions=[dict(row) for row in decisions],
            durable_tasks=durable_tasks,
            project_tasks=[dict(row) for row in tasks],
        )
        return {
            "project": dict(project),
            "health": {
                "asset_count": int(asset_count),
                "asset_version_count": int(asset_version_count),
                "unversioned_asset_count": int(unversioned_asset_count),
                "open_task_count": int(open_task_count),
                "overdue_task_count": int(overdue_task_count),
            },
            "recent_runs": [dict(row) for row in recent_runs],
            "tasks": [dict(row) for row in tasks],
            "decisions": [dict(row) for row in decisions],
            "episodic_view": episodic_view,
        }
    finally:
        catalog.close()


def current_presentation_service() -> PresentationService:
    workspace = current_workspace()
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        raise RuntimeError("workspace keys are not available")
    return PresentationService(
        workspace,
        keys,
        audit_logger=current_audit_logger(),
        outline_generator=_presentation_outline_with_local_model,
        style_skill_lookup=_presentation_style_skill,
    )


def _presentation_style_skill(skill_id: str, project_id: str | None = None) -> dict | None:
    workspace = current_workspace()
    store = current_learning_store()
    try:
        # A Skill can be installed or edited locally between the UI list
        # refresh and PPT generation.  Sync at the final consumer boundary so
        # the selected identifier resolves against the current project files.
        sync_project_skills(store, workspace, project_id=project_id)
        get_skill = getattr(store, "get_skill", None)
        if callable(get_skill):
            try:
                skill = get_skill(skill_id, project_id=project_id, include_global=True)
            except TypeError as exc:
                if "unexpected keyword" not in str(exc):
                    raise
                skill = get_skill(skill_id)
                if skill is not None and not _skill_scope_matches(skill, project_id, include_global=True):
                    skill = None
        else:
            skill = next(
                (item for item in _list_scoped_skills(store, project_id=project_id, include_global=True)
                 if item["skill_id"] == skill_id),
                None,
            )
        if skill is None or skill.get("target") != "presentation" or skill.get("compatibility_status") != "verified":
            return None
        steps = skill.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        output_contract = skill.get("output_contract") or []
        if isinstance(output_contract, str):
            output_contract = [output_contract]
        success_criteria = skill.get("success_criteria") or []
        if isinstance(success_criteria, str):
            success_criteria = [success_criteria]
        return {
            "skill_id": skill["skill_id"],
            "title": skill.get("title") or "PPT 模板",
            "content": str(skill.get("content") or "")[:8000],
            "applicability": str(skill.get("applicability") or "")[:2000],
            "steps": [str(item) for item in steps[:40]],
            "output_contract": [str(item) for item in output_contract[:20]],
            "success_criteria": [str(item) for item in success_criteria[:20]],
            "source_path": str(skill.get("source_path") or ""),
            "presentation_style": skill.get("presentation_style") or {},
            "presentation_style_source": skill.get("presentation_style_source") or "",
            "presentation_contract": skill.get("presentation_contract") or {},
            "target": skill.get("target"),
            "compatibility_status": skill.get("compatibility_status"),
            "resource_manifest": [
                dict(item)
                for item in (skill.get("resource_manifest") or [])[:48]
                if isinstance(item, dict) and item.get("kind") != "script"
            ],
        }
    finally:
        store.close()


def _presentation_skill_prompt_is_safe(skill: Any) -> bool:
    if not isinstance(skill, dict):
        return False
    try:
        text = json.dumps(skill, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = " ".join(str(value or "") for value in skill.values())
    return not (
        contains_sensitive_memory(text)
        or contains_untrusted_memory_instruction(text)
    )


def _presentation_outline_with_local_model(records: list[dict], inputs: dict) -> dict:
    from core.presentation_service import (
        build_extractable_presentation_plan,
        presentation_skill_application,
    )

    # The deterministic evidence-bound plan remains the fallback and the validation baseline.
    raw_skill = inputs.get("style_skill") or {}
    selected_skill = raw_skill if _presentation_skill_prompt_is_safe(raw_skill) else {}
    effective_inputs = {**inputs, "style_skill": selected_skill}
    baseline = build_extractable_presentation_plan(records, effective_inputs)
    try:
        ensure_runtime_loaded()
    except Exception:
        return {**baseline, "outline_source": "extractive"}
    if rag is None:
        return {**baseline, "outline_source": "extractive"}

    skill_lines = [
        str(selected_skill.get("content") or "").strip(),
        str(selected_skill.get("applicability") or "").strip(),
    ]
    skill_lines.extend(
        str(step.get("instruction") or step.get("text") or step).strip()
        if isinstance(step, dict) else str(step).strip()
        for step in selected_skill.get("steps") or []
    )
    contract = selected_skill.get("presentation_contract") or {}
    if contract:
        skill_lines.append("本地 PPT 适配契约：" + json.dumps(contract, ensure_ascii=False, sort_keys=True))
    skill_context = "\n".join(line for line in skill_lines if line)[:10000]

    # Keep every selected source represented in the prompt.  The old global
    # first-24 slice could spend the whole budget on the first document, so a
    # multi-document outline was sometimes generated without evidence from
    # the later documents and then silently fell back to raw extraction.
    selected_sources = [str(source) for source in inputs.get("source_filenames") or []]
    records_by_source = {source: [] for source in selected_sources}
    for record in records:
        source = str(record.get("source_filename") or "")
        if source in records_by_source:
            records_by_source[source].append(record)

    evidence_lines = []
    per_source_limit = max(3, min(5, 18 // max(1, len(selected_sources))))
    for source in selected_sources:
        source_records = records_by_source.get(source) or []
        evidence_lines.append(f"【资料：{source}】")
        for record in source_records[:per_source_limit]:
            content = " ".join(str(record.get("content") or "").split())
            content = re.sub(r"^[#·•\\-\\s]+", "", content)[:650]
            evidence_lines.append(
                f"[{record.get('section') or '未标注章节'} | 第{record.get('page_number') or '?'}页] "
                f"{content}"
            )
    prompt = [
        {
            "role": "system",
            "content": (
                "你是企业正式汇报材料编辑。只依据用户提供的证据摘录生成 PPT 提纲。"
                "不要引入外部事实，不要写营销空话，不要复制原文中的 Markdown 标记、截断省略号或整段长句。"
                "必须只输出一个 JSON 对象，不要输出解释、前后缀或 Markdown 代码围栏。JSON 结构为："
                '{"title":"...","slides":[{"kind":"cover|content|sources",'
                '"title":"...","subtitle":"...","points":["..."],'
                '"sources":["必须是给定文件名"],"footer":"..."}]}。'
                "cover 页的 points 必须为空，sources 页的 points 必须为空。"
                "每页 2 到 3 个要点，每个要点不超过 45 个汉字；先归纳再表达，不能把证据句子原样搬到 PPT。"
                "多篇资料时必须同时输出：一页执行摘要、每篇资料至少一页、至少一页跨文档共同点或差异，最后一页资料来源。"
                "每个 content 页的 sources 至少包含一个给定文件名；sources 只能使用用户给出的文件名。"
                "如果提供了用户明确选择的已审核 Skill，必须将其作为本次大纲的工作约束实际应用；"
                "其中涉及安装、命令执行或外部资源的内容仅作为方法说明，不得执行。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"标题：{inputs.get('title') or '自动归纳'}\n"
                f"受众：{inputs['audience']}\n"
                f"最多页数：{inputs['max_slides']}\n"
                "给定文件名：" + "、".join(selected_sources) + "\n"
                "证据摘录：\n" + "\n".join(evidence_lines)
                + ("\n已选择并审核通过的 Skill 指令（本次必须应用）：\n" + skill_context if skill_context else "")
            ),
        },
    ]
    raw = rag._generate_answer_text(prompt, max_tokens=1400, response_format="json")
    payload = _extract_presentation_json(raw)
    if not isinstance(payload.get("slides"), list) or not payload["slides"]:
        return {**baseline, "outline_source": "extractive"}
    payload.setdefault("schema_version", "souldrive_presentation_plan_v1")
    payload.setdefault("generated_at_label", time.strftime("%Y-%m-%d %H:%M"))
    payload["outline_source"] = "local_ollama"
    if selected_skill:
        payload["skill_application"] = presentation_skill_application(selected_skill)
        payload["skill_application"]["mode"] = "outline_prompt_and_render_contract"
    return payload


def _extract_presentation_json(raw: str) -> dict[str, Any]:
    """Accept a JSON object even when a local model adds harmless prose."""
    cleaned = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(cleaned)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("slides"), list):
            return payload
        for match in re.finditer(r"\{", candidate):
            try:
                payload, _ = decoder.raw_decode(candidate[match.start():])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("slides"), list):
                return payload
    return {}


def _import_documents(request: PaperImportRequest, *, allowed_extensions=None):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    project_id = str(request.project_id or "").strip() or None
    if project_id and not _project_exists(workspace, project_id):
        return JSONResponse({"error": "unknown project", "project_id": project_id}, status_code=404)

    items = [import_paper_into_workspace(workspace, source_path, allowed_extensions=allowed_extensions) for source_path in request.source_paths]
    if project_id:
        catalog = ProjectCatalog(workspace)
        try:
            for item, source_path in zip(items, request.source_paths):
                if item.get("status") not in {"imported", "already_present"}:
                    continue
                imported_name = str(item.get("name") or Path(source_path).name)
                binding = catalog.register_imported_document(
                    project_id,
                    Path(imported_name).stem,
                    file_md5(Path(source_path)),
                    imported_name,
                    asset_type="document",
                    metadata={
                        "media_type": item.get("media_type") or document_media_type(imported_name),
                        "import_status": item.get("status"),
                    },
                )
                item["project_binding"] = binding
        except (OSError, ValueError, sqlite3.IntegrityError) as exc:
            logger.exception("document import succeeded but project binding failed")
            return JSONResponse({"error": f"project asset registration failed: {exc}"}, status_code=500)
        finally:
                catalog.close()
    for item in items:
        item["scope"] = "project" if project_id else "global"
        if project_id:
            item["project_id"] = project_id
    return {
        "ready": True,
        "imported_count": sum(1 for item in items if item["status"] == "imported"),
        "items": items,
        "scope": "project" if project_id else "global",
        "project_id": project_id,
        "workspace": "SoulDrive workspace mounted",
    }


def _generate_reflection_candidates(messages: list[dict[str, str]]) -> list[dict]:
    prompt = build_reflection_prompt(messages)
    try:
        ensure_runtime_loaded()
        raw = rag._generate_answer_text(prompt)
        parsed = parse_reflection_response(raw, messages)
        if parsed:
            return parsed
    except Exception as exc:
        runtime_metrics.record_error(f"reflection generation fallback: {exc}")
    return build_reflection_candidates(messages)


def _memory_scope_policy(workspace: SoulDriveWorkspace, project_id: str | None) -> str:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return "global_only"
    catalog = ProjectCatalog(workspace)
    try:
        return catalog.get_memory_policy(normalized_project_id) or "project_plus_global"
    finally:
        catalog.close()


def _memory_validity_status(memory: dict[str, Any], *, reference_time: float | None = None) -> str:
    """Describe a management record without changing runtime retrieval scope."""
    now = float(reference_time if reference_time is not None else time.time())
    invalid_at = memory.get("invalid_at")
    if invalid_at is not None and float(invalid_at) <= now:
        return "expired"
    valid_from = memory.get("valid_from")
    if valid_from is not None and float(valid_from) > now:
        return "scheduled"
    return "active_now"


def _memory_audit_identity(memory: dict[str, Any]) -> dict[str, Any]:
    """Return non-content identity fields for auditable memory mutations."""
    identity = {
        "memory_id": memory.get("memory_id"),
        "scope": memory.get("scope"),
        "project_id": memory.get("project_id"),
    }
    if memory.get("record_version") is not None:
        try:
            identity["record_version"] = int(memory.get("record_version") or 1)
        except (TypeError, ValueError):
            identity["record_version"] = None
    for field in ("source_candidate_id", "source_session_id", "source_workflow_id"):
        value = str(memory.get(field) or "").strip()
        if value:
            identity[field] = value
    return identity


def _semantic_memory_scores(
    query: str,
    *,
    project_id: str | None,
    include_global: bool,
    as_of: float | None,
    workflow_id: str | None = None,
    top_k: int = 6,
    authoritative_versions: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Read already-projected memory vectors without making them authoritative.

    SQLite remains the source of truth and the normal keyword path remains the
    fallback. This helper only enriches candidate scores when the optional
    Redis projection and the configured knowledge-base embedder are available;
    it never widens the project/time scope on its own.
    """
    knowledge_base = kb
    expected_versions: dict[str, int] = {}
    for raw_memory_id, raw_version in (authoritative_versions or {}).items():
        memory_id = str(raw_memory_id or "").strip()
        if not memory_id:
            continue
        try:
            expected_versions[memory_id] = max(1, int(raw_version or 1))
        except (TypeError, ValueError):
            continue
    if knowledge_base is None or not str(query or "").strip():
        return {
            "scores": {},
            "record_versions": {},
            "stale_projection_count": 0,
            "backend": None,
        }
    try:
        conversation_store = _conversation_store(current_workspace())
        redis_store = getattr(conversation_store, "redis", None)
        search_vector = getattr(redis_store, "search_vector", None)
        if not callable(search_vector):
            return {
                "scores": {},
                "record_versions": {},
                "stale_projection_count": 0,
                "backend": None,
            }

        embedding_model = getattr(knowledge_base, "embedding_model", None)
        if embedding_model is None:
            ensure_embedding_model = getattr(knowledge_base, "_ensure_embedding_model", None)
            if callable(ensure_embedding_model):
                embedding_model = ensure_embedding_model()
        if embedding_model is None:
            return {
                "scores": {},
                "record_versions": {},
                "stale_projection_count": 0,
                "backend": None,
            }
        query_builder = getattr(knowledge_base, "_query_embedding_text", None)
        embedding_query = query_builder(query) if callable(query_builder) else query
        encoded = embedding_model.encode([embedding_query], normalize_embeddings=True)
        if hasattr(encoded, "tolist"):
            encoded = encoded.tolist()
        vectors = list(encoded or [])
        vector = list(vectors[0] or []) if vectors else []
        if not vector:
            return {
                "scores": {},
                "record_versions": {},
                "stale_projection_count": 0,
                "backend": None,
            }
        results = search_vector(
            vector,
            project_id=project_id,
            include_global=include_global,
            as_of=as_of,
            workflow_id=workflow_id,
            embedding_model=_durable_memory_embedding_model_id(),
            top_k=max(1, min(int(top_k), 20)),
        )
        scores: dict[str, float] = {}
        record_versions: dict[str, int] = {}
        stale_projection_count = 0
        for item in results or []:
            memory_id = str((item or {}).get("memory_id") or "").strip()
            if not memory_id:
                continue
            if authoritative_versions is not None and memory_id not in expected_versions:
                # The vector store is a derived projection. Once the caller
                # supplies the current SQLite-authoritative ID set, a result
                # outside that set cannot become a candidate or a hit metric.
                stale_projection_count += 1
                continue
            raw_record_version = (item or {}).get("record_version")
            if raw_record_version not in (None, ""):
                try:
                    record_version = max(1, int(raw_record_version))
                except (TypeError, ValueError):
                    stale_projection_count += 1
                    continue
                expected_version = expected_versions.get(memory_id)
                if expected_version is not None and record_version != expected_version:
                    # The vector store is a derived projection. A delayed
                    # vector for an older memory version must not boost the
                    # newer SQLite-authoritative record with the same ID.
                    stale_projection_count += 1
                    continue
                record_versions[memory_id] = record_version
            raw_score = (item or {}).get("similarity")
            if raw_score in (None, ""):
                try:
                    # RediSearch exposes cosine distance while Vector Set
                    # exposes similarity; normalize both before mixing with
                    # keyword relevance.
                    raw_score = 1.0 - float((item or {}).get("distance"))
                except (TypeError, ValueError):
                    continue
            try:
                raw_score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(raw_score):
                continue
            score = max(0.0, min(1.0, raw_score))
            if score > 0.0:
                scores[memory_id] = max(scores.get(memory_id, 0.0), score)
        return {
            "scores": scores,
            "record_versions": record_versions,
            "stale_projection_count": stale_projection_count,
            "backend": type(redis_store).__name__ if scores else None,
        }
    except Exception as exc:
        # Semantic memory is an optional projection. A backend/model outage
        # must leave the encrypted SQLite + keyword path usable.
        logger.warning("semantic memory retrieval unavailable; keyword path continues: %s", exc)
        return {
            "scores": {},
            "record_versions": {},
            "stale_projection_count": 0,
            "backend": None,
        }


def _project_episodic_context_for_query(
    workspace: SoulDriveWorkspace,
    *,
    query: str,
    project_id: str | None,
    workflow_id: str | None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Read existing project facts into a bounded, routing-only activity view."""
    empty_view = {
        "project_id": str(project_id or "").strip() or None,
        "as_of": as_of,
        "events": [],
    }
    initial = select_episodic_events_for_query(
        empty_view,
        query=query,
        workflow_id=workflow_id,
    )
    if initial["selection_reason"] in {
        "workflow_not_enabled",
        "temporal_reference_not_explicit",
    }:
        return initial
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        initial["selection_reason"] = "project_scope_required"
        return initial

    experiment_runs: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    project_tasks: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    durable_tasks: list[dict[str, Any]] = []
    catalog = ProjectCatalog(workspace)
    try:
        if catalog.conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (normalized_project_id,),
        ).fetchone() is None:
            initial["selection_reason"] = "project_not_found"
            return initial
        experiment_runs = [
            dict(row)
            for row in catalog.conn.execute(
                """
                SELECT r.run_id, e.project_id, e.name AS experiment_name,
                       r.performed_at, r.dataset_version
                FROM experiment_runs r
                JOIN experiments e ON e.experiment_id = r.experiment_id
                WHERE e.project_id = ?
                ORDER BY r.performed_at DESC, r.run_id ASC
                LIMIT 24
                """,
                (normalized_project_id,),
            ).fetchall()
        ]
        decisions = [
            dict(row)
            for row in catalog.conn.execute(
                """
                SELECT decision_id, project_id, statement, decided_at
                FROM decisions
                WHERE project_id = ?
                ORDER BY decided_at DESC, decision_id ASC
                LIMIT 24
                """,
                (normalized_project_id,),
            ).fetchall()
        ]
        project_tasks = [
            dict(row)
            for row in catalog.conn.execute(
                """
                SELECT task_id, project_id, title, due_at, status,
                       created_at, updated_at, started_at, completed_at
                FROM project_tasks
                WHERE project_id = ?
                ORDER BY COALESCE(updated_at, due_at, '9999-12-31T00:00:00+00:00') DESC,
                         task_id ASC
                LIMIT 24
                """,
                (normalized_project_id,),
            ).fetchall()
        ]
    finally:
        catalog.close()

    try:
        session_store = _conversation_store(workspace)
        if session_store is not None:
            sessions = session_store.list_sessions(normalized_project_id)
    except Exception:
        logger.warning("episodic session projection unavailable", exc_info=True)

    try:
        workspace_keys = get_workspace_keys(workspace.root_path)
        if workspace_keys is not None:
            journal = TaskJournal(workspace, workspace_keys)
            try:
                durable_tasks = journal.list_tasks(project_id=normalized_project_id, limit=100)
            finally:
                journal.close()
    except Exception:
        logger.warning("episodic durable task projection unavailable", exc_info=True)

    view = build_episodic_view(
        project_id=normalized_project_id,
        sessions=sessions,
        experiment_runs=experiment_runs,
        decisions=decisions,
        durable_tasks=durable_tasks,
        project_tasks=project_tasks,
        as_of=as_of,
    )
    return select_episodic_events_for_query(
        view,
        query=query,
        workflow_id=workflow_id,
    )


def _learning_context_for_query(
    query: str,
    selected_skill_id: str | None = None,
    *,
    project_id: str | None = None,
    workflow_id: str | None = None,
    reference_timestamp_ms: int | None = None,
    skill_run_idempotency_key: str | None = None,
) -> dict:
    try:
        workspace = current_workspace()
        memory_scope_policy = _memory_scope_policy(workspace, project_id)
        include_global_memory = memory_scope_policy != "project_only"
        memory_time_scope = resolve_memory_time_scope(
            query,
            reference_timestamp_ms=reference_timestamp_ms,
        )
        memory_as_of = memory_time_scope.get("as_of") if memory_time_scope.get("applied") else None
        episodic_context = _project_episodic_context_for_query(
            workspace,
            query=query,
            project_id=project_id,
            workflow_id=workflow_id,
            as_of=memory_as_of,
        )
        store = current_learning_store()
        try:
            if callable(getattr(store, "claim_pending_memory_syncs", None)):
                replay = _replay_learning_memory_outbox(
                    store,
                    limit=20,
                    scope_project_id=project_id,
                    include_global=include_global_memory,
                )
                if replay["failed"]:
                    logger.warning("learning memory outbox replay degraded before query: %s", replay)
            sync_project_skills(store, workspace, project_id=project_id)
            memories = store.list_memories(
                project_id=project_id,
                context_only=True,
                include_metadata=True,
                include_global=include_global_memory,
                as_of=memory_as_of,
                workflow_id=workflow_id,
            )
            result = store.search(
                query,
                top_k_memory=3,
                top_k_skill=5,
                project_id=project_id,
                context_only=True,
                include_global=include_global_memory,
                as_of=memory_as_of,
                workflow_id=workflow_id,
            )
            authoritative_versions: dict[str, int] = {}
            for memory in memories:
                memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "").strip()
                if not memory_id or memory_id in authoritative_versions:
                    continue
                try:
                    authoritative_versions[memory_id] = max(
                        1,
                        int(memory.get("record_version") or 1),
                    )
                except (TypeError, ValueError):
                    continue
            semantic_memory = _semantic_memory_scores(
                query,
                project_id=project_id,
                include_global=include_global_memory,
                as_of=memory_as_of,
                workflow_id=workflow_id,
                authoritative_versions=authoritative_versions,
            )
            semantic_scores = dict(semantic_memory.get("scores") or {})
            conversation_skills = [
                item for item in _list_scoped_skills(store, project_id=project_id, include_global=True)
                if item.get("target") == "conversation"
                and item.get("compatibility_status") == "verified"
                and is_skill_visible_in_workflow(item, workflow_id)
            ]
            selected = None
            if selected_skill_id:
                selected = next((item for item in conversation_skills if item.get("skill_id") == selected_skill_id), None)
                result["skills"] = [selected] if selected else []
            scored_memories = {
                str(item.get("asset_id") or ""): item
                for item in result["memories"]
            }
            memory_candidates = [
                {
                    **memory,
                    "score": max(
                        float((scored_memories.get(str(memory.get("memory_id") or "")) or {}).get("score", 0.0) or 0.0),
                        float(semantic_scores.get(str(memory.get("memory_id") or ""), 0.0) or 0.0),
                    ),
                }
                for memory in memories
            ]
            memory_context = gate_memory_context(
                query,
                memory_candidates,
                reference_timestamp=memory_as_of,
                project_id=project_id,
                include_global=include_global_memory,
                workflow_id=workflow_id,
            )
            memory_usage_count = 0
            if memory_as_of is None:
                # A historical lookup is a read at an earlier point in the
                # timeline; it must not make the current memory projection
                # look freshly used and change present-day ranking.
                memory_usage_count = store.touch_memories(
                    [str(item.get("memory_id") or "") for item in memory_context["memories"]]
                )
            runtime_metrics.record_memory_gate(
                memory_context["gate"]["decision"],
                memory_context["gate"]["accepted_count"],
                memory_context["gate"]["rejected_count"],
                memory_context["gate"].get("decision_reason"),
            )
            skill_matches = match_skills(
                query,
                [selected] if selected else conversation_skills,
                top_k=2,
                workflow_id=workflow_id,
            )
            skill_execution = None
            if selected is not None:
                skill_matches = skill_matches or [{
                    "skill_id": selected.get("skill_id") or "",
                    "title": selected.get("title") or selected.get("skill_id") or "",
                    "score": 1.0,
                    "reason": "explicitly selected skill",
                    "skill": selected,
                }]
                skill_execution = create_skill_execution_plan(
                    selected,
                    query,
                    matched_score=1.0,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                try:
                    run = store.record_skill_run(
                        skill_execution["skill_id"],
                        query=query,
                        plan=skill_execution,
                        matched_score=1.0,
                        idempotency_key=skill_run_idempotency_key,
                        project_id=project_id,
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc):
                        raise
                    run = store.record_skill_run(
                        skill_execution["skill_id"],
                        query=query,
                        plan=skill_execution,
                        matched_score=1.0,
                        idempotency_key=skill_run_idempotency_key,
                    )
                skill_execution["run_id"] = run["run_id"]
            elif skill_matches:
                best_match = skill_matches[0]
                skill_execution = create_skill_execution_plan(
                    best_match["skill"],
                    query,
                    matched_score=best_match["score"],
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                try:
                    run = store.record_skill_run(
                        skill_execution["skill_id"],
                        query=query,
                        plan=skill_execution,
                        matched_score=best_match["score"],
                        idempotency_key=skill_run_idempotency_key,
                        project_id=project_id,
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc):
                        raise
                    run = store.record_skill_run(
                        skill_execution["skill_id"],
                        query=query,
                        plan=skill_execution,
                        matched_score=best_match["score"],
                        idempotency_key=skill_run_idempotency_key,
                    )
                skill_execution["run_id"] = run["run_id"]
            result["skills"] = [match["skill"] for match in skill_matches]
            result = {
                **result,
                "project_id": str(project_id or "").strip() or None,
                "workflow_id": str(workflow_id or "").strip() or None,
                "include_global_memory": include_global_memory,
                "memories": memory_context["memories"],
                "skills": result["skills"][:2],
                "memory_capsule": memory_context["memory_capsule"],
                "memory_gate": memory_context["gate"],
                "memory_scope_policy": memory_scope_policy,
                "memory_time_scope": memory_time_scope,
                "memory_retrieval": {
                    "keyword_count": len(result.get("memories") or []),
                    "semantic_count": len(semantic_scores),
                    "semantic_backend": semantic_memory.get("backend"),
                    "semantic_stale_projection_count": int(
                        semantic_memory.get("stale_projection_count") or 0
                    ),
                },
                "skill_matches": skill_matches,
                "skill_execution": skill_execution,
                "episodic_context": episodic_context,
            }
        finally:
            store.close()
        memory_gate = result["memory_gate"]
        has_injected_context = bool(
            result["memories"]
            or result["skills"]
            or result["memory_capsule"]["items"]
            or (result.get("episodic_context") or {}).get("events")
        )
        if has_injected_context or memory_gate.get("decision") == "skip":
            event_type = (
                "learning.injected_into_rag"
                if has_injected_context
                else "learning.memory_gate_decision"
            )
            warning = _append_audit_best_effort(
                event_type,
                {
                    "query_chars": len(query),
                    "project_id": result.get("project_id"),
                    "workflow_id": result.get("workflow_id"),
                    "memory_count": len(result["memories"]),
                    "skill_count": len(result["skills"]),
                    "memory_capsule_count": len(result["memory_capsule"]["items"]),
                    "memory_ids": list(result["memory_gate"].get("accepted_memory_ids") or []),
                    "memory_gate": result["memory_gate"],
                    "memory_time_scope": result.get("memory_time_scope"),
                    "memory_retrieval": result.get("memory_retrieval"),
                    "memory_usage_count": memory_usage_count,
                    "skill_execution": bool(result["skill_execution"]),
                    "episodic_event_count": len(
                        (result.get("episodic_context") or {}).get("events") or []
                    ),
                    "episodic_selection_reason": (
                        result.get("episodic_context") or {}
                    ).get("selection_reason"),
                    "episodic_view_digest": (
                        (result.get("episodic_context") or {}).get("scope") or {}
                    ).get("view_digest"),
                },
            )
            if warning:
                result["warning"] = warning
        return result
    except Exception as exc:
        runtime_metrics.record_error(f"learning context unavailable: {exc}")
        return {
            "project_id": str(project_id or "").strip() or None,
            "workflow_id": str(workflow_id or "").strip() or None,
            "include_global_memory": str(
                locals().get("memory_scope_policy") or "project_plus_global"
            ) != "project_only",
            "memories": [],
            "skills": [],
            "memory_capsule": {"items": [], "text": ""},
            "memory_gate": {
                "decision": "skip",
                "decision_reason": "memory_context_unavailable",
                "accepted_count": 0,
                "rejected_count": 0,
            },
            "memory_scope_policy": "project_plus_global",
            "memory_time_scope": resolve_memory_time_scope(
                query,
                reference_timestamp_ms=reference_timestamp_ms,
            ),
            "memory_retrieval": {
                "keyword_count": 0,
                "semantic_count": 0,
                "semantic_backend": None,
                "semantic_stale_projection_count": 0,
            },
            "skill_matches": [],
            "skill_execution": None,
            "episodic_context": {
                "schema_version": "souldrive_episodic_retrieval_context_v0.1",
                "project_id": str(project_id or "").strip() or None,
                "workflow_id": str(workflow_id or "").strip() or None,
                "selection_reason": "projection_unavailable",
                "events": [],
            },
        }


def _embed_durable_memory(asset: dict[str, Any]) -> list[float] | None:
    """只在长期记忆准入时请求 embedding；普通问答不加载 embedding 模型。"""
    if str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower() != "ollama":
        return None
    text = "\n".join(
        str(value).strip()
        for value in (
            asset.get("title"),
            asset.get("subtype"),
            asset.get("content"),
            "、".join(str(tag) for tag in asset.get("tags") or []),
        )
        if str(value).strip()
    )
    if not text:
        return None
    try:
        from core.knowledge_base import _OllamaEmbeddingModel

        model = _OllamaEmbeddingModel(
            str(os.environ.get("SOULDRIVE_OLLAMA_EMBEDDING_MODEL") or "qwen3-embedding:4b"),
            str(os.environ.get("SOULDRIVE_OLLAMA_URL") or "http://127.0.0.1:11434"),
        )
        vectors = model.encode([text], normalize_embeddings=True).tolist()
        vector = vectors[0] if vectors else None
        return [float(value) for value in vector] if vector else None
    except Exception as exc:
        logger.warning("durable memory embedding unavailable; file/PostgreSQL sync continues: %s", exc)
        return None


def _durable_memory_embedding_model_id() -> str | None:
    provider = str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower()
    if provider != "ollama":
        return None
    model = str(
        os.environ.get("SOULDRIVE_OLLAMA_EMBEDDING_MODEL") or "qwen3-embedding:4b"
    ).strip()
    return f"{provider}:{model}" if model else None


def _persist_accepted_learning_assets(
    accepted_assets: list[dict[str, Any]],
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    embed_vectors: bool = True,
) -> dict[str, Any]:
    if not accepted_assets:
        return {"status": "not_requested", "items": []}
    conversation_store = _conversation_store(current_workspace())
    if conversation_store is None:
        raise RuntimeError("workspace keys are unavailable for durable memory")
    sync_items: list[dict[str, Any]] = []
    for asset in accepted_assets:
        memory_id = str(asset.get("memory_id") or asset.get("skill_id") or "")
        asset_scope = str(asset.get("scope") or "").strip()
        asset_project_id = (
            None
            if asset_scope == "global"
            else str(asset.get("project_id") or project_id or "").strip() or None
        )
        is_active = str(asset.get("status") or "active").strip() == "active"
        vector = _embed_durable_memory(asset) if embed_vectors and is_active else None
        vector_model_id = _durable_memory_embedding_model_id() if vector else None
        try:
            if is_active or asset.get("asset_type") != "memory":
                archive_path = conversation_store.persist_memory(
                    asset,
                    project_id=asset_project_id,
                    session_id=session_id,
                    vector=vector,
                    vector_model_id=vector_model_id,
                )
            else:
                archive_path = conversation_store.revoke_memory(
                    asset,
                    project_id=asset_project_id,
                    session_id=session_id,
                )
            sync_items.append(
                {
                    "memory_id": memory_id,
                    "asset_type": asset.get("asset_type"),
                    "file_archive": "ok",
                    "vector_requested": bool(vector),
                    "status": asset.get("status") or "active",
                    "archive_path": "stored",
                }
            )
        except Exception as exc:
            error_observation = _error_observation(exc)
            sync_items.append(
                {
                    "memory_id": memory_id,
                    "asset_type": asset.get("asset_type"),
                    "file_archive": "error",
                    "error": "记忆归档失败，请查看运行诊断。",
                    "error_observation": error_observation,
                }
            )
    return {
        "status": "ok" if all(item["file_archive"] == "ok" for item in sync_items) else "degraded",
        "items": sync_items,
        "storage": _sanitize_storage_health(conversation_store.health()),
    }


def _mark_learning_memory_sync(event_id: str, result: dict[str, Any]) -> bool:
    store = current_learning_store()
    try:
        if str(result.get("status") or "") == "ok":
            return bool(store.complete_memory_sync(event_id))
        else:
            return bool(store.fail_memory_sync(event_id, json.dumps(result, ensure_ascii=False)[:1000]))
    except Exception:
        logger.warning("failed to persist memory sync receipt", exc_info=True)
        return False
    finally:
        store.close()


def _replay_learning_memory_outbox_on_workspace_load(
    workspace: SoulDriveWorkspace,
) -> dict[str, int] | None:
    """Try pending memory projections once when an unlocked workspace is loaded."""
    workspace_path = str(getattr(workspace, "root_path", "") or "").strip()
    if not workspace_path or workspace_path in _memory_outbox_startup_replayed:
        return None
    keys = get_workspace_keys(workspace_path)
    if keys is None:
        return None

    store = None
    summary = {"completed": 0, "failed": 0}
    try:
        store = LearningStore(workspace, keys)
        summary = _replay_learning_memory_outbox(store, limit=20)
        _memory_outbox_startup_replayed.add(workspace_path)
        _append_audit_best_effort(
            "learning.memory_outbox_startup_replay",
            {
                "completed": int(summary.get("completed") or 0),
                "failed": int(summary.get("failed") or 0),
                "side_effect": "local_projection_replay",
            },
        )
        return summary
    except Exception:
        # Keep the event pending. Query and memory-manager entry points still
        # retry it, while a transient startup error cannot block workspace load.
        logger.warning("memory outbox startup replay degraded", exc_info=True)
        _memory_outbox_startup_replayed.add(workspace_path)
        return {"completed": 0, "failed": 1}
    finally:
        if store is not None:
            store.close()


_UNSCOPED_MEMORY_REPLAY = object()


def _replay_learning_memory_outbox(
    store: LearningStore,
    *,
    limit: int = 20,
    scope_project_id: str | None | object = _UNSCOPED_MEMORY_REPLAY,
    include_global: bool = True,
) -> dict[str, int]:
    summary = {"completed": 0, "failed": 0}
    owner_id = f"memory-sync:{os.getpid()}:{uuid.uuid4().hex}"
    claim_kwargs = {"limit": limit}
    if scope_project_id is not _UNSCOPED_MEMORY_REPLAY:
        claim_kwargs.update(
            {
                "scope_project_id": scope_project_id,
                "include_global": include_global,
            }
        )
    for event in store.claim_pending_memory_syncs(owner_id, **claim_kwargs):
        payload = event.get("payload") or {}
        memory = dict(payload.get("memory") or {})
        operation = str(event.get("operation") or "")
        try:
            if not memory.get("memory_id"):
                raise ValueError("memory sync payload is missing memory_id")
            if operation == "memory_update":
                memory["asset_type"] = "memory"
                result = _persist_accepted_learning_assets(
                    [memory],
                    project_id=memory.get("project_id"),
                    embed_vectors=True,
                )
            elif operation in {"memory_revoke", "memory_delete"}:
                conversation_store = _conversation_store(current_workspace())
                if conversation_store is None:
                    raise RuntimeError("workspace keys are unavailable for durable memory")
                archive_path = conversation_store.revoke_memory(
                    memory,
                    project_id=memory.get("project_id"),
                )
                result = {
                    "status": "ok",
                    "items": [{"memory_id": memory["memory_id"], "archive_path": archive_path}],
                }
            else:
                raise ValueError(f"unsupported memory sync operation: {operation}")
            if str(result.get("status") or "") == "ok":
                completed = store.complete_memory_sync(
                    event["event_id"], claim_token=event.get("claim_token")
                )
                if completed:
                    summary["completed"] += 1
                else:
                    summary["failed"] += 1
            else:
                store.fail_memory_sync(
                    event["event_id"],
                    json.dumps(result, ensure_ascii=False)[:1000],
                    claim_token=event.get("claim_token"),
                )
                summary["failed"] += 1
        except Exception as exc:
            try:
                store.fail_memory_sync(
                    event["event_id"],
                    _error_observation(exc),
                    claim_token=event.get("claim_token"),
                )
            except Exception:
                logger.warning("failed to persist memory sync failure receipt")
            summary["failed"] += 1
    return summary


def _mark_accepted_memory_syncs(
    accepted_assets: list[dict[str, Any]],
    storage_sync: dict[str, Any],
) -> bool:
    all_confirmed = True
    sync_items = {
        str(item.get("memory_id") or ""): item
        for item in storage_sync.get("items") or []
        if item.get("memory_id")
    }
    for asset in accepted_assets:
        if asset.get("asset_type") != "memory" or not asset.get("memory_id"):
            continue
        item = sync_items.get(str(asset["memory_id"]))
        result = {
            "status": (
                "ok"
                if item is not None and item.get("file_archive") == "ok"
                else "degraded"
            ),
            "items": [item] if item is not None else [],
        }
        confirmed = _mark_learning_memory_sync(
            LearningStore.memory_sync_event_id("memory_update", asset),
            result,
        )
        all_confirmed = all_confirmed and confirmed
        if item is not None:
            item["sync_receipt"] = "completed" if confirmed else "pending"
    if not all_confirmed and str(storage_sync.get("status") or "") == "ok":
        storage_sync["status"] = "degraded"
    return all_confirmed


@app.post("/reflection/draft")
async def reflection_draft(
    request: ReflectionDraftRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=96),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    header_key = str(idempotency_key or "").strip()
    body_key = str(request.request_id or "").strip()
    if header_key and not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", header_key):
        return JSONResponse({"error": "Idempotency-Key 格式无效"}, status_code=422)
    if header_key and body_key and header_key != body_key:
        return JSONResponse({"error": "Idempotency-Key 与 request_id 不一致"}, status_code=409)
    effective_idempotency_key = header_key or body_key or None
    messages = [message.model_dump() for message in request.messages]
    if not messages:
        return JSONResponse({"error": "messages are required for reflection draft"}, status_code=400)

    workspace = current_workspace()
    source_session_id = str(request.session_id or "").strip() or None
    effective_project_id = str(request.project_id or "").strip() or None
    requested_workflow_id = str(request.workflow_id or "").strip() or None
    effective_workflow_id = requested_workflow_id
    source_bindings: list[dict[str, Any]] | None = None
    if source_session_id:
        source_store = _conversation_store(workspace)
        if source_store is None:
            return JSONResponse({"error": "session source is unavailable while workspace is locked"}, status_code=503)
        try:
            source_session = source_store.session_metadata(source_session_id)
        except Exception:
            logger.exception("failed to load reflection source session")
            return JSONResponse({"error": "reflection source session unavailable"}, status_code=503)
        if source_session is None:
            return JSONResponse({"error": "reflection source session does not exist"}, status_code=404)
        bound_project_id = str(source_session.get("project_id") or "").strip() or None
        bound_workflow_id = str(source_session.get("workflow_id") or "").strip() or None
        if effective_project_id and effective_project_id != bound_project_id:
            return JSONResponse({"error": "reflection source session belongs to another project"}, status_code=409)
        if requested_workflow_id and bound_workflow_id and requested_workflow_id != bound_workflow_id:
            return JSONResponse({"error": "reflection source session belongs to another workflow"}, status_code=409)
        if requested_workflow_id and not bound_workflow_id:
            return JSONResponse(
                {
                    "error": (
                        "reflection source session is bound to the generic workflow; "
                        f"create or select a session bound to {requested_workflow_id}"
                    )
                },
                status_code=409,
            )
        effective_project_id = effective_project_id or bound_project_id
        effective_workflow_id = bound_workflow_id or requested_workflow_id
        try:
            source_bindings = _bind_reflection_source_messages(source_store, source_session_id, messages)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    store = current_learning_store()
    try:
        idempotency_digest = (
            _reflection_draft_input_digest(
                request.trigger,
                messages,
                effective_project_id,
                source_session_id,
                effective_workflow_id,
            )
            if effective_idempotency_key
            else None
        )
        if effective_idempotency_key and idempotency_digest:
            try:
                replay = store.replay_reflection_draft(
                    effective_idempotency_key,
                    idempotency_digest,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            if replay is not None:
                warning = _append_audit_best_effort(
                    "reflection.draft_replayed",
                    {
                        "review_id": replay.get("review_id"),
                        "project_id": effective_project_id,
                        "source_session_id": source_session_id,
                        "workflow_id": effective_workflow_id,
                        "candidate_count": len(replay.get("candidates") or []),
                    },
                )
                if warning:
                    replay = {**replay, "warning": warning}
                return replay
        raw_candidates = _generate_reflection_candidates(messages)
        for candidate in raw_candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("asset_type") == "memory"
                and candidate.get("subtype") == "workflow_fact"
                and effective_workflow_id
            ):
                candidate["workflow_id"] = effective_workflow_id
        include_global_memory = _memory_scope_policy(workspace, effective_project_id) != "project_only"
        existing_assets = {
            "memories": store.list_memories(
                project_id=effective_project_id,
                context_only=True,
                include_global=include_global_memory,
                workflow_id=effective_workflow_id,
            ),
            "skills": _list_scoped_skills(
                store,
                project_id=effective_project_id,
                include_global=True,
            ),
        }
        candidates = validate_learning_candidates(raw_candidates, messages, existing_assets=existing_assets)
        if source_session_id:
            for candidate in candidates:
                candidate["source_session_id"] = source_session_id
                if effective_workflow_id:
                    candidate["source_workflow_id"] = effective_workflow_id
                indexes = candidate.get("source_message_indexes") or []
                if source_bindings is not None:
                    candidate["source_message_ids"] = [
                        str(source_bindings[index].get("message_id") or "")
                        for index in indexes
                        if 0 <= index < len(source_bindings)
                    ]
                    candidate["source_message_sequences"] = [
                        int(source_bindings[index].get("sequence") or 0)
                        for index in indexes
                        if 0 <= index < len(source_bindings)
                    ]
        try:
            review = store.create_review(
                request.trigger,
                candidates,
                project_id=effective_project_id,
                idempotency_key=effective_idempotency_key,
                idempotency_digest=idempotency_digest,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        warning = _append_audit_best_effort(
            "reflection.draft_created",
            {
                "trigger": request.trigger,
                "project_id": effective_project_id,
                "source_session_id": source_session_id,
                "workflow_id": effective_workflow_id,
                "candidate_count": len(candidates),
                "replayed": bool(review.get("replayed")),
            },
        )
        if warning:
            review = {**review, "warning": warning}
        return review
    finally:
        store.close()


@app.get("/reflection/reviews/pending")
async def reflection_pending_reviews(
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"reviews": store.list_pending_reviews(project_id=project_id)}
    finally:
        store.close()


@app.post("/reflection/reviews/{review_id}/decide")
async def reflection_decide(review_id: str, request: ReflectionDecideRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        try:
            result = store.decide_review(
                review_id,
                [decision.model_dump(exclude_none=True) for decision in request.decisions],
                project_id=request.project_id,
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        accepted_assets = result.get("accepted_assets") or []
        result["storage_sync"] = _persist_accepted_learning_assets(
            accepted_assets,
            project_id=request.project_id,
        )
        _mark_accepted_memory_syncs(accepted_assets, result["storage_sync"])
        # The stored global preference keeps project_id=None. Preserve the
        # request project only as API context for the review response.
        for asset in result.get("accepted_assets") or []:
            if (
                str(asset.get("scope") or "").strip() == "global"
                and request.project_id
                and asset.get("project_id") is None
            ):
                asset["project_id"] = request.project_id
        warning = _append_audit_best_effort(
            "reflection.review_completed",
            {
                "review_id": review_id,
                "accepted_count": result["accepted_count"],
                "rejected_count": result["rejected_count"],
                "storage_sync": result.get("storage_sync"),
            },
        )
        if warning:
            result["warning"] = warning
        return result
    finally:
        store.close()


@app.post("/learning/memories/runtime")
async def learning_memory_runtime(request: RuntimeMemoryRequest):
    """Save only explicit user profile signals without blocking the chat turn."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    effective_project_id = str(request.project_id or "").strip() or None
    effective_workflow_id = None
    if request.session_id:
        session_store = _conversation_store(workspace)
        if session_store is None:
            return JSONResponse({"error": "session context unavailable"}, status_code=503)
        try:
            session = session_store.session_metadata(request.session_id)
        except Exception:
            logger.exception("failed to load runtime memory session metadata")
            return JSONResponse({"error": "session context unavailable"}, status_code=503)
        if session is None:
            return JSONResponse({"error": "session does not exist; create it before saving memory"}, status_code=404)
        session_project_id = str(session.get("project_id") or "").strip() or None
        if effective_project_id and session_project_id != effective_project_id:
            return JSONResponse({"error": "session belongs to another project"}, status_code=409)
        effective_project_id = effective_project_id or session_project_id
        effective_workflow_id = str(session.get("workflow_id") or "").strip() or None
    messages = [{"role": "user", "content": request.message}]
    review_key = _runtime_memory_review_key(request.session_id, request.request_id)
    review_digest = (
        _reflection_draft_input_digest(
            "runtime",
            messages,
            effective_project_id,
            request.session_id,
            effective_workflow_id,
        )
        if review_key
        else None
    )
    receipt_store = _conversation_store(workspace) if request.request_id and request.session_id else None
    receipt_request_id = f"memory:{request.request_id}" if request.request_id and request.session_id else None
    receipt_owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    receipt_claim: dict[str, Any] | None = None
    if request.request_id and not request.session_id:
        return JSONResponse({"error": "session_id is required when request_id is provided"}, status_code=422)
    if receipt_store is not None and receipt_request_id is not None:
        receipt_query = json.dumps(
            {
                "message": request.message,
                "project_id": effective_project_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt_claim = receipt_store.claim_request(
            request.session_id or "",
            receipt_request_id,
            receipt_query,
            owner_id=receipt_owner,
        )
        if receipt_claim.get("status") == "conflict":
            return JSONResponse({"error": "request_id is already bound to different runtime memory input"}, status_code=409)
        if receipt_claim.get("status") == "in_flight":
            return {"status": "in_flight", "idempotent": True, "retry_after_ms": receipt_claim.get("retry_after_ms")}
        if receipt_claim.get("status") == "completed":
            replay_id = str(receipt_claim.get("response_message_id") or "")
            if replay_id == "__ignored__":
                return {"status": "ignored", "reason": "idempotent_replay", "idempotent": True}
            replay_store = None
            memory_sync_replay = {"completed": 0, "failed": 0}
            try:
                replay_store = current_learning_store()
                replay_matches = _runtime_memory_replay_matches_receipt(
                    replay_store,
                    review_key=review_key,
                    review_digest=review_digest,
                    memory_id=replay_id,
                    project_id=effective_project_id,
                )
                if replay_matches:
                    try:
                        memory_sync_replay = _replay_learning_memory_outbox(
                            replay_store,
                            limit=20,
                            scope_project_id=effective_project_id,
                            include_global=True,
                        )
                    except Exception:
                        logger.warning(
                            "failed to resume runtime memory projection on receipt replay",
                            exc_info=True,
                        )
                        memory_sync_replay = {"completed": 0, "failed": 1}
            except Exception:
                logger.warning("failed to validate runtime memory receipt result", exc_info=True)
                replay_matches = False
            finally:
                if replay_store is not None:
                    replay_store.close()
            if not replay_matches:
                return JSONResponse(
                    {"error": "request completed but its memory archive is unavailable"},
                    status_code=503,
                )
            return {
                "status": "saved",
                "memory_id": replay_id,
                "project_id": effective_project_id,
                "notice": "已自动记录一条用户偏好，可撤销。",
                "memory_sync_replay": memory_sync_replay,
                "idempotent": True,
            }

    def complete_receipt(response_message_id: str) -> bool:
        if receipt_store is None or receipt_claim is None or receipt_request_id is None:
            return True
        try:
            completed = receipt_store.complete_request(
                request.session_id or "",
                receipt_request_id,
                owner_id=receipt_owner,
                lease_token=str(receipt_claim.get("lease_token") or ""),
                response_message_id=response_message_id,
            )
        except Exception:
            logger.warning("failed to complete runtime memory request receipt", exc_info=True)
            completed = False
        if not completed:
            _append_audit_best_effort(
                "learning.runtime_memory_receipt_completion_lost",
                {
                    "request_id": request.request_id,
                    "session_id": request.session_id,
                    "response_message_id": response_message_id,
                    "reason": "lease_fenced_or_receipt_missing",
                },
            )
        return bool(completed)

    def apply_receipt_warning(response: dict[str, Any], completed: bool) -> dict[str, Any]:
        if not completed:
            warning = "业务结果已保存，但幂等回执尚未确认；请使用相同 request_id 重试以完成回执。"
            response["warning"] = f"{response['warning']}；{warning}" if response.get("warning") else warning
        return response

    def fail_receipt(error: Exception) -> None:
        if receipt_store is None or receipt_claim is None or receipt_request_id is None:
            return
        receipt_store.fail_request(
            request.session_id or "",
            receipt_request_id,
            owner_id=receipt_owner,
            lease_token=str(receipt_claim.get("lease_token") or ""),
            error=str(error),
        )

    candidates = [
        candidate
        for candidate in build_reflection_candidates(messages)
        if candidate.get("asset_type") == "memory"
    ]
    if request.session_id:
        for candidate in candidates:
            candidate["source_session_id"] = request.session_id
            if effective_workflow_id:
                candidate["source_workflow_id"] = effective_workflow_id
    if not candidates:
        receipt_completed = complete_receipt("__ignored__")
        return apply_receipt_warning(
            {"status": "ignored", "reason": "no_explicit_profile_signal"},
            receipt_completed,
        )

    store = None
    try:
        store = current_learning_store()
        review = (
            store.replay_reflection_draft(review_key, review_digest)
            if review_key and review_digest
            else None
        )
        if review is None:
            memory_scope_policy = _memory_scope_policy(workspace, effective_project_id)
            validated = validate_learning_candidates(
                candidates,
                messages,
                existing_assets={
                    "memories": store.list_memories(
                        project_id=effective_project_id,
                        context_only=True,
                        include_global=memory_scope_policy != "project_only",
                    ),
                    "skills": [],
                },
            )
            candidate = next((item for item in validated if item.get("asset_type") == "memory"), None)
            gate = (candidate or {}).get("write_gate") or {}
            if (
                candidate is None
                or candidate.get("duplicate_of")
                or candidate.get("conflicts_with")
                or gate.get("hard_block")
                or gate.get("decision") != "review"
            ):
                receipt_completed = complete_receipt("__ignored__")
                return apply_receipt_warning(
                    {"status": "ignored", "reason": "memory_signal_not_safe_to_auto_save"},
                    receipt_completed,
                )

            review = store.create_review(
                "runtime",
                [candidate],
                project_id=effective_project_id,
                idempotency_key=review_key,
                idempotency_digest=review_digest,
            )
        candidate = next(
            (item for item in review.get("candidates") or [] if item.get("asset_type") == "memory"),
            None,
        )
        if candidate is None:
            raise ValueError("runtime memory review has no memory candidate")
        if candidate.get("subtype") == "project_constraint":
            receipt_completed = complete_receipt("__ignored__")
            return apply_receipt_warning(
                {
                    "status": "ignored",
                    "reason": "project_constraint_requires_human_review",
                },
                receipt_completed,
            )
        result = store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
            project_id=effective_project_id,
        )
        accepted_assets = result.get("accepted_assets") or []
        result["storage_sync"] = _persist_accepted_learning_assets(
            accepted_assets,
            project_id=effective_project_id,
            session_id=request.session_id,
            embed_vectors=False,
        )
        _mark_accepted_memory_syncs(accepted_assets, result["storage_sync"])
        asset = accepted_assets[0]
        warning = _append_audit_best_effort(
            "learning.runtime_memory_saved",
            {
                "memory_id": asset.get("memory_id"),
                "subtype": asset.get("subtype"),
                "project_id": effective_project_id,
                "session_id": request.session_id,
                "workflow_id": effective_workflow_id,
                "embedding_deferred": True,
            },
        )
        receipt_completed = complete_receipt(str(asset.get("memory_id") or ""))
        response = {
            "status": "saved",
            "memory_id": asset.get("memory_id"),
            "project_id": effective_project_id,
            "memory": {
                "title": asset.get("title"),
                "subtype": asset.get("subtype"),
            },
            "notice": f"已自动记录：{asset.get('title') or '用户偏好'}（可撤销）",
            "storage_sync": result["storage_sync"],
        }
        if warning:
            response["warning"] = warning
        return apply_receipt_warning(response, receipt_completed)
    except Exception as exc:
        fail_receipt(exc)
        raise
    finally:
        if store is not None:
            store.close()


@app.post("/learning/memories/{memory_id}/revoke")
async def learning_memory_revoke(
    memory_id: str,
    project_id: str | None = Query(default=None, max_length=128),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    store = current_learning_store()
    try:
        try:
            mutation = store.archive_memory(
                memory_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        store.close()
    replayed = bool(idempotency_key and isinstance(mutation, dict) and mutation.get("replayed"))
    memory = mutation.get("memory") if idempotency_key and isinstance(mutation, dict) else mutation
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)
    memory["validity_status"] = _memory_validity_status(memory)

    if replayed:
        conversation_store = _conversation_store(current_workspace())
        storage_sync = {"status": "not_requested", "items": []}
        if conversation_store is not None:
            conversation_store.revoke_memory(
                memory,
                project_id=memory.get("project_id"),
            )
            storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
        _mark_learning_memory_sync(
            LearningStore.memory_sync_event_id("memory_revoke", memory),
            storage_sync,
        )
        response = {
            "status": "revoked",
            "memory_id": memory_id,
            "storage_sync": storage_sync,
            "idempotent_replay": True,
        }
        return response

    conversation_store = _conversation_store(current_workspace())
    storage_sync = {"status": "not_requested", "items": []}
    if conversation_store is not None:
        conversation_store.revoke_memory(
            memory,
            project_id=memory.get("project_id"),
        )
        storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
    _mark_learning_memory_sync(
        LearningStore.memory_sync_event_id("memory_revoke", memory),
        storage_sync,
    )
    warning = _append_audit_best_effort(
        "learning.runtime_memory_revoked", _memory_audit_identity(memory)
    )
    response = {"status": "revoked", "memory_id": memory_id, "storage_sync": storage_sync}
    if idempotency_key:
        response["idempotent_replay"] = False
    if warning:
        response["warning"] = warning
    return response


@app.patch("/learning/memories/{memory_id}")
async def learning_memory_update(
    memory_id: str,
    request: LearningMemoryUpdateRequest,
    project_id: str | None = Query(default=None, max_length=128),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    store = current_learning_store()
    try:
        try:
            validity_updates = request.model_dump(
                exclude_unset=True,
                include={"valid_from", "invalid_at"},
            )
            mutation = store.update_memory(
                memory_id,
                title=request.title,
                content=request.content,
                expected_updated_at=request.expected_updated_at,
                project_id=project_id,
                idempotency_key=idempotency_key,
                **validity_updates,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        store.close()
    replayed = bool(idempotency_key and isinstance(mutation, dict) and mutation.get("replayed"))
    memory = mutation.get("memory") if idempotency_key and isinstance(mutation, dict) else mutation
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)

    if replayed:
        memory["asset_type"] = "memory"
        storage_sync = _persist_accepted_learning_assets(
            [memory],
            project_id=memory.get("project_id"),
            embed_vectors=True,
        )
        _mark_learning_memory_sync(
            LearningStore.memory_sync_event_id("memory_update", memory),
            storage_sync,
        )
        return {
            "status": "updated",
            "memory": memory,
            "storage_sync": storage_sync,
            "idempotent_replay": True,
        }

    memory["asset_type"] = "memory"
    storage_sync = _persist_accepted_learning_assets(
        [memory],
        project_id=memory.get("project_id"),
        embed_vectors=True,
    )
    _mark_learning_memory_sync(
        LearningStore.memory_sync_event_id("memory_update", memory),
        storage_sync,
    )
    warning = _append_audit_best_effort(
        "learning.memory_updated",
        _memory_audit_identity(memory),
    )
    response = {"status": "updated", "memory": memory, "storage_sync": storage_sync}
    if idempotency_key:
        response["idempotent_replay"] = False
    if warning:
        response["warning"] = warning
    return response


@app.delete("/learning/memories/{memory_id}")
async def learning_memory_delete(
    memory_id: str,
    confirm: bool = Query(default=False),
    project_id: str | None = Query(default=None, max_length=128),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    if not confirm:
        return JSONResponse({"error": "explicit confirm=true is required to delete a memory"}, status_code=400)

    store = current_learning_store()
    try:
        try:
            mutation = store.delete_memory(
                memory_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        store.close()
    replayed = bool(idempotency_key and isinstance(mutation, dict) and mutation.get("replayed"))
    memory = mutation.get("memory") if idempotency_key and isinstance(mutation, dict) else mutation
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)

    if replayed:
        conversation_store = _conversation_store(current_workspace())
        storage_sync = {"status": "not_requested", "items": []}
        if conversation_store is not None:
            conversation_store.revoke_memory(
                memory,
                project_id=memory.get("project_id"),
            )
            storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
        _mark_learning_memory_sync(
            LearningStore.memory_sync_event_id("memory_delete", memory),
            storage_sync,
        )
        return {
            "status": "deleted",
            "memory_id": memory_id,
            "storage_sync": storage_sync,
            "idempotent_replay": True,
        }

    conversation_store = _conversation_store(current_workspace())
    storage_sync = {"status": "not_requested", "items": []}
    if conversation_store is not None:
        conversation_store.revoke_memory(
            memory,
            project_id=memory.get("project_id"),
        )
        storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
    _mark_learning_memory_sync(
        LearningStore.memory_sync_event_id("memory_delete", memory),
        storage_sync,
    )
    warning = _append_audit_best_effort(
        "learning.memory_deleted",
        _memory_audit_identity(memory),
    )
    response = {"status": "deleted", "memory_id": memory_id, "storage_sync": storage_sync}
    if idempotency_key:
        response["idempotent_replay"] = False
    if warning:
        response["warning"] = warning
    return response


@app.post("/learning/memory-sync/{event_id}/resume")
async def learning_memory_sync_resume(
    event_id: str,
    project_id: str | None = Query(default=None, max_length=128),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    store = current_learning_store()
    try:
        try:
            resumed_result = store.resume_memory_sync(
                event_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        replayed = bool(
            idempotency_key
            and isinstance(resumed_result, dict)
            and resumed_result.get("replayed")
        )
        resumed = (
            bool(resumed_result.get("resumed"))
            if isinstance(resumed_result, dict)
            else bool(resumed_result)
        )
        if resumed:
            warning = None
            if not replayed:
                warning = _append_audit_best_effort(
                    "learning.memory_sync_resumed",
                    {"event_id": event_id, "project_id": project_id},
                )
            response = {"status": "resumed", "event_id": event_id}
            if idempotency_key:
                response["idempotent_replay"] = replayed
            if warning:
                response["warning"] = warning
            return response
        known = store.conn.execute(
            "SELECT 1 FROM learning_sync_outbox WHERE event_id = ?",
            (str(event_id or "").strip(),),
        ).fetchone()
        if known is None:
            return JSONResponse({"error": "memory sync event not found"}, status_code=404)
        response = {"status": "not_paused", "event_id": event_id}
        if idempotency_key:
            response["idempotent_replay"] = replayed
        return JSONResponse(response, status_code=409)
    finally:
        store.close()


@app.get("/learning/memories")
async def learning_memories(project_id: str | None = Query(default=None, max_length=128)):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        scope_policy = _memory_scope_policy(current_workspace(), project_id)
        include_global_memory = scope_policy != "project_only"
        sync_replay_warning = None
        try:
            sync_replay = _replay_learning_memory_outbox(
                store,
                scope_project_id=project_id,
                include_global=include_global_memory,
            )
        except Exception:
            logger.warning("learning memory outbox claim unavailable before list", exc_info=True)
            sync_replay = {"completed": 0, "failed": 1}
            sync_replay_warning = "记忆派生同步暂不可用，已使用本地事实源列表"
        memories = store.list_memories(
            project_id=project_id,
            # A missing project is a global-only management scope; an explicit
            # project keeps all active heads so scheduled/expired records stay
            # editable and are labelled by ``validity_status`` below.
            context_only=not bool(project_id),
            include_metadata=True,
            include_global=include_global_memory,
        )
        reference_time = time.time()
        for memory in memories:
            memory["validity_status"] = _memory_validity_status(
                memory,
                reference_time=reference_time,
            )
        health = store.memory_health(
            project_id=project_id,
            include_global=include_global_memory,
        )
        feedback_health = store.retrieval_feedback_health(
            project_id=project_id,
            include_global=include_global_memory,
        )
        result = {
            "memories": memories,
            "project_id": project_id,
            "scope_policy": scope_policy,
            "sync_replay": sync_replay,
            "health": health,
            "feedback_health": feedback_health,
        }
        if sync_replay_warning:
            result["warning"] = sync_replay_warning
        return result
    finally:
        store.close()


@app.get("/learning/skills")
async def learning_skills(
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    store = None
    try:
        store = current_learning_store()
        summary = sync_project_skills(store, workspace, project_id=project_id)
        skill_path = _project_skill_path(workspace, project_id) or Path(workspace.skills_path).resolve()
        return {
            "skills": _list_scoped_skills(store, project_id=project_id, include_global=True),
            "summary": summary,
            "path": str(skill_path),
            "project_id": project_id,
        }
    except Exception as exc:
        logger.exception("Failed to load project Skills")
        return JSONResponse(
            {
                "error": "本地 Skill 列表读取失败，请点击“刷新列表”重试。",
                "stage": "list",
                **_error_observation(exc),
                "skills": [],
            },
            status_code=503,
        )
    finally:
        if store is not None:
            store.close()


@app.post("/learning/skills/sync")
async def learning_skill_sync(
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    store = None
    try:
        store = current_learning_store()
        summary = sync_project_skills(store, workspace, project_id=project_id)
        warning = None
        if any(summary.get(key, 0) for key in ("imported", "updated", "duplicates_removed")):
            warning = _append_audit_best_effort("learning.project_skills_synced", summary)
        skill_path = _project_skill_path(workspace, project_id) or Path(workspace.skills_path).resolve()
        payload = {
            "skills": _list_scoped_skills(store, project_id=project_id, include_global=True),
            "summary": summary,
            "path": str(skill_path),
            "project_id": project_id,
        }
        if warning:
            payload["warning"] = warning
        return payload
    except Exception as exc:
        logger.exception("Failed to sync project Skills")
        return JSONResponse(
            {
                "error": "本地 Skill 同步失败，请检查 Skill 目录后重试。",
                "stage": "sync",
                **_error_observation(exc),
                "skills": [],
            },
            status_code=503,
        )
    finally:
        if store is not None:
            store.close()


@app.get("/learning/skills/inbox")
async def learning_skill_inbox(
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    skill_path = _project_skill_path(workspace, project_id)
    return {"skills": list_skill_inbox(str(skill_path)) if skill_path is not None else [], "project_id": project_id}


@app.post("/learning/skills/install")
async def learning_skill_install(request: SkillInstallRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        workspace = current_workspace()
        skill_path = _project_skill_path(workspace, request.project_id)
        if skill_path is None:
            return JSONResponse({"error": "项目没有可用的本地 Skill 目录"}, status_code=409)
        result = install_skill_to_inbox(
            str(skill_path),
            request.source,
            request.skill_name,
            request.target,
        )
        warning = None
        if not result.get("replayed", False):
            warning = _append_audit_best_effort(
                "learning.project_skill_downloaded",
                {
                    "skill_name": result["skill_name"],
                    "source": result["source"],
                    "file_count": len(result["files"]),
                    "status": result["status"],
                    "target": result.get("target") or request.target,
                },
            )
        if warning:
            result = {**result, "warning": warning}
        return result
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/learning/skills/inbox/{skill_name}/approve")
async def learning_skill_approve(
    skill_name: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        workspace = current_workspace()
        skill_path = _project_skill_path(workspace, project_id)
        if skill_path is None:
            return JSONResponse({"error": "项目没有可用的本地 Skill 目录"}, status_code=409)
        result = approve_skill_from_inbox(str(skill_path), skill_name)
        warning = None
        if not result.get("replayed", False):
            warning = _append_audit_best_effort(
                "learning.project_skill_adapter_review_ready",
                {"skill_name": result["skill_name"], "target": result["target"]},
            )
        payload = {**result, "skills": []}
        if warning:
            payload["warning"] = warning
        return payload
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/learning/skills/inbox/{skill_name}/adapter")
async def learning_skill_inbox_adapter(
    skill_name: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        workspace = current_workspace()
        skill_path = _project_skill_path(workspace, project_id)
        if skill_path is None:
            return JSONResponse({"error": "项目没有可用的本地 Skill 目录"}, status_code=409)
        result = approve_skill_from_inbox(
            str(skill_path),
            skill_name,
            mark_review=False,
        )
        return result
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/learning/skills/inbox/{skill_name}/activate")
async def learning_skill_activate(
    skill_name: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = None
    try:
        workspace = current_workspace()
        skill_path = _project_skill_path(workspace, project_id)
        if skill_path is None:
            return JSONResponse({"error": "项目没有可用的本地 Skill 目录"}, status_code=409)
        result = activate_skill_from_inbox(str(skill_path), skill_name)
        store = current_learning_store()
        summary = sync_project_skills(store, workspace, project_id=project_id)
        skills = _list_scoped_skills(store, project_id=project_id, include_global=True)
        warning = None
        if not result.get("replayed", False):
            warning = _append_audit_best_effort(
                "learning.project_skill_activated",
                {"skill_name": result["skill_name"], "target": result["target"]},
            )
        payload = {**result, "summary": summary, "skills": skills}
        if warning:
            payload["warning"] = warning
        return payload
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        if store is not None:
            store.close()


@app.post("/learning/skills/{skill_id}/presentation-style")
async def learning_skill_presentation_style(
    skill_id: str,
    project_id: str | None = Query(default=None, max_length=128),
):
    """Prepare an editable local style contract without running Skill code."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    store = None
    try:
        store = current_learning_store()
        try:
            skill, resolved_skill_id = _resolve_skill_for_delete(store, skill_id, project_id=project_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "stage": "resolve"}, status_code=409)
        if skill is None:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        skill_path = _project_skill_path(workspace, skill.get("project_id"))
        if skill_path is None:
            return JSONResponse({"error": "Skill 所属项目没有可用的本地目录", "stage": "source"}, status_code=409)
        result = ensure_project_skill_presentation_adapter(
            str(skill_path),
            str(skill.get("source_path") or ""),
        )
        summary = sync_project_skills(store, workspace, project_id=project_id)
        skills = _list_scoped_skills(store, project_id=project_id, include_global=True)
        warning = _append_audit_best_effort(
            "learning.project_skill_presentation_style_prepared",
            {
                "skill_id": resolved_skill_id,
                "style_path": result["style_path"],
                "created": result["created"],
            },
        )
        payload = {**result, "skill_id": resolved_skill_id, "summary": summary, "skills": skills}
        if warning:
            payload["warning"] = warning
        return payload
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc), "stage": "style"}, status_code=400)
    except Exception as exc:
        logger.exception("Failed to prepare Skill presentation style")
        return JSONResponse(
            {
                "error": "PPT 样式适配器准备失败，请刷新 Skill 列表后重试。",
                "stage": "style",
                **_error_observation(exc),
            },
            status_code=500,
        )
    finally:
        if store is not None:
            store.close()


@app.delete("/learning/skills/{skill_id}")
async def learning_skill_delete(
    skill_id: str,
    project_id: str | None = Query(default=None, max_length=128),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = None
    try:
        try:
            store = current_learning_store()
        except Exception as exc:
            logger.exception("Failed to open Skill store before deleting %s", skill_id)
            return JSONResponse(
                {
                    "error": "本地 Skill 服务暂时不可用，未删除任何内容；请确认工作区已解锁后重试。",
                    "stage": "open",
                    **_error_observation(exc),
                },
                status_code=503,
            )
        normalized_key = str(idempotency_key or "").strip() or None
        deletion_owner = f"skill-delete:{os.getpid()}:{uuid.uuid4().hex}"
        receipt = None
        resume_source_removed = False
        record_already_deleted = False
        if normalized_key:
            try:
                receipt = store.skill_deletion_receipt(normalized_key)
            except ValueError as exc:
                return JSONResponse({"error": str(exc), "stage": "idempotency"}, status_code=422)
            except Exception as exc:
                logger.exception("Failed to read Skill deletion receipt for %s", skill_id)
                return JSONResponse(
                    {
                        "error": "无法读取 Skill 删除收据，未修改本地内容；请稍后使用原幂等键重试。",
                        "stage": "idempotency",
                        **_error_observation(exc),
                    },
                    status_code=500,
                )
            if receipt is not None:
                requested_identifier = str(skill_id or "").strip()
                receipt_project = receipt.get("requested_project_id")
                if receipt_project is None:
                    receipt_project = receipt.get("project_id")
                    if not str(receipt_project or "").strip():
                        return JSONResponse(
                            {
                                "error": "旧 Skill 删除收据缺少发起项目身份，未继续执行；请刷新列表后人工确认。",
                                "stage": "idempotency",
                            },
                            status_code=409,
                        )
                receipt_project_id = str(receipt_project or "").strip() or None
                requested_project_id = str(project_id or "").strip() or None
                if (
                    str(receipt.get("requested_identifier") or "").strip() != requested_identifier
                    or receipt_project_id != requested_project_id
                ):
                    return JSONResponse(
                        {"error": "idempotency_key is already bound to different Skill deletion input"},
                        status_code=409,
                    )
                if receipt.get("status") == "completed":
                    stored_response = receipt.get("response")
                    if not isinstance(stored_response, dict):
                        return JSONResponse(
                            {
                                "error": "Skill 删除已完成，但完成收据内容不可读；请刷新 Skill 列表确认状态。",
                                "stage": "idempotency",
                                "deletion_status": "completed",
                            },
                            status_code=500,
                        )
                    replay = dict(stored_response)
                    replay["idempotency_key"] = normalized_key
                    replay["idempotent_replay"] = True
                    return replay
                resume_source_removed = receipt.get("status") == "source_removed"
        try:
            skill, resolved_skill_id = _resolve_skill_for_delete(store, skill_id, project_id=project_id)
        except ValueError as exc:
            logger.warning("Skill deletion identifier is ambiguous: %s", skill_id)
            return JSONResponse(
                {"error": str(exc), "stage": "resolve"},
                status_code=409,
            )
        except Exception as exc:
            logger.exception("Failed to read Skill %s before deletion", skill_id)
            return JSONResponse(
                {
                    "error": "无法读取待删除 Skill，数据库未修改；请刷新后重试。",
                    "stage": "read",
                    **_error_observation(exc),
                },
                status_code=500,
            )
        if skill is None:
            if resume_source_removed and receipt is not None:
                resolved_skill_id = str(receipt.get("skill_id") or "").strip()
                source_path = str(receipt.get("source_path") or "").replace("\\", "/").strip()
                if not resolved_skill_id or not source_path:
                    return JSONResponse(
                        {
                            "error": "Skill 删除收据缺少可恢复的本地身份，未继续删除；请刷新列表后人工确认。",
                            "stage": "record",
                        },
                        status_code=500,
                    )
                record_already_deleted = True
            else:
                return JSONResponse({"error": "unknown active skill"}, status_code=404)
        else:
            source_path = str(skill.get("source_path") or "").replace("\\", "/").strip()
        if not source_path:
            return JSONResponse(
                {
                    "error": "Skill 没有可验证的本地源路径，未删除数据库记录；请先修复源路径后重试。",
                    "stage": "source_cleanup",
                },
                status_code=409,
            )
        skill_project_id = (
            str(skill.get("project_id") or "").strip() or None
            if skill is not None
            else (str(receipt.get("project_id") or "").strip() or None if receipt is not None else None)
        )
        if normalized_key:
            try:
                deletion_claim = store.claim_skill_deletion(
                    str(skill_id or "").strip(),
                    resolved_skill_id,
                    source_path,
                    normalized_key,
                    project_id=skill_project_id,
                    requested_project_id=project_id,
                    source_digest=(
                        str(skill.get("source_digest") or "").strip()
                        if skill is not None
                        else str(receipt.get("source_digest") or "").strip()
                    ),
                    owner_id=deletion_owner,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc), "stage": "idempotency"}, status_code=409)
            except Exception as exc:
                logger.exception("Failed to claim Skill deletion %s", resolved_skill_id)
                return JSONResponse(
                    {
                        "error": "无法建立 Skill 删除收据，未修改本地内容；请稍后重试。",
                        "stage": "idempotency",
                        **_error_observation(exc),
                    },
                    status_code=500,
                )
            if not deletion_claim.get("claimed"):
                stored_response = deletion_claim.get("response")
                if deletion_claim.get("status") == "completed" and isinstance(stored_response, dict):
                    replay = dict(stored_response)
                    replay["idempotency_key"] = normalized_key
                    replay["idempotent_replay"] = True
                    return replay
                return JSONResponse(
                    {
                        "error": "Skill 删除正在处理中，请稍后使用同一幂等键重试。",
                        "stage": "idempotency",
                        "deletion_status": deletion_claim.get("status") or "in_progress",
                        "idempotency_key": normalized_key,
                    },
                    status_code=409,
                )
            resume_source_removed = deletion_claim.get("status") == "source_removed"

        def recovered_source_result() -> dict[str, Any]:
            source = Path(source_path)
            skill_name = source.parent.name if source.name.casefold() == "skill.md" else source.name
            return {"status": "deleted", "skill_name": skill_name or source.stem, "path": source_path}

        if resume_source_removed or record_already_deleted:
            result = recovered_source_result()
        else:
            try:
                source_root = _project_skill_path(current_workspace(), skill_project_id)
                if source_root is None:
                    if normalized_key:
                        store.mark_skill_deletion_failed(
                            normalized_key,
                            deletion_owner,
                            RuntimeError("Skill project source root is unavailable"),
                        )
                    return JSONResponse(
                        {"error": "Skill 所属项目没有可用的本地目录，未删除数据库记录。", "stage": "source_cleanup"},
                        status_code=409,
                    )
                result = delete_project_skill_source_path(str(source_root), source_path)
            except Exception as exc:
                logger.exception("Failed to clean Skill source %s", resolved_skill_id)
                if normalized_key:
                    try:
                        store.mark_skill_deletion_failed(normalized_key, deletion_owner, exc)
                    except Exception:
                        logger.exception("Failed to record Skill source cleanup failure for %s", resolved_skill_id)
                return JSONResponse(
                    {
                        "error": "Skill 本地目录清理失败，数据库记录已保留；请处理文件占用或路径后重试。",
                        "stage": "source_cleanup",
                        **_error_observation(exc),
                    },
                    status_code=409,
                )
            if normalized_key:
                try:
                    store.mark_skill_deletion_source_removed(normalized_key, deletion_owner)
                except Exception as exc:
                    logger.exception("Failed to record removed Skill source %s", resolved_skill_id)
                    return JSONResponse(
                        {
                            "error": "Skill 本地目录已清理，但删除阶段收据未写入；数据库记录已保留，请使用原幂等键重试。",
                            "stage": "idempotency",
                            **_error_observation(exc),
                        },
                        status_code=500,
                    )
        try:
            deleted = True if record_already_deleted else False
            if not record_already_deleted:
                try:
                    deleted = store.delete_skill(
                        resolved_skill_id,
                        project_id=project_id,
                        include_global=True,
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc):
                        raise
                    deleted = store.delete_skill(resolved_skill_id)
        except Exception as exc:
            logger.exception("Failed to delete Skill record %s", resolved_skill_id)
            return JSONResponse(
                {
                    "error": "本地 Skill 目录已清理，但数据库记录删除失败；请刷新后重试。",
                    "stage": "record",
                    **_error_observation(exc),
                },
                status_code=500,
            )
        if not deleted:
            if normalized_key and resume_source_removed:
                result = recovered_source_result()
            else:
                return JSONResponse({"error": "unknown active skill"}, status_code=404)
        cleanup_warning = _append_audit_best_effort(
            "learning.project_skill_deleted",
            {"skill_id": resolved_skill_id, "source_path": source_path},
        ) or ""
        cleanup_warning_observation = None
        try:
            remaining_skills = _list_scoped_skills(store, project_id=project_id, include_global=True)
        except Exception as exc:
            # The target record is already gone.  Keep the destructive result
            # truthful and let the next explicit refresh report any unrelated
            # legacy-record problem instead of returning a false failure.
            logger.exception("Skill list refresh failed after deleting %s", resolved_skill_id)
            remaining_skills = []
            cleanup_warning_observation = _error_observation(exc)
            cleanup_warning = "删除已完成，但列表刷新失败，请点击刷新列表。" if not cleanup_warning else cleanup_warning
        payload = {**result, "skills": remaining_skills}
        if cleanup_warning:
            payload["warning"] = cleanup_warning
        if cleanup_warning_observation:
            payload["warning_observation"] = cleanup_warning_observation
        if normalized_key:
            payload["idempotency_key"] = normalized_key
            payload["idempotent_replay"] = False
            try:
                store.complete_skill_deletion(normalized_key, deletion_owner, payload)
            except Exception as exc:
                logger.exception("Failed to complete Skill deletion receipt for %s", resolved_skill_id)
                return JSONResponse(
                    {
                        "error": "Skill 已删除，但完成收据未写入；请使用原幂等键重试以确认结果。",
                        "stage": "idempotency",
                        "deletion_status": "source_removed",
                        **_error_observation(exc),
                    },
                    status_code=500,
                )
        return payload
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                logger.exception("Skill store close failed after deleting %s", skill_id)


def _resolve_skill_for_delete(
    store: LearningStore,
    identifier: str,
    *,
    project_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve current and legacy UI identifiers without weakening path safety."""
    requested = str(identifier or "").strip()
    if not requested:
        return None, ""
    try:
        direct = store.get_skill(requested, project_id=project_id, include_global=True)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        direct = store.get_skill(requested)
        if direct is not None and not _skill_scope_matches(direct, project_id, include_global=True):
            direct = None
    if direct is not None:
        return direct, str(direct.get("skill_id") or requested)

    requested_path = requested.replace("\\", "/").strip("/")
    matches: list[dict[str, Any]] = []
    for skill in _list_scoped_skills(store, project_id=project_id, include_global=True):
        skill_id = str(skill.get("skill_id") or "")
        title = str(skill.get("title") or "")
        source_path = str(skill.get("source_path") or "").replace("\\", "/").strip("/")
        aliases = {value for value in (skill_id, title, source_path) if value}
        if requested in aliases or requested_path in {value.strip("/") for value in aliases}:
            matches.append(skill)
    if len(matches) > 1:
        raise ValueError("存在多个匹配的 Skill，请刷新列表后使用唯一条目删除。")
    if not matches:
        return None, requested
    skill = matches[0]
    return skill, str(skill.get("skill_id") or requested)


@app.post("/learning/skills/match")
async def learning_skill_match(request: SkillMatchRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    store = current_learning_store()
    try:
        sync_project_skills(store, workspace, project_id=request.project_id)
        # Skill matching is already a small, structured candidate set.  The
        # learning-store lexical prefilter drops cross-language matches before
        # the bilingual matcher can score them.
        matches = match_skills(
            request.query,
            [
                skill for skill in _list_scoped_skills(
                    store,
                    project_id=request.project_id,
                    include_global=True,
                )
                if skill.get("target") == "conversation" and skill.get("compatibility_status") == "verified"
            ],
            top_k=request.top_k,
            workflow_id=request.workflow_id,
        )
        warning = _append_audit_best_effort(
            "learning.skill_match_performed",
            {
                "query_chars": len(request.query),
                "match_count": len(matches),
            },
        )
        payload = {"matches": [_public_skill_match(match) for match in matches]}
        if warning:
            payload["warning"] = warning
        return payload
    finally:
        store.close()


@app.get("/learning/skills/runs")
async def learning_skill_runs(
    project_id: str | None = Query(default=None, max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {
            "runs": store.list_skill_runs(project_id=project_id, include_global=True),
            "project_id": project_id,
        }
    finally:
        store.close()


def _available_skill_scripts(skill: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only script resources supported by the explicit runner."""
    allowed_suffixes = {".py", ".js", ".mjs"}
    scripts = []
    for resource in skill.get("resource_manifest") or []:
        if not isinstance(resource, dict) or str(resource.get("kind") or "").casefold() != "script":
            continue
        path = str(resource.get("path") or "").replace("\\", "/").strip()
        if path and Path(path).suffix.casefold() in allowed_suffixes:
            scripts.append({
                "path": path,
                "size_bytes": resource.get("size_bytes"),
                "sha256": resource.get("sha256"),
                "execution": "explicit_user_action_only",
            })
    return scripts[:12]


@app.post("/learning/skills/{skill_id}/runs")
async def learning_skill_run(
    skill_id: str,
    request: SkillRunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    if request.mode == "execute":
        return JSONResponse(
            {"error": "通用 Skill 脚本执行已停用；运行时只使用自动适配后的声明式契约。"},
            status_code=410,
        )
    workspace = current_workspace()
    store = current_learning_store()
    try:
        try:
            skill = store.get_skill(
                skill_id,
                project_id=request.project_id,
                include_global=True,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            skill = store.get_skill(skill_id)
            if skill is not None and not _skill_scope_matches(skill, request.project_id, include_global=True):
                skill = None
        if skill is None:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        available_scripts = _available_skill_scripts(skill)
        try:
            plan = create_skill_execution_plan(
                skill,
                request.query,
                matched_score=request.matched_score,
                inputs=request.inputs,
                workflow_id=request.workflow_id,
                project_id=request.project_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        plan["execution"]["available_scripts"] = available_scripts
        if request.mode == "execute" and not request.script_path:
            return JSONResponse(
                {
                    "error": "execute mode requires an explicit script_path",
                    "available_scripts": available_scripts,
                },
                status_code=422,
            )
        try:
            run = store.record_skill_run(
                skill_id,
                query=request.query,
                plan=plan,
                matched_score=request.matched_score,
                inputs=request.inputs,
                idempotency_key=idempotency_key,
                project_id=request.project_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        if request.mode == "plan_only":
            warning = None
            if not run.get("replayed"):
                warning = _append_audit_best_effort(
                    "learning.skill_run_planned",
                    {
                        "skill_id": skill_id,
                        "query_chars": len(request.query),
                        "script_count": len(available_scripts),
                    },
                )
            payload = {
                "run_id": run["run_id"],
                "plan": plan,
                "status": run["status"],
                "replayed": bool(run.get("replayed")),
            }
            if warning:
                payload["warning"] = warning
            return payload

        output_root = Path(workspace.runtime_path) / "skill-runs"
        output_directory = output_root / run["run_id"]
        plan["execution"].update(
            {
                "mode": "explicit_script",
                "script_path": request.script_path,
                "output_directory": str(output_directory),
            }
        )
        try:
            execution = await run_in_threadpool(
                execute_skill_script,
                workspace.skills_path,
                skill,
                request.script_path,
                args=request.args,
                input_payload=request.inputs,
                output_directory=str(output_directory),
                allowed_output_root=str(output_root),
                timeout_seconds=request.timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Skill script execution failed before returning a result: %s", skill_id)
            execution = {
                "status": "failed",
                "script_path": request.script_path,
                "returncode": None,
                "duration_ms": 0.0,
                "stdout": "",
                "stderr": "",
                "output_truncated": False,
                "error": f"Skill 脚本执行异常：{exc}",
            }
        terminal_status = "completed" if execution.get("status") == "completed" else "failed"
        outcome = json.dumps(
            {
                "mode": "explicit_script",
                "script_path": execution.get("script_path"),
                "output_directory": str(output_directory),
                "status": execution.get("status"),
                "returncode": execution.get("returncode"),
                "duration_ms": execution.get("duration_ms"),
                "stdout": str(execution.get("stdout") or "")[:900],
                "stderr": str(execution.get("stderr") or "")[:900],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        failure_reason = str(
            execution.get("error")
            or execution.get("stderr")
            or ("Skill 脚本未通过" if terminal_status != "completed" else "")
        )
        updated = store.update_skill_run(
            run["run_id"],
            status=terminal_status,
            outcome=outcome,
            failure_reason=failure_reason,
        )
        if updated is None:
            return JSONResponse(
                {"error": "Skill 执行结果未能写入本地运行记录", "run_id": run["run_id"], "execution": execution},
                status_code=500,
            )
        warning = _append_audit_best_effort(
            "learning.skill_run_executed",
            {
                "skill_id": skill_id,
                "run_id": run["run_id"],
                "script_path": execution.get("script_path"),
                "status": execution.get("status"),
                "returncode": execution.get("returncode"),
                "duration_ms": execution.get("duration_ms"),
            },
        )
        payload = {
            "run_id": run["run_id"],
            "skill_id": skill_id,
            "status": execution.get("status"),
            "run_status": updated["status"],
            "script_path": execution.get("script_path"),
            "output_directory": str(output_directory),
            "returncode": execution.get("returncode"),
            "duration_ms": execution.get("duration_ms"),
            "stdout": execution.get("stdout") or "",
            "stderr": execution.get("stderr") or "",
            "output_truncated": bool(execution.get("output_truncated")),
            "error": execution.get("error") or "",
            "available_scripts": available_scripts,
            "plan": plan,
        }
        if warning:
            payload["warning"] = warning
        return payload
    finally:
        store.close()


@app.post("/learning/search")
async def learning_search(request: LearningSearchRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        scope_policy = _memory_scope_policy(current_workspace(), request.project_id)
        sync_replay = {"completed": 0, "failed": 0}
        sync_replay_warning = None
        if callable(getattr(store, "claim_pending_memory_syncs", None)):
            try:
                sync_replay = _replay_learning_memory_outbox(
                    store,
                    scope_project_id=request.project_id,
                    include_global=scope_policy != "project_only",
                )
                if sync_replay["failed"]:
                    logger.warning("learning memory outbox replay degraded before search: %s", sync_replay)
            except Exception:
                # The SQLite fact source remains usable when the derived
                # projection claim itself is temporarily unavailable.
                logger.warning("learning memory outbox claim unavailable before search", exc_info=True)
                sync_replay = {"completed": 0, "failed": 1}
                sync_replay_warning = "记忆派生同步暂不可用，已使用本地事实源检索"
        semantic_memory = {
            "scores": {},
            "record_versions": {},
            "stale_projection_count": 0,
            "backend": None,
        }
        raw_result = store.search(
            request.query,
            request.top_k_memory,
            request.top_k_skill,
            project_id=request.project_id,
            # A missing project is a global-only search boundary.  Passing
            # ``False`` here would make the store return every project asset
            # because there is no SQL scope predicate to constrain it.
            context_only=True,
            include_global=scope_policy != "project_only",
            as_of=request.as_of,
            workflow_id=request.workflow_id,
        )
        keyword_memory_count = len(raw_result.get("memories") or [])
        list_memories = getattr(store, "list_memories", None)
        if request.top_k_memory > 0 and callable(list_memories):
            authoritative_memories = list_memories(
                project_id=request.project_id,
                context_only=True,
                include_metadata=True,
                include_global=scope_policy != "project_only",
                as_of=request.as_of,
                workflow_id=request.workflow_id,
            )
            authoritative_versions: dict[str, int] = {}
            for memory in authoritative_memories:
                memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "").strip()
                if not memory_id or memory_id in authoritative_versions:
                    continue
                try:
                    authoritative_versions[memory_id] = max(
                        1,
                        int(memory.get("record_version") or 1),
                    )
                except (TypeError, ValueError):
                    continue
            semantic_memory = _semantic_memory_scores(
                request.query,
                project_id=request.project_id,
                include_global=scope_policy != "project_only",
                as_of=request.as_of,
                workflow_id=request.workflow_id,
                authoritative_versions=authoritative_versions,
            )
            semantic_scores = dict(semantic_memory.get("scores") or {})
            if semantic_scores:
                combined_memories: list[dict[str, Any]] = []
                seen_memory_ids: set[str] = set()
                for item in raw_result.get("memories") or []:
                    memory_id = str(item.get("memory_id") or item.get("asset_id") or "").strip()
                    if memory_id:
                        seen_memory_ids.add(memory_id)
                    keyword_score = float(item.get("score") or 0.0)
                    semantic_score = float(semantic_scores.get(memory_id) or 0.0)
                    combined_memories.append({
                        **item,
                        "score": max(keyword_score, semantic_score),
                    })
                for memory in authoritative_memories:
                    memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "").strip()
                    if not memory_id or memory_id in seen_memory_ids:
                        continue
                    semantic_score = float(semantic_scores.get(memory_id) or 0.0)
                    if semantic_score <= 0.0:
                        continue
                    combined_memories.append({
                        **memory,
                        "asset_id": memory.get("asset_id") or memory_id,
                        "score": semantic_score,
                    })
                    seen_memory_ids.add(memory_id)
                raw_result = {**raw_result, "memories": combined_memories}
        memory_gate = gate_memory_context(
            request.query,
            raw_result.get("memories") or [],
            max_items=request.top_k_memory,
            reference_timestamp=request.as_of,
            project_id=request.project_id,
            include_global=scope_policy != "project_only",
            workflow_id=request.workflow_id,
        )
        result = {
            **raw_result,
            "memories": memory_gate["memories"],
            "memory_gate": memory_gate["gate"],
            "memory_retrieval": {
                "keyword_count": keyword_memory_count,
                "semantic_count": len(semantic_memory.get("scores") or {}),
                "semantic_backend": semantic_memory.get("backend"),
                "semantic_stale_projection_count": int(
                    semantic_memory.get("stale_projection_count") or 0
                ),
            },
            "memory_sync_replay": sync_replay,
        }
        runtime_metrics.record_memory_gate(
            result["memory_gate"]["decision"],
            result["memory_gate"]["accepted_count"],
            result["memory_gate"]["rejected_count"],
            result["memory_gate"].get("decision_reason"),
        )
        warning_messages = []
        if sync_replay_warning:
            warning_messages.append(sync_replay_warning)
        audit_warning = _append_audit_best_effort(
            "learning.search_performed",
            {
                "query_chars": len(request.query),
                "project_id": request.project_id,
                "workflow_id": request.workflow_id,
                "memory_scope_policy": scope_policy,
                "memory_count": len(result["memories"]),
                "skill_count": len(result["skills"]),
                "memory_gate": {
                    "decision": result["memory_gate"]["decision"],
                    "decision_reason": result["memory_gate"].get("decision_reason"),
                    "accepted_count": result["memory_gate"]["accepted_count"],
                    "rejected_count": result["memory_gate"]["rejected_count"],
                },
            },
        )
        if audit_warning:
            warning_messages.append(audit_warning)
        if warning_messages:
            result["warning"] = "；".join(warning_messages)
        return result
    finally:
        store.close()


def _public_skill_match(match: dict) -> dict:
    return {
        "skill_id": match.get("skill_id") or "",
        "title": match.get("title") or "",
        "score": match.get("score") or 0,
        "reason": match.get("reason") or "",
    }


def _presentation_tool_generate(arguments: dict) -> dict:
    locked_response = require_software_unlock()
    if locked_response is not None:
        raise RuntimeError("workspace must be unlocked")
    payload = dict(arguments)
    project_id = str(payload.get("project_id") or "").strip() or None
    if project_id:
        catalog = ProjectCatalog(current_workspace())
        try:
            if catalog.conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone() is None:
                raise ValueError("unknown project")
            payload["source_version_ids"] = _verified_source_version_ids(
                catalog,
                project_id,
                payload.get("source_version_ids") or [],
            )
        finally:
            catalog.close()
    service = current_presentation_service()
    task = service.create_plan(payload)
    runtime_metrics.increment("presentation_tasks_started")
    replayed = bool(task.get("replayed"))
    public_task = service.get_task(task["task_id"])
    started = (
        False
        if public_task.get("status") in {"completed", "cancelled"}
        else _start_presentation_thread(task["task_id"])
    )
    return {
        **public_task,
        "replayed": replayed,
        "worker_started": started,
    }


def _presentation_tool_confirm(arguments: dict) -> dict:
    locked_response = require_software_unlock()
    if locked_response is not None:
        raise RuntimeError("workspace must be unlocked")
    service = current_presentation_service()
    plan_task_id = str(arguments.get("plan_task_id") or "")
    existing = service.get_task(plan_task_id)
    scope_error = _project_scope_error(
        existing,
        arguments.get("project_id"),
        resource_label="presentation task",
    )
    if scope_error is not None:
        raise ValueError(scope_error)
    task = service.confirm_plan(plan_task_id, arguments["plan_digest"])
    started = (
        False
        if task.get("status") in {"completed", "cancelled"}
        else _start_presentation_thread(task["task_id"])
    )
    return {**task, "worker_started": started}


tool_registry = build_default_tool_registry(
    runtime_status_handler=lambda _arguments: public_runtime_state(),
    audit_recent_handler=lambda arguments: {
        "events": current_audit_logger().read_recent(limit=max(1, min(int(arguments.get("limit", 30)), 100)))
    },
    workspace_diagnostics_handler=lambda _arguments: workspace_diagnostics(),
    product_diagnostics_handler=lambda _arguments: product_diagnostics(),
    audit_verify_handler=lambda arguments: audit_verify_report(arguments.get("limit")),
    presentation_generate_handler=_presentation_tool_generate,
    presentation_confirm_handler=_presentation_tool_confirm,
)


@app.get("/tools/list")
async def tools_list():
    return {"tools": tool_registry.list_tools()}


@app.post("/tools/call")
async def tools_call(request: ToolCallRequest):
    try:
        spec, arguments = tool_registry.validate_call(request.name, request.arguments)
        if spec.requires_unlocked_workspace:
            locked_response = require_software_unlock()
            if locked_response is not None:
                return locked_response
        return {"result": spec.handler(arguments)}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

@app.get("/audit/recent")
async def audit_recent(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    return {
        "events": current_audit_logger().read_recent(limit=safe_limit)
    }

@app.get("/audit/verify")
async def audit_verify(limit: int | None = None):
    return audit_verify_report(limit)


@app.post("/shutdown")
async def shutdown():
    _cleanup_portable_session()
    warning = _append_audit_best_effort("sidecar.shutdown_requested", {})
    handler = getattr(app.state, "shutdown_handler", None)
    if callable(handler):
        await _call_shutdown_handler(handler)
        payload = {"status": "shutdown_requested"}
        if warning:
            payload["warning"] = warning
        return payload
    payload = {"status": "shutdown_unavailable"}
    if warning:
        payload["warning"] = warning
    return JSONResponse(payload, status_code=503)

@app.post("/runtime/lock")
async def runtime_lock(request: RuntimeRequest):
    _cleanup_portable_session()
    return lock_runtime(request.reason)

@app.post("/runtime/unlock")
async def runtime_unlock(request: RuntimeRequest):
    state = get_runtime_state()
    active_drive = request.active_drive or state.get("active_drive")
    if active_drive:
        if not is_souldrive_workspace(active_drive):
            return JSONResponse({"error": "active_drive is not an initialized SoulDrive workspace"}, status_code=400)
        workspace = resolve_workspace(active_drive)
    elif state.get("workspace_path"):
        workspace = SoulDriveWorkspace(state["workspace_path"]).ensure()
    else:
        return JSONResponse(
            {
                "error": "SoulDrive removable workspace is not mounted",
                "status": "locked",
                "reason": "waiting for removable SoulDrive workspace",
            },
            status_code=423,
        )
    return unlock_runtime(
        auth_level=request.auth_level,
        hardware_sn=request.hardware_sn,
        active_drive=active_drive,
        workspace_path=workspace.root_path,
        reason=request.reason,
    )
