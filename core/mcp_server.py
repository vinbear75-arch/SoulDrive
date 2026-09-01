import time
import uuid
import sqlite3
import inspect
import os
import json
import re
import hashlib
import subprocess
import sys
import threading
import shutil
import secrets
import tempfile
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI
from fastapi import File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from core.api_security import TOKEN_HEADER, is_authorized_token
from core.audit_log import AuditLogger, default_audit_logger
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
from core.conversation_store import ConversationStore
from core.state_backends import ProductionSessionStore
from core.learning_validator import validate_learning_candidates
from core.interaction_context import (
    VISUAL_EVENT_MODALITIES,
    normalize_interaction_context,
    summarize_interaction_context,
)
from core.multimodal_workflows import (
    build_workflow_query,
    get_workflow_spec,
    list_workflow_specs,
    plan_workflow,
)
from core.soulalign import decide as soulalign_decide
from core.topic5_runtime import DEFAULT_MAX_NEW_TOKENS, clear_topic5_runtime, get_topic5_runtime
from core.memory_runtime import gate_memory_context
from core.observability import runtime_metrics
from core.paper_importer import file_md5, import_paper_into_workspace
from core.plain_document_parser import PlainDocumentParser
from core.presentation_service import PresentationService
from core.project_catalog import ProjectCatalog
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
from core.skill_runtime import create_skill_execution_plan, execute_skill_script, match_skills
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
        return store.health()
    except Exception as exc:
        return {
            "schema_version": "storage_health_v1",
            "status": "error",
            "degraded": True,
            "error": str(exc),
        }


def cleanup_runtime():
    global kb, rag, loaded_workspace_path
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


def _indexer_worker_command(workspace: SoulDriveWorkspace, auth_level: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "indexer",
            "--workspace-path",
            workspace.root_path,
            "--auth-level",
            auth_level,
        ]
    return [
        sys.executable,
        "-m",
        "core.indexer_worker",
        "--workspace-path",
        workspace.root_path,
        "--auth-level",
        auth_level,
    ]


def _subprocess_creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _start_indexer_worker(workspace: SoulDriveWorkspace, auth_level: str):
    global indexer_process
    command = _indexer_worker_command(workspace, auth_level)
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


def public_runtime_state():
    _reconcile_indexer_state()
    state = effective_runtime_state()
    public_state = dict(state)
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


def current_learning_store() -> LearningStore:
    workspace = current_workspace()
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        raise RuntimeError("workspace keys are not available")
    return LearningStore(workspace, keys)


def sync_project_skills(store: LearningStore, workspace) -> dict[str, int]:
    """Import changed project-local skills before any consumer reads the skill store."""
    summary = {"imported": 0, "updated": 0, "unchanged": 0, "duplicates_removed": 0}
    discovered = discover_project_skills(workspace.skills_path)
    duplicate_paths = {
        path
        for skill in discovered
        for path in skill.pop("duplicate_source_paths", [])
    }
    for existing in store.list_skills():
        if existing.get("source_path") in duplicate_paths and store.archive_skill(existing["skill_id"]):
            summary["duplicates_removed"] += 1
    for skill in discovered:
        summary[store.sync_project_skill(skill)] += 1
    return summary


def require_software_unlock():
    state = effective_runtime_state()
    if state.get("locked") or not state.get("software_unlocked"):
        current_audit_logger().append_event("security.password_required", {
            "reason": state.get("security_reason") or state.get("reason") or "workspace passphrase required",
        })
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


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=DEFAULT_RAG_TOP_K, ge=1, le=20)
    project_id: str | None = Field(default=None, max_length=128)


class RetrievalFeedbackRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    corrected_query: str = Field(min_length=1, max_length=6000)
    scope: str = Field(default="global", pattern=r"^(global|project)$")
    project_id: str | None = Field(default=None, max_length=128)


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
    has_audio: bool = False
    has_image: bool = False
    speech_text: str = Field(default="", max_length=6000)
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


class ReflectionDecision(BaseModel):
    candidate_id: str = Field(max_length=128)
    action: str = Field(pattern="^(accept|reject)$")
    edited_title: str | None = Field(default=None, max_length=240)
    edited_content: str | None = Field(default=None, max_length=4000)


class ReflectionDecideRequest(BaseModel):
    decisions: list[ReflectionDecision] = Field(default_factory=list, max_length=100)
    project_id: str | None = Field(default=None, max_length=128)


class RuntimeMemoryRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    project_id: str | None = Field(default=None, max_length=128)


class LearningMemoryUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=4000)
    expected_updated_at: float | None = Field(default=None, ge=0)


class LearningSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k_memory: int = Field(default=3, ge=0, le=10)
    top_k_skill: int = Field(default=2, ge=0, le=10)


class SkillMatchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=3, ge=1, le=10)


class SkillRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    matched_score: float = Field(default=0.0, ge=0.0, le=1.0)
    inputs: dict = Field(default_factory=dict)
    mode: str = Field(default="plan_only", pattern=r"^(plan_only|execute)$")
    script_path: str | None = Field(default=None, max_length=260)
    args: list[str] = Field(default_factory=list, max_length=12)
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=120.0)


class SkillInstallRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    skill_name: str | None = Field(default=None, max_length=64)
    target: str = Field(default="conversation", pattern=r"^(conversation|presentation)$")


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
    name: str = Field(min_length=1, max_length=260)


class PresentationGenerateRequest(BaseModel):
    source_filenames: list[str] = Field(min_length=1, max_length=12)
    project_id: str | None = Field(default=None, max_length=128)
    source_version_ids: list[str] = Field(default_factory=list, max_length=64)
    title: str = Field(default="", max_length=120)
    audience: str = Field(default="research", pattern="^(research|technical|government)$")
    max_slides: int = Field(default=10, ge=4, le=16)
    open_in_wps: bool = True
    style_skill_id: str | None = Field(default=None, max_length=128)


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
    recipient_query: str = Field(min_length=1, max_length=240)
    subject: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=12000)
    source_version_ids: list[str] = Field(default_factory=list, max_length=64)
    attachment_refs: list[str] = Field(default_factory=list, max_length=12)


class ScheduleDraftRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
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
    name: str = Field(min_length=1, max_length=260)
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

@app.post("/chat")
async def chat_stream(request: ChatRequest):
    """
    前端问答主接口。
    接收自然语言提问 -> 执行 RAG 流水线 -> 以 SSE (Server-Sent Events) 流式返回。
    """
    _reconcile_indexer_state()
    trace_id = str(uuid.uuid4())
    runtime_metrics.increment("chat_requests")
    audit_logger = current_audit_logger()
    indexing_busy = str((get_runtime_state().get("indexing") or {}).get("status") or "idle") in {"queued", "scanning", "indexing"}
    history = [message.model_dump(exclude_none=True) for message in request.history]
    conversation_store = None
    storage_context: dict[str, Any] = {}
    try:
        workspace = current_workspace()
    except RuntimeError as exc:
        # Keep the streaming contract for transient startup/indexer states;
        # do not let workspace discovery turn an observable status into HTTP 500.
        message = (
            "知识索引正在更新，请等待索引完成后再提问。"
            if indexing_busy
            else f"本地工作区暂不可用：{str(exc)}"
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
    if request.session_id and not indexing_busy:
        try:
            conversation_store = _conversation_store(workspace)
            if conversation_store:
                session = conversation_store.session_metadata(request.session_id)
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
            if conversation_store and not history:
                storage_context = conversation_store.load_context(request.session_id, limit=8)
                history = storage_context.get("history") or []
        except Exception:
            logger.exception("failed to load conversation history")
    workflow_id = session_workflow_id or request.workflow_id
    try:
        rag_query = build_workflow_query(workflow_id, request.query)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
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
        audit_logger.append_event(
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
    audit_logger.append_event("chat.requested", {
        "query_chars": len(request.query),
        "top_k": clamp_chat_top_k(request.top_k),
        "workflow_id": workflow_id,
        "project_mode": project_mode,
        "interaction_context": interaction_summary,
    }, trace_id=trace_id)

    # 工业标准的流式返回闭包
    def event_generator():
        chat_started_at = time.perf_counter()
        first_token_ms = None
        success = False
        output = []
        learning_context = None
        _reconcile_indexer_state()
        indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
        if indexing_status in {"queued", "scanning", "indexing"}:
            yield "知识索引正在更新。为避免回答使用旧知识库，请等待索引完成后再提问。"
            return
        locked_response = require_software_unlock()
        if locked_response is not None:
            cleanup_runtime()
            state = get_runtime_state()
            current_audit_logger().append_event("chat.rejected_locked", {
                "reason": state.get("reason", "storage device removed"),
            }, trace_id=trace_id)
            yield (
                f"本地知识引擎已锁定：{state.get('reason', 'workspace locked')}。"
                "请解锁当前私有工作区；若使用便携模式，请确认工作区存储已连接。"
            )
            return

        try:
            request_id = request.request_id or trace_id
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
            )
            retrieval_feedback = _retrieval_feedback_for_query(
                request.query,
                project_id=project_id,
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

            if not supports_progress_callbacks:
                # Keep lightweight test doubles and older local plugins compatible.
                for chunk in active_rag.generate_response_stream(
                    query=rag_query,
                    top_k=clamp_chat_top_k(request.top_k),
                    trace_id=trace_id,
                    learning_context=learning_context,
                    interaction_context=rag_interaction_context,
                ):
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - chat_started_at) * 1000)
                    output.append(chunk)
                    yield chunk
            else:
                # The RAG engine validates citations after model generation. Run it
                # in a worker so the connection stays alive during local retrieval
                # without exposing internal pipeline status in the chat transcript.
                updates: Queue[tuple[str, Any]] = Queue()

                def run_rag() -> None:
                    try:
                        for chunk in active_rag.generate_response_stream(
                            query=rag_query,
                            top_k=clamp_chat_top_k(request.top_k),
                            trace_id=trace_id,
                            learning_context=learning_context,
                            interaction_context=rag_interaction_context,
                        ):
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
                        # A zero-width heartbeat keeps the response alive without
                        # polluting the persisted assistant message.
                        yield "\u200b"
                        continue
                    if kind == "output":
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
            success = True
            _finish_skill_execution(learning_context, status="completed", outcome={
                "mode": "chat_context_injection",
                "response_chars": len("".join(output)),
                "response_emitted": True,
            })
            if conversation_store and request.session_id:
                conversation_store.append_message(
                    request.session_id,
                    "assistant",
                    "".join(output),
                    metadata={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "status": "completed",
                        "project_id": project_id,
                    },
                    request_id=request_id,
                    project_id=project_id,
                )
                conversation_store.compact_session(
                    request.session_id,
                    project_id=project_id,
                    threshold=int(os.environ.get("SOULDRIVE_SESSION_COMPACTION_THRESHOLD", "16")),
                )
        except Exception as exc:
            runtime_metrics.increment("chat_failures")
            runtime_metrics.record_error(str(exc))
            _finish_skill_execution(
                learning_context,
                status="failed",
                failure_reason=str(exc),
            )
            current_audit_logger().append_event("chat.failed", {
                "query_chars": len(request.query),
                "error": str(exc),
                "interaction_context": interaction_summary,
            }, trace_id=trace_id)
            if conversation_store and request.session_id:
                try:
                    conversation_store.append_message(
                        request.session_id,
                        "assistant",
                        "本地推理服务暂时不可用，已记录诊断事件。",
                        metadata={"trace_id": trace_id, "request_id": request.request_id or trace_id, "status": "failed"},
                        request_id=request.request_id or trace_id,
                    )
                except Exception:
                    logger.exception("failed to persist failed chat turn")
            yield "本地推理服务暂时不可用，已记录诊断事件。"
        finally:
            total_ms = int((time.perf_counter() - chat_started_at) * 1000)
            runtime_metrics.record_chat_latency(
                first_token_ms if first_token_ms is not None else total_ms,
                total_ms,
                success=success,
            )
            if success:
                current_audit_logger().append_event(
                    "chat.completed",
                    {
                        "first_token_ms": first_token_ms if first_token_ms is not None else total_ms,
                        "total_ms": total_ms,
                        "under_5s": total_ms < 5000,
                        "interaction_context": interaction_summary,
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
    current_audit_logger().append_event("multimodal.decision", {
        "intent": decision["intent"],
        "action": decision["action"],
        "event_count": len(events),
        "side_effect": "none",
    })
    return decision


@app.get("/multimodal/workflows")
async def multimodal_workflows(project_id: str | None = Query(default=None, max_length=128)):
    """List business-first task contracts without loading ASR/OCR/VLM models."""
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    approvals = _playbook_approvals_for_project(current_workspace(), project_id)
    return {"workflows": list_workflow_specs(approvals)}


@app.post("/multimodal/workflows/plan")
async def multimodal_workflow_plan(request: MultimodalWorkflowPlanRequest):
    """Check task readiness before spending time on perception or generation."""
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        plan = plan_workflow(
            request.workflow_id,
            project_id=request.project_id,
            document_names=request.document_names,
            has_audio=request.has_audio,
            has_image=request.has_image,
            speech_text=request.speech_text,
            selected_text=request.selected_text,
            baseline_run_id=request.baseline_run_id,
            current_run_id=request.current_run_id,
            approval=_playbook_approval_for_project(
                current_workspace(),
                request.project_id,
                request.workflow_id,
            ),
            require_approval=True,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    experiment_comparison = None
    if request.workflow_id == "experiment_compare" and (request.baseline_run_id or request.current_run_id):
        try:
            experiment_comparison = _load_explicit_experiment_comparison(
                current_workspace(),
                request.project_id,
                ActiveInteractionContext(
                    project_id=request.project_id,
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
    current_audit_logger().append_event(
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
    current_audit_logger().append_event(
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
    current_audit_logger().append_event(
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
    current_audit_logger().append_event(
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
    current_audit_logger().append_event(
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
        current_audit_logger().append_event(
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
async def session_history(session_id: str, limit: int = 8, before_sequence: int | None = Query(default=None, ge=1)):
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
        if store.session_metadata(session_id) is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
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
        session = store.create_session(session_id, project_id=None, title=request.title, workflow_id=request.workflow_id)
        current_audit_logger().append_event("session.created", {"project_id": None, "session_id": session_id})
        return {"status": "created", "session": session}
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
        session = store.create_session(session_id, project_id=project_id, title=request.title, workflow_id=request.workflow_id)
        current_audit_logger().append_event("session.created", {"project_id": project_id, "session_id": session_id})
        return {"status": "created", "session": session}
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
        current_audit_logger().append_event("session.deleted", {"project_id": project_id, "session_id": session_id})
        return {"status": "deleted", "session": deleted}
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
    current_audit_logger().append_event("security.keystore_initialized", {"no_recovery": True})
    mark_software_unlocked()
    return {"initialized": True, "software_unlocked": True, "state": public_runtime_state()}

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
        current_audit_logger().append_event("security.unlock_failed", {"reason": "incorrect passphrase"})
        return JSONResponse({"error": "incorrect passphrase"}, status_code=403)

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


def _document_payload_by_name(workspace: SoulDriveWorkspace, name: str) -> tuple[bytes | None, str | None]:
    """读取一个文档本体；同名多文档时拒绝猜测。"""
    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        store = SecureDocumentStore(workspace, workspace_keys)
        try:
            matches = [item for item in store.iter_documents() if item["name"] == name]
            if len(matches) > 1:
                return None, "同名文档不唯一，请先删除重复文档后再归为全局"
            if matches:
                return store.read_document_bytes(matches[0]["object_id"]), None
        finally:
            store.close()

    source = Path(workspace.papers_path) / name
    if source.exists() and source.is_file() and source.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
        return source.read_bytes(), None
    return None, None


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
    name = request.name.strip()
    payload, payload_error = _document_payload_by_name(workspace, name)
    if payload_error:
        return JSONResponse({"error": payload_error}, status_code=409)
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)

    catalog = ProjectCatalog(workspace)
    try:
        result = catalog.unbind_imported_document(name, hashlib.md5(payload).hexdigest())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        catalog.close()

    if result["status"] == "globalized":
        current_audit_logger().append_event("document.globalized", {
            "source_filename": name,
            "previous_project_ids": result["project_ids"],
            "reindex_required": True,
        })
    return {
        "status": result["status"],
        "scope": "global",
        "name": name,
        "previous_project_ids": result["project_ids"],
        "reindex_required": result["status"] == "globalized",
    }


@app.get("/documents/preview")
async def documents_preview(name: str = Query(min_length=1, max_length=260)):
    """Return a bounded, read-only preview for the in-app document viewer."""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    payload: bytes | None = None
    media_type = document_media_type(name)
    keys = get_workspace_keys(workspace.root_path)
    if keys is not None:
        store = SecureDocumentStore(workspace, keys)
        try:
            for item in store.iter_documents():
                if item["name"] == name:
                    payload = store.read_document_bytes(item["object_id"])
                    media_type = item.get("media_type") or media_type
                    break
        finally:
            store.close()
    if payload is None:
        source = Path(workspace.papers_path) / name
        if source.exists() and source.is_file() and source.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
            payload = source.read_bytes()
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)

    indexed_preview = _indexed_document_preview(workspace, name)
    if indexed_preview["chunks"]:
        return {
            "name": name,
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
        current_audit_logger().append_event("document.delete_authorization_failed", {"reason": "incorrect passphrase"})
        return JSONResponse({"error": "incorrect passphrase"}, status_code=403)

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


def _delete_indexed_document(workspace: SoulDriveWorkspace, name: str, payload: bytes | None) -> None:
    ensure_runtime_loaded()
    existing = kb.collection.get(where={"source_filename": name}, include=["metadatas"])
    child_ids = list(existing.get("ids") or [])
    metadatas = list(existing.get("metadatas") or [])
    project_ids = {
        str(metadata.get("project_id") or "")
        for metadata in metadatas
        if metadata and str(metadata.get("project_id") or "")
    }
    if len(project_ids) > 1:
        raise ValueError("同名文档属于多个项目，请使用文档 ID 删除，系统未执行删除")
    parent_ids = sorted({
        str(metadata.get("parent_id") or "")
        for metadata in metadatas
        if metadata and metadata.get("parent_id")
    })
    if payload is not None:
        kb.delete_document_indexes(hashlib.md5(payload).hexdigest())
    kb.delete_index_records(child_ids=child_ids, parent_ids=parent_ids)
    if Path(workspace.parent_doc_path).exists():
        with sqlite3.connect(str(workspace.parent_doc_path)) as conn:
            rows = conn.execute(
                "SELECT parent_id FROM parent_documents WHERE json_extract(metadata_json, '$.source_filename') = ?",
                (name,),
            ).fetchall()
            durable_parent_ids = [str(row[0]) for row in rows]
            if durable_parent_ids:
                placeholders = ",".join("?" for _ in durable_parent_ids)
                conn.execute(f"DELETE FROM parent_documents WHERE parent_id IN ({placeholders})", durable_parent_ids)
                conn.commit()
    from core.document_usage import forget_document
    forget_document(workspace.root_path, name)


@app.post("/documents/delete")
async def documents_delete(request: DocumentDeleteRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    workspace = current_workspace()
    if not _document_delete_authorized(workspace, request.authorization):
        return JSONResponse({"error": "document delete authorization expired"}, status_code=403)

    payload: bytes | None = None
    secure_document = False
    keys = get_workspace_keys(workspace.root_path)
    if keys is not None:
        store = SecureDocumentStore(workspace, keys)
        try:
            payload = store.read_document_named(request.name)
            secure_document = payload is not None
        finally:
            store.close()
    source = Path(workspace.papers_path) / request.name
    if payload is None and source.exists() and source.is_file():
        payload = source.read_bytes()
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)

    try:
        _delete_indexed_document(workspace, request.name, payload)
    except Exception as exc:
        logger.exception("[Documents] Refusing source deletion because index cleanup failed for %s", request.name)
        return JSONResponse({"error": f"index cleanup failed: {exc}"}, status_code=500)

    if secure_document:
        store = SecureDocumentStore(workspace, keys)
        try:
            store.delete_document(request.name)
        finally:
            store.close()
    else:
        source.unlink()
    current_audit_logger().append_event("document.deleted", {"source_filename": request.name})
    return {"deleted": True, "name": request.name}


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
async def project_create(request: ProjectCreateRequest):
    """在本地项目目录创建项目，不产生云端或外部副作用。"""
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        project_id = request.project_id or f"project_{uuid.uuid4().hex}"
        root_path = _validated_project_root(current_workspace(), request.root_path, project_id)
        root_path.mkdir(parents=True, exist_ok=True)
        project_id = catalog.create_project(
            request.name.strip(), project_id=project_id, project_mode=request.project_mode,
            root_path=str(root_path), instructions=request.instructions,
            memory_policy=request.memory_policy,
        )
        current_audit_logger().append_event(
            "project.created", {"project_id": project_id, "project_mode": request.project_mode, "root_path": str(root_path)}
        )
        return {"project_id": project_id, "status": "created", "root_path": str(root_path)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
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
    _reconcile_indexer_state()
    indexing_status = str((get_runtime_state().get("indexing") or {}).get("status") or "idle")
    if indexing_status in {"queued", "scanning", "indexing"}:
        return JSONResponse({"error": "知识索引正在更新，请完成后再删除项目"}, status_code=409)

    catalog = ProjectCatalog(workspace)
    try:
        project = catalog.conn.execute(
            "SELECT project_id FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
    finally:
        catalog.close()

    try:
        identity = _project_index_identity(workspace, project_id)
        # Close open vector/session handles before mutating their backing stores.
        cleanup_runtime()
        index_report = _delete_project_indexes(workspace, project_id, identity)
        store = _conversation_store(workspace)
        if store is None:
            raise RuntimeError("workspace keys unavailable")
        sessions = store.list_sessions(project_id)
        for session in sessions:
            store.delete_session(str(session["session_id"]), project_id=project_id)

        catalog = ProjectCatalog(workspace)
        try:
            deleted = catalog.delete_project(project_id)
        finally:
            catalog.close()
        if deleted is None:
            return JSONResponse({"error": "project disappeared during deletion"}, status_code=409)

        root_report = _delete_project_root(workspace, deleted.get("root_path"))
        result = {
            "status": "deleted",
            **deleted,
            **root_report,
            **index_report,
            "session_count": len(sessions),
        }
        current_audit_logger().append_event("project.deleted", {
            "project_id": project_id,
            "index_child_count": result["index_child_count"],
            "index_parent_count": result["index_parent_count"],
            "session_count": result["session_count"],
            "root_deleted": result["root_deleted"],
        })
        return result
    except Exception as exc:
        logger.exception("project deletion failed for %s", project_id)
        current_audit_logger().append_event("project.delete_failed", {
            "project_id": project_id,
            "error": str(exc)[:240],
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
        current_audit_logger().append_event(
            "experiment.repeats_summarized",
            {"project_id": project_id, "run_count": len(request.run_ids), "status": result.get("status"), "side_effect": "none"},
        )
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
    payload, payload_error = _document_payload_by_name(workspace, source_name)
    if payload_error:
        return JSONResponse({"error": payload_error}, status_code=409)
    if payload is None:
        return JSONResponse({"error": "document not found"}, status_code=404)
    catalog = ProjectCatalog(workspace)
    temp_path: Path | None = None
    try:
        evidence = catalog.find_asset_version_for_file(source_name)
        if evidence is None or str(evidence.get("project_id")) != project_id:
            return JSONResponse({"error": "metrics source is not uniquely cataloged in this project"}, status_code=409)
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
        current_audit_logger().append_event(
            "experiment.metrics_imported" if result["status"] == "imported" else "experiment.metrics_import_planned",
            {
                "project_id": project_id,
                "source_name": source_name,
                "experiment_name": request.experiment_name,
                "status": result["status"],
                "confirmed": request.confirmed,
                "side_effect": "catalog_write" if result["status"] == "imported" else "none",
            },
        )
        return {**result, "source_version_id": evidence["version_id"]}
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
            current_audit_logger().append_event(
                "experiment.compared",
                {
                    "project_id": project_id,
                    "baseline_run_id": request.baseline_run_id,
                    "current_run_id": request.current_run_id,
                    "status": result.get("status"),
                    "side_effect": "none",
                },
            )
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
        contact_id = catalog.create_contact(**request.model_dump())
        current_audit_logger().append_event(
            "contact.created",
            {"contact_id": contact_id, "project_count": len(request.project_ids)},
        )
        return {"contact_id": contact_id, "status": "created"}
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
        current_audit_logger().append_event("contact.deleted", {"contact_id": contact_id})
        return {"status": "deleted", "contact_id": contact_id}
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


def _draft_contact_or_response(catalog: ProjectCatalog, query: str, project_id: str):
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
    project_id: str,
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
        evidence = catalog.find_asset_version_for_file(name)
        if evidence is not None and str(evidence.get("project_id") or "") == project_id:
            version_id = str(evidence.get("version_id") or "").strip()
            if version_id and version_id not in resolved_ids:
                resolved_ids.append(version_id)
            refs.append({"source_filename": name[:260], "version_id": version_id, "status": "resolved"})
        else:
            refs.append({"source_filename": name[:260], "version_id": "", "status": "unresolved"})
    return resolved_ids[:64], refs[:64]


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
        source_version_ids, source_refs = _artifact_source_refs(
            catalog,
            request.project_id,
            request.attachment_refs,
            request.source_version_ids,
        )
        plan["source_refs"] = source_refs
        artifact_id = catalog.create_artifact(
            request.project_id,
            "email_draft",
            plan,
            source_version_ids=source_version_ids,
            confirmation_record={
                "status": "draft",
                "mode": "draft_created",
                "required": True,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            change_summary="创建邮件草稿；未执行外部发送，等待用户确认。",
        )
        artifact = catalog.get_artifact(artifact_id)
        current_audit_logger().append_event(
            "artifact.email_draft_created",
            {"artifact_id": artifact_id, "project_id": request.project_id, "body_chars": len(request.body)},
        )
        return {"artifact": artifact}
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
            attachment_refs.append(str(result.get("name") or filename))
        return {"status": "ok", "attachment_refs": attachment_refs}
    finally:
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
        participant = None
        if request.participant_query:
            participant, response = _draft_contact_or_response(catalog, request.participant_query, request.project_id)
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
            request.project_id,
            source_version_ids=request.source_version_ids,
        )
        plan["source_refs"] = source_refs
        try:
            artifact_id = catalog.create_artifact(
                request.project_id,
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
            )
        except sqlite3.IntegrityError:
            if not request.idempotency_key:
                raise
            existing = catalog.conn.execute(
                "SELECT artifact_id FROM artifacts WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is None:
                raise
            artifact_id = str(existing[0])
        artifact = catalog.get_artifact(artifact_id)
        current_audit_logger().append_event(
            "artifact.schedule_draft_created",
            {"artifact_id": artifact_id, "project_id": request.project_id},
        )
        return {"artifact": artifact}
    except (ValueError, sqlite3.IntegrityError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.patch("/drafts/{artifact_id}")
async def schedule_draft_update(artifact_id: str, request: ScheduleDraftUpdateRequest):
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
        current_audit_logger().append_event(
            "artifact.schedule_draft_updated",
            {"artifact_id": artifact_id, "project_id": current["project_id"], "previous_status": previous_status},
        )
        return {"artifact": artifact}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.delete("/drafts/{artifact_id}")
async def draft_delete(artifact_id: str):
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
        current_audit_logger().append_event(
            "artifact.schedule_draft_deleted",
            {"artifact_id": artifact_id, "project_id": current["project_id"]},
        )
        return {"artifact": catalog.get_artifact(artifact_id)}
    finally:
        catalog.close()


@app.get("/drafts/{artifact_id}")
async def draft_get(artifact_id: str):
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
        return {"artifact": artifact}
    finally:
        catalog.close()


@app.post("/drafts/{artifact_id}/status")
async def draft_set_status(artifact_id: str, request: ArtifactStatusRequest):
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
        if current["confirmation_status"] != "draft":
            return JSONResponse({"error": "artifact is already finalized", "artifact": current}, status_code=409)
        artifact = catalog.set_artifact_status(artifact_id, request.status)
        current_audit_logger().append_event(
            "artifact.status_changed",
            {"artifact_id": artifact_id, "artifact_type": current["artifact_type"], "status": request.status},
        )
        return {"artifact": artifact}
    finally:
        catalog.close()


@app.post("/email/smtp-config")
async def email_smtp_config(request: EmailSmtpConfigRequest):
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        result = configure_smtp(request.provider, request.sender, request.authorization_code)
        current_audit_logger().append_event("email.smtp_configured", {"provider": result["provider"], "sender": result["sender"]})
        return result
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
    current_audit_logger().append_event(
        "cloud_model.configured",
        {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "model": config.model,
            "model_count": len(config.models),
        },
    )
    return config.public_dict()


@app.post("/drafts/{artifact_id}/send")
async def email_draft_send(artifact_id: str, request: EmailSendRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        artifact = catalog.get_artifact(artifact_id)
        if artifact is None or artifact["artifact_type"] != "email_draft":
            return JSONResponse({"error": "unknown email draft"}, status_code=404)
        if artifact["confirmation_status"] != "confirmed":
            return JSONResponse({"error": "email draft requires confirmation"}, status_code=409)
        plan = dict(artifact["plan"])
        if plan.get("sender") and plan["sender"] != request.sender:
            return JSONResponse({"error": "sender differs from the confirmed draft"}, status_code=409)
        keys = get_workspace_keys(current_workspace().root_path)
        if keys is None:
            return JSONResponse({"error": "workspace passphrase required"}, status_code=423)
        store = SecureDocumentStore(current_workspace(), keys)
        try:
            attachments = []
            for name in plan.get("attachment_refs") or []:
                payload = store.read_document_named(str(name))
                if payload is None:
                    raise EmailDeliveryError(f"confirmed attachment is unavailable: {name}")
                attachments.append((str(name), payload))
        finally:
            store.close()
        plan["sender"] = request.sender
        plan["idempotency_key"] = request.idempotency_key
        existing = catalog.reserve_email_delivery(artifact_id, request.idempotency_key)
        if existing == "sent":
            return {"status": "sent", "idempotent": True}
        if existing != "reserved":
            return JSONResponse({"error": "email delivery already attempted; create a new confirmed draft to retry"}, status_code=409)
        result = send_confirmed_email(plan, attachments)
        catalog.finish_email_delivery(artifact_id, "sent", result)
        current_audit_logger().append_event("email.sent", {"artifact_id": artifact_id, "provider": result["provider"], "attachment_count": result["attachment_count"]})
        return {"status": "sent", "idempotent": False}
    except (EmailConfigurationError, EmailDeliveryError) as exc:
        catalog.finish_email_delivery(artifact_id, "failed", {"error": str(exc)})
        current_audit_logger().append_event("email.failed", {"artifact_id": artifact_id, "reason": str(exc)})
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        catalog.close()


@app.post("/drafts/{artifact_id}/calendar-ics")
async def schedule_draft_export_ics(artifact_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    catalog = ProjectCatalog(current_workspace())
    try:
        artifact = catalog.get_artifact(artifact_id)
        if artifact is None or artifact["artifact_type"] != "schedule_draft":
            return JSONResponse({"error": "unknown schedule draft"}, status_code=404)
        if artifact["confirmation_status"] != "confirmed":
            return JSONResponse({"error": "schedule draft requires confirmation"}, status_code=409)
        output_path = export_ics(current_workspace(), artifact_id, artifact["plan"])
        current_audit_logger().append_event("schedule.ics_exported", {"artifact_id": artifact_id})
        return {"status": "exported", "output_path": output_path}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        catalog.close()


@app.post("/documents/import")
async def documents_import(request: PaperImportRequest):
    return _import_documents(request)


def _run_presentation_task(task_id: str) -> None:
    try:
        service = current_presentation_service()
        result = service.run_task(task_id)
        if result.get("status") == "completed":
            runtime_metrics.increment("presentation_tasks_completed")
        else:
            runtime_metrics.increment("presentation_tasks_failed")
    except Exception as exc:
        runtime_metrics.increment("presentation_tasks_failed")
        runtime_metrics.record_error(f"presentation task failed: {exc}")
    finally:
        with presentation_thread_lock:
            presentation_threads.pop(task_id, None)


def _start_presentation_thread(task_id: str) -> bool:
    with presentation_thread_lock:
        existing = presentation_threads.get(task_id)
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(
            target=_run_presentation_task,
            args=(task_id,),
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
        task = service.create_task(payload)
        runtime_metrics.increment("presentation_tasks_started")
        started = _start_presentation_thread(task["task_id"])
        public_task = service.get_task(task["task_id"])
        return JSONResponse(
            {**public_task, "worker_started": started},
            status_code=202,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/presentations/tasks")
async def presentation_tasks(limit: int = 30):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    service = current_presentation_service()
    return {"tasks": service.list_tasks(limit=max(1, min(limit, 100)))}


@app.get("/presentations/tasks/{task_id}")
async def presentation_task(task_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        return JSONResponse(
            current_presentation_service().get_task(task_id),
            headers={"Cache-Control": "no-store"},
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/presentations/tasks/{task_id}/resume")
async def presentation_resume(task_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        task = current_presentation_service().get_task(task_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if task["status"] == "completed":
        return task
    started = _start_presentation_thread(task_id)
    return JSONResponse({**task, "worker_started": started}, status_code=202 if started else 409)


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
async def index_run():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    state = get_runtime_state()

    workspace = current_workspace()
    auth_level = str(state.get("auth_level") or "PRO")
    try:
        process = _start_indexer_worker(workspace, auth_level)
    except Exception as exc:
        runtime_metrics.record_error(f"indexer start failed: {exc}")
        return JSONResponse({"error": "indexer worker failed to start"}, status_code=500)

    if process is None:
        return JSONResponse(
            {"started": False, "status": "already_running"},
            status_code=409,
        )

    update_indexing_status(
        status="queued",
        run_id=str(uuid.uuid4()),
        current_file=None,
        started_at=time.time(),
        finished_at=None,
    )
    return JSONResponse(
        {"started": True, "status": "queued", "workspace": "SoulDrive workspace mounted"},
        status_code=202,
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

        diagnostics = GraphRAGIndex(graph_store).diagnostics(project_id=project_id)
        state = get_runtime_state()
        indexing = state.get("indexing") or {}
        indexing_status = str(indexing.get("status") or "idle")
        graph_observation = (indexing.get("observability") or {}).get("graphrag") or {}
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
            if observed_status == "failed" and not diagnostics.get("ready"):
                diagnostics.update(
                    status="failed",
                    reason="graphrag_index_build_failed",
                    error=str(observed.get("error") or "")[:240],
                )
            elif observed_status == "skipped" and not diagnostics.get("ready"):
                diagnostics.update(
                    status="disabled",
                    reason=str(observed.get("reason") or "auth_level_not_graph_enabled"),
                )
            elif diagnostics.get("ready"):
                diagnostics["status"] = "ready"
            else:
                diagnostics["status"] = "not_built"
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


def _retrieval_feedback_for_query(query: str, project_id: str | None = None) -> list[dict[str, Any]]:
    try:
        store = current_learning_store()
        try:
            return store.list_retrieval_feedback(query, project_id=project_id)
        finally:
            store.close()
    except Exception as exc:
        runtime_metrics.record_error(f"retrieval feedback unavailable: {exc}")
        return []


def _run_retrieval_test(query: str, top_k: int, project_id: str | None) -> dict[str, Any]:
    knowledge_base, _workspace = ensure_knowledge_base_loaded()
    started_at = time.perf_counter()
    feedback = _retrieval_feedback_for_query(query, project_id=project_id)
    search_kwargs = {
        "query": query,
        "top_k": top_k,
        "project_id": project_id,
    }
    feedback_variants = [
        str(item.get("corrected_query") or "").strip()
        for item in feedback
        if str(item.get("corrected_query") or "").strip()
    ][:3]
    if feedback_variants:
        search_kwargs["extra_query_variants"] = feedback_variants
    try:
        result = knowledge_base.search_with_evidence(**search_kwargs)
    except TypeError as exc:
        if "extra_query_variants" not in str(exc) or not feedback_variants:
            raise
        search_kwargs.pop("extra_query_variants", None)
        result = knowledge_base.search_with_evidence(**search_kwargs)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    observability = dict(result.get("observability") or {})
    project_focus = dict(observability.get("project_focus") or {})
    return {
        "schema_version": "retrieval_test_v1",
        "query": query,
        "project_id": project_id,
        "top_k": top_k,
        "elapsed_ms": elapsed_ms,
        "retrieval_mode": result.get("retrieval_mode"),
        "project_route": project_focus.get("route") or "global",
        "query_variants": expand_query_variants(query, limit=5),
        "feedback": [
            {
                "feedback_id": item.get("feedback_id"),
                "scope": item.get("scope"),
                "project_id": item.get("project_id"),
                "corrected_query": item.get("corrected_query"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in feedback
        ],
        "chunks": _retrieval_test_chunks(result),
        "observability": {
            "stage_counts": observability.get("stage_counts") or {},
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
        )
    except Exception as exc:
        runtime_metrics.record_error("retrieval diagnostic failed")
        current_audit_logger().append_event(
            "retrieval.diagnostics_failed",
            {"query_chars": len(query), "project_id": project_id, "error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "检索测试暂不可用，请检查本地索引和模型"}, status_code=503)

    current_audit_logger().append_event(
        "retrieval.diagnostics_test",
        {
            "query_chars": len(query),
            "project_id": project_id,
            "top_k": int(request.top_k),
            "chunk_count": len(payload["chunks"]),
            "project_route": payload["project_route"],
        },
    )
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
            )
        finally:
            store.close()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    current_audit_logger().append_event(
        "retrieval.feedback_saved",
        {
            "feedback_id": feedback["feedback_id"],
            "query_chars": len(request.query.strip()),
            "corrected_query_chars": len(request.corrected_query.strip()),
            "scope": feedback["scope"],
            "project_id": feedback["project_id"],
        },
    )
    return {"status": "saved", "feedback": feedback}


@app.delete("/diagnostics/retrieval/feedback/{feedback_id}")
async def diagnostics_retrieval_feedback_revoke(
    feedback_id: str,
    confirm: bool = Query(default=False),
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

    store = current_learning_store()
    try:
        feedback = store.revoke_retrieval_feedback(feedback_id)
    finally:
        store.close()
    if feedback is None:
        return JSONResponse({"error": "feedback not found or already revoked"}, status_code=404)
    current_audit_logger().append_event(
        "retrieval.feedback_revoked",
        {
            "feedback_id": feedback["feedback_id"],
            "scope": feedback["scope"],
            "project_id": feedback["project_id"],
        },
    )
    return {"status": "revoked", "feedback": feedback}


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


def _indexed_document_preview(workspace: SoulDriveWorkspace, source_filename: str) -> dict:
    """Read the exact cleaned parent chunks used by the local RAG index."""
    records: list[dict] = []
    keys = get_workspace_keys(workspace.root_path)
    if keys is not None and Path(workspace.secure_vector_store_path).exists():
        store = SecureVectorStore(workspace.secure_vector_store_path, keys)
        try:
            parent_ids = [row[0] for row in store.conn.execute(
                "SELECT parent_id FROM parent_documents ORDER BY parent_id"
            ).fetchall()]
            for parent_id in parent_ids:
                parent = store.get_parent(parent_id)
                if parent and str(parent["metadata"].get("source_filename") or "") == source_filename:
                    records.append({"parent_id": parent_id, **parent})
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
            records = [
                {"parent_id": row[0], "content": row[1], "metadata": json.loads(row[2])}
                for row in rows
            ]

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
    return {
        "text": full_text[:30000],
        "chunks": chunks,
        "truncated": len(chunks) < len(records) or any(
            len(chunk["text"]) < len(record["content"])
            for chunk, record in zip(chunks, records)
        ),
    }


def _document_library_payload(workspace: SoulDriveWorkspace) -> dict:
    indexed_sources = _indexed_source_filenames(workspace)
    documents = []
    seen_names = set()
    catalog = ProjectCatalog(workspace)

    def scope_metadata(name: str) -> dict[str, Any]:
        bindings = catalog.find_imported_document_bindings(name)
        project_ids = sorted({str(item["project_id"]) for item in bindings if item.get("project_id")})
        if not project_ids:
            return {"scope": "global", "project_ids": []}
        return {
            "scope": "project" if len(project_ids) == 1 else "ambiguous",
            "project_id": project_ids[0] if len(project_ids) == 1 else None,
            "project_ids": project_ids,
        }

    try:
        workspace_keys = get_workspace_keys(workspace.root_path)
        if workspace_keys is not None:
            store = SecureDocumentStore(workspace, workspace_keys)
            try:
                for item in store.list_documents():
                    documents.append({
                        **item,
                        **scope_metadata(item["name"]),
                        "indexed": item["name"] in indexed_sources,
                    })
                    seen_names.add(item["name"])
            finally:
                store.close()

        documents_dir = Path(workspace.papers_path)

        for path in sorted(documents_dir.rglob("*"), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
                continue
            if path.name in seen_names:
                continue
            stat = path.stat()
            documents.append({
                "name": path.name,
                "relative_path": path.relative_to(documents_dir).as_posix(),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "media_type": document_media_type(path.name),
                **scope_metadata(path.name),
                "indexed": path.name in indexed_sources,
            })

        return {
            "ready": True,
            "document_count": len(documents),
            "indexed_count": sum(1 for document in documents if document["indexed"]),
            "workspace": "SoulDrive workspace mounted",
            "documents": documents,
        }
    finally:
        catalog.close()


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
            ORDER BY p.created_at DESC, p.name COLLATE NOCASE
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


def _verified_source_version_ids(
    catalog: ProjectCatalog,
    project_id: str,
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
    rows = catalog.conn.execute(
        f"""
        SELECT v.version_id
        FROM asset_versions v
        JOIN assets a ON a.asset_id = v.asset_id
        WHERE a.project_id = ? AND v.version_id IN ({placeholders})
        """,
        [project_id, *cleaned],
    ).fetchall()
    verified = {str(row[0]) for row in rows}
    if verified != set(cleaned):
        raise ValueError("source_version_ids must belong to the selected project")
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
            SELECT r.run_id, e.name AS experiment_name, r.performed_at, r.dataset_version,
                   a.title AS evidence_title
            FROM experiment_runs r
            JOIN experiments e ON e.experiment_id = r.experiment_id
            LEFT JOIN asset_versions v ON v.version_id = r.evidence_version_id
            LEFT JOIN assets a ON a.asset_id = v.asset_id
            WHERE e.project_id = ?
            ORDER BY r.performed_at DESC
            LIMIT 6
            """,
            (project_id,),
        ).fetchall()
        tasks = catalog.conn.execute(
            """
            SELECT title, due_at, status FROM project_tasks
            WHERE project_id = ? ORDER BY COALESCE(due_at, '9999-12-31T00:00:00+00:00'), title LIMIT 8
            """,
            (project_id,),
        ).fetchall()
        decisions = catalog.conn.execute(
            """
            SELECT statement, decided_at FROM decisions
            WHERE project_id = ? ORDER BY decided_at DESC LIMIT 4
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


def _presentation_style_skill(skill_id: str) -> dict | None:
    workspace = current_workspace()
    store = current_learning_store()
    try:
        # A Skill can be installed or edited locally between the UI list
        # refresh and PPT generation.  Sync at the final consumer boundary so
        # the selected identifier resolves against the current project files.
        sync_project_skills(store, workspace)
        get_skill = getattr(store, "get_skill", None)
        skill = get_skill(skill_id) if callable(get_skill) else next(
            (item for item in store.list_skills() if item["skill_id"] == skill_id),
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


def _presentation_outline_with_local_model(records: list[dict], inputs: dict) -> dict:
    from core.presentation_service import (
        build_extractable_presentation_plan,
        presentation_skill_application,
    )

    # The deterministic evidence-bound plan remains the fallback and the validation baseline.
    baseline = build_extractable_presentation_plan(records, inputs)
    try:
        ensure_runtime_loaded()
    except Exception:
        return {**baseline, "outline_source": "extractive"}
    if rag is None:
        return {**baseline, "outline_source": "extractive"}

    selected_skill = inputs.get("style_skill") or {}
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


def _learning_context_for_query(
    query: str,
    selected_skill_id: str | None = None,
    *,
    project_id: str | None = None,
) -> dict:
    try:
        workspace = current_workspace()
        store = current_learning_store()
        try:
            sync_project_skills(store, workspace)
            memories = store.list_memories(
                project_id=project_id,
                context_only=True,
                include_metadata=True,
            )
            result = store.search(
                query,
                top_k_memory=3,
                top_k_skill=5,
                project_id=project_id,
                context_only=True,
            )
            conversation_skills = [
                item for item in store.list_skills()
                if item.get("target") == "conversation" and item.get("compatibility_status") == "verified"
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
                    "score": (scored_memories.get(str(memory.get("memory_id") or "")) or {}).get("score", 0.0),
                }
                for memory in memories
            ]
            memory_context = gate_memory_context(query, memory_candidates)
            memory_usage_count = store.touch_memories(
                [str(item.get("memory_id") or "") for item in memory_context["memories"]]
            )
            runtime_metrics.record_memory_gate(
                memory_context["gate"]["decision"],
                memory_context["gate"]["accepted_count"],
                memory_context["gate"]["rejected_count"],
            )
            skill_matches = match_skills(query, [selected] if selected else conversation_skills, top_k=2)
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
                )
                run = store.record_skill_run(
                    skill_execution["skill_id"],
                    query=query,
                    plan=skill_execution,
                    matched_score=1.0,
                )
                skill_execution["run_id"] = run["run_id"]
            elif skill_matches:
                best_match = skill_matches[0]
                skill_execution = create_skill_execution_plan(
                    best_match["skill"],
                    query,
                    matched_score=best_match["score"],
                )
                run = store.record_skill_run(
                    skill_execution["skill_id"],
                    query=query,
                    plan=skill_execution,
                    matched_score=best_match["score"],
                )
                skill_execution["run_id"] = run["run_id"]
            result["skills"] = [match["skill"] for match in skill_matches]
            result = {
                **result,
                "memories": memory_context["memories"],
                "skills": result["skills"][:2],
                "memory_capsule": memory_context["memory_capsule"],
                "memory_gate": memory_context["gate"],
                "skill_matches": skill_matches,
                "skill_execution": skill_execution,
            }
        finally:
            store.close()
        if result["memories"] or result["skills"] or result["memory_capsule"]["items"]:
            current_audit_logger().append_event(
                "learning.injected_into_rag",
                {
                    "query_chars": len(query),
                    "memory_count": len(result["memories"]),
                    "skill_count": len(result["skills"]),
                    "memory_capsule_count": len(result["memory_capsule"]["items"]),
                    "memory_gate": result["memory_gate"],
                    "memory_usage_count": memory_usage_count,
                    "skill_execution": bool(result["skill_execution"]),
                },
            )
        return result
    except Exception as exc:
        runtime_metrics.record_error(f"learning context unavailable: {exc}")
        return {
            "memories": [],
            "skills": [],
            "memory_capsule": {"items": [], "text": ""},
            "memory_gate": {"decision": "skip", "accepted_count": 0, "rejected_count": 0},
            "skill_matches": [],
            "skill_execution": None,
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
        vector = _embed_durable_memory(asset) if embed_vectors else None
        try:
            archive_path = conversation_store.persist_memory(
                asset,
                project_id=project_id,
                session_id=session_id,
                vector=vector,
            )
            sync_items.append(
                {
                    "memory_id": memory_id,
                    "asset_type": asset.get("asset_type"),
                    "file_archive": "ok",
                    "vector_requested": bool(vector),
                    "archive_path": archive_path,
                }
            )
        except Exception as exc:
            sync_items.append(
                {
                    "memory_id": memory_id,
                    "asset_type": asset.get("asset_type"),
                    "file_archive": "error",
                    "error": str(exc),
                }
            )
    return {
        "status": "ok" if all(item["file_archive"] == "ok" for item in sync_items) else "degraded",
        "items": sync_items,
        "storage": conversation_store.health(),
    }


@app.post("/reflection/draft")
async def reflection_draft(request: ReflectionDraftRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    messages = [message.model_dump() for message in request.messages]
    if not messages:
        return JSONResponse({"error": "messages are required for reflection draft"}, status_code=400)

    store = current_learning_store()
    try:
        raw_candidates = _generate_reflection_candidates(messages)
        existing_assets = {
            "memories": store.list_memories(),
            "skills": store.list_skills(),
        }
        candidates = validate_learning_candidates(raw_candidates, messages, existing_assets=existing_assets)
        review = store.create_review(request.trigger, candidates)
        current_audit_logger().append_event(
            "reflection.draft_created",
            {
                "trigger": request.trigger,
                "candidate_count": len(candidates),
            },
        )
        return review
    finally:
        store.close()


@app.get("/reflection/reviews/pending")
async def reflection_pending_reviews():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"reviews": store.list_pending_reviews()}
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
        result = store.decide_review(
            review_id,
            [decision.model_dump(exclude_none=True) for decision in request.decisions],
            project_id=request.project_id,
        )
        accepted_assets = result.get("accepted_assets") or []
        result["storage_sync"] = _persist_accepted_learning_assets(accepted_assets)
        current_audit_logger().append_event(
            "reflection.review_completed",
            {
                "review_id": review_id,
                "accepted_count": result["accepted_count"],
                "rejected_count": result["rejected_count"],
                "storage_sync": result.get("storage_sync"),
            },
        )
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

    messages = [{"role": "user", "content": request.message}]
    candidates = [
        candidate
        for candidate in build_reflection_candidates(messages)
        if candidate.get("asset_type") == "memory"
    ]
    if not candidates:
        return {"status": "ignored", "reason": "no_explicit_profile_signal"}

    store = current_learning_store()
    try:
        validated = validate_learning_candidates(
            candidates,
            messages,
            existing_assets={"memories": store.list_memories(), "skills": []},
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
            return {"status": "ignored", "reason": "memory_signal_not_safe_to_auto_save"}

        review = store.create_review("runtime", [candidate])
        result = store.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
        )
        accepted_assets = result.get("accepted_assets") or []
        result["storage_sync"] = _persist_accepted_learning_assets(
            accepted_assets,
            project_id=request.project_id,
            session_id=request.session_id,
            embed_vectors=False,
        )
        asset = accepted_assets[0]
        current_audit_logger().append_event(
            "learning.runtime_memory_saved",
            {
                "memory_id": asset.get("memory_id"),
                "subtype": asset.get("subtype"),
                "project_id": request.project_id,
                "session_id": request.session_id,
                "embedding_deferred": True,
            },
        )
        return {
            "status": "saved",
            "memory_id": asset.get("memory_id"),
            "memory": {
                "title": asset.get("title"),
                "subtype": asset.get("subtype"),
            },
            "notice": f"已自动记录：{asset.get('title') or '用户偏好'}（可撤销）",
            "storage_sync": result["storage_sync"],
        }
    finally:
        store.close()


@app.post("/learning/memories/{memory_id}/revoke")
async def learning_memory_revoke(memory_id: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    store = current_learning_store()
    try:
        memory = store.archive_memory(memory_id)
    finally:
        store.close()
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)

    conversation_store = _conversation_store(current_workspace())
    storage_sync = {"status": "not_requested", "items": []}
    if conversation_store is not None:
        conversation_store.revoke_memory(memory)
        storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
    current_audit_logger().append_event("learning.runtime_memory_revoked", {"memory_id": memory_id})
    return {"status": "revoked", "memory_id": memory_id, "storage_sync": storage_sync}


@app.patch("/learning/memories/{memory_id}")
async def learning_memory_update(memory_id: str, request: LearningMemoryUpdateRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    store = current_learning_store()
    try:
        try:
            memory = store.update_memory(
                memory_id,
                title=request.title,
                content=request.content,
                expected_updated_at=request.expected_updated_at,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    finally:
        store.close()
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)

    memory["asset_type"] = "memory"
    storage_sync = _persist_accepted_learning_assets(
        [memory],
        project_id=memory.get("project_id"),
        embed_vectors=True,
    )
    current_audit_logger().append_event(
        "learning.memory_updated",
        {
            "memory_id": memory_id,
            "scope": memory.get("scope"),
            "project_id": memory.get("project_id"),
            "record_version": memory.get("record_version"),
        },
    )
    return {"status": "updated", "memory": memory, "storage_sync": storage_sync}


@app.delete("/learning/memories/{memory_id}")
async def learning_memory_delete(memory_id: str, confirm: bool = Query(default=False)):
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
        memory = store.delete_memory(memory_id)
    finally:
        store.close()
    if memory is None:
        return JSONResponse({"error": "memory not found or already revoked"}, status_code=404)

    conversation_store = _conversation_store(current_workspace())
    storage_sync = {"status": "not_requested", "items": []}
    if conversation_store is not None:
        conversation_store.revoke_memory(memory)
        storage_sync = {"status": "ok", "items": [{"memory_id": memory_id}]}
    current_audit_logger().append_event(
        "learning.memory_deleted",
        {"memory_id": memory_id, "scope": memory.get("scope"), "project_id": memory.get("project_id")},
    )
    return {"status": "deleted", "memory_id": memory_id, "storage_sync": storage_sync}


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
        memories = store.list_memories(project_id=project_id, include_metadata=True)
        return {
            "memories": memories,
            "project_id": project_id,
            "scope_policy": "project_plus_global" if project_id else "all_active",
        }
    finally:
        store.close()


@app.get("/learning/skills")
async def learning_skills():
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
        summary = sync_project_skills(store, workspace)
        return {"skills": store.list_skills(), "summary": summary, "path": workspace.skills_path}
    except Exception as exc:
        logger.exception("Failed to load project Skills")
        return JSONResponse(
            {
                "error": "本地 Skill 列表读取失败，请点击“刷新列表”重试。",
                "stage": "list",
                "detail": str(exc)[:240],
                "skills": [],
            },
            status_code=503,
        )
    finally:
        if store is not None:
            store.close()


@app.post("/learning/skills/sync")
async def learning_skill_sync():
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
        summary = sync_project_skills(store, workspace)
        current_audit_logger().append_event("learning.project_skills_synced", summary)
        return {"skills": store.list_skills(), "summary": summary, "path": workspace.skills_path}
    except Exception as exc:
        logger.exception("Failed to sync project Skills")
        return JSONResponse(
            {
                "error": "本地 Skill 同步失败，请检查 Skill 目录后重试。",
                "stage": "sync",
                "detail": str(exc)[:240],
                "skills": [],
            },
            status_code=503,
        )
    finally:
        if store is not None:
            store.close()


@app.get("/learning/skills/inbox")
async def learning_skill_inbox():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return {"skills": list_skill_inbox(current_workspace().skills_path)}


@app.post("/learning/skills/install")
async def learning_skill_install(request: SkillInstallRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        result = install_skill_to_inbox(
            current_workspace().skills_path,
            request.source,
            request.skill_name,
            request.target,
        )
        current_audit_logger().append_event(
            "learning.project_skill_downloaded",
            {
                "skill_name": result["skill_name"],
                "source": result["source"],
                "file_count": len(result["files"]),
                "status": result["status"],
                "target": result.get("target") or request.target,
            },
        )
        return result
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/learning/skills/inbox/{skill_name}/approve")
async def learning_skill_approve(skill_name: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        result = approve_skill_from_inbox(current_workspace().skills_path, skill_name)
        current_audit_logger().append_event(
            "learning.project_skill_adapter_review_ready",
            {"skill_name": result["skill_name"], "target": result["target"]},
        )
        return {**result, "skills": []}
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/learning/skills/inbox/{skill_name}/adapter")
async def learning_skill_inbox_adapter(skill_name: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    try:
        result = approve_skill_from_inbox(current_workspace().skills_path, skill_name)
        return result
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/learning/skills/inbox/{skill_name}/activate")
async def learning_skill_activate(skill_name: str):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = None
    try:
        result = activate_skill_from_inbox(current_workspace().skills_path, skill_name)
        store = current_learning_store()
        summary = sync_project_skills(store, current_workspace())
        skills = store.list_skills()
        current_audit_logger().append_event(
            "learning.project_skill_activated",
            {"skill_name": result["skill_name"], "target": result["target"]},
        )
        return {**result, "summary": summary, "skills": skills}
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        if store is not None:
            store.close()


@app.post("/learning/skills/{skill_id}/presentation-style")
async def learning_skill_presentation_style(skill_id: str):
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
            skill, resolved_skill_id = _resolve_skill_for_delete(store, skill_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "stage": "resolve"}, status_code=409)
        if skill is None:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        result = ensure_project_skill_presentation_adapter(
            workspace.skills_path,
            str(skill.get("source_path") or ""),
        )
        summary = sync_project_skills(store, workspace)
        skills = store.list_skills()
        try:
            current_audit_logger().append_event(
                "learning.project_skill_presentation_style_prepared",
                {
                    "skill_id": resolved_skill_id,
                    "style_path": result["style_path"],
                    "created": result["created"],
                },
            )
        except Exception:
            logger.exception("Skill style adapter audit failed for %s", resolved_skill_id)
        return {**result, "skill_id": resolved_skill_id, "summary": summary, "skills": skills}
    except SkillInstallError as exc:
        return JSONResponse({"error": str(exc), "stage": "style"}, status_code=400)
    except Exception as exc:
        logger.exception("Failed to prepare Skill presentation style")
        return JSONResponse(
            {"error": "PPT 样式适配器准备失败，请刷新 Skill 列表后重试。", "stage": "style", "detail": str(exc)[:240]},
            status_code=500,
        )
    finally:
        if store is not None:
            store.close()


@app.delete("/learning/skills/{skill_id}")
async def learning_skill_delete(skill_id: str):
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
                    "detail": str(exc)[:240],
                },
                status_code=503,
            )
        try:
            skill, resolved_skill_id = _resolve_skill_for_delete(store, skill_id)
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
                    "detail": str(exc)[:240],
                },
                status_code=500,
            )
        if skill is None:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        source_path = str(skill.get("source_path") or "").replace("\\", "/").strip()
        if not source_path:
            return JSONResponse(
                {
                    "error": "Skill 没有可验证的本地源路径，未删除数据库记录；请先修复源路径后重试。",
                    "stage": "source_cleanup",
                },
                status_code=409,
            )
        try:
            result = delete_project_skill_source_path(current_workspace().skills_path, source_path)
        except Exception as exc:
            logger.exception("Failed to clean Skill source %s", resolved_skill_id)
            return JSONResponse(
                {
                    "error": "Skill 本地目录清理失败，数据库记录已保留；请处理文件占用或路径后重试。",
                    "stage": "source_cleanup",
                    "detail": str(exc)[:500],
                },
                status_code=409,
            )
        try:
            deleted = store.delete_skill(resolved_skill_id)
        except Exception as exc:
            logger.exception("Failed to delete Skill record %s", resolved_skill_id)
            return JSONResponse(
                {
                    "error": "本地 Skill 目录已清理，但数据库记录删除失败；请刷新后重试。",
                    "stage": "record",
                    "detail": str(exc)[:500],
                    "path": result.get("path", ""),
                },
                status_code=500,
            )
        if not deleted:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        cleanup_warning = ""
        try:
            current_audit_logger().append_event("learning.project_skill_deleted", {"skill_id": resolved_skill_id, "source_path": source_path})
        except Exception as exc:
            # Deletion has already completed; an audit sink failure must not
            # turn a successful destructive operation into a misleading 500.
            logger.exception("Skill deletion audit failed for %s", resolved_skill_id)
            cleanup_warning = f"审计记录写入失败：{exc}" if not cleanup_warning else cleanup_warning
        try:
            remaining_skills = store.list_skills()
        except Exception as exc:
            # The target record is already gone.  Keep the destructive result
            # truthful and let the next explicit refresh report any unrelated
            # legacy-record problem instead of returning a false failure.
            logger.exception("Skill list refresh failed after deleting %s", resolved_skill_id)
            remaining_skills = []
            cleanup_warning = f"删除已完成，但列表刷新失败，请点击刷新列表：{exc}" if not cleanup_warning else cleanup_warning
        payload = {**result, "skills": remaining_skills}
        if cleanup_warning:
            payload["warning"] = cleanup_warning
        return payload
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                logger.exception("Skill store close failed after deleting %s", skill_id)


def _resolve_skill_for_delete(store: LearningStore, identifier: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve current and legacy UI identifiers without weakening path safety."""
    requested = str(identifier or "").strip()
    if not requested:
        return None, ""
    direct = store.get_skill(requested)
    if direct is not None:
        return direct, str(direct.get("skill_id") or requested)

    requested_path = requested.replace("\\", "/").strip("/")
    matches: list[dict[str, Any]] = []
    for skill in store.list_skills():
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
        sync_project_skills(store, workspace)
        # Skill matching is already a small, structured candidate set.  The
        # learning-store lexical prefilter drops cross-language matches before
        # the bilingual matcher can score them.
        matches = match_skills(
            request.query,
            [
                skill for skill in store.list_skills()
                if skill.get("target") == "conversation" and skill.get("compatibility_status") == "verified"
            ],
            top_k=request.top_k,
        )
        current_audit_logger().append_event(
            "learning.skill_match_performed",
            {
                "query_chars": len(request.query),
                "match_count": len(matches),
            },
        )
        return {"matches": [_public_skill_match(match) for match in matches]}
    finally:
        store.close()


@app.get("/learning/skills/runs")
async def learning_skill_runs():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"runs": store.list_skill_runs()}
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
async def learning_skill_run(skill_id: str, request: SkillRunRequest):
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
        skill = store.get_skill(skill_id)
        if skill is None:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        available_scripts = _available_skill_scripts(skill)
        plan = create_skill_execution_plan(
            skill,
            request.query,
            matched_score=request.matched_score,
            inputs=request.inputs,
        )
        plan["execution"]["available_scripts"] = available_scripts
        if request.mode == "execute" and not request.script_path:
            return JSONResponse(
                {
                    "error": "execute mode requires an explicit script_path",
                    "available_scripts": available_scripts,
                },
                status_code=422,
            )
        run = store.record_skill_run(
            skill_id,
            query=request.query,
            plan=plan,
            matched_score=request.matched_score,
            inputs=request.inputs,
        )
        if request.mode == "plan_only":
            current_audit_logger().append_event(
                "learning.skill_run_planned",
                {
                    "skill_id": skill_id,
                    "query_chars": len(request.query),
                    "script_count": len(available_scripts),
                },
            )
            return {"run_id": run["run_id"], "plan": plan, "status": run["status"]}

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
        current_audit_logger().append_event(
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
        return {
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
        result = store.search(request.query, request.top_k_memory, request.top_k_skill)
        current_audit_logger().append_event(
            "learning.search_performed",
            {
                "query_chars": len(request.query),
                "memory_count": len(result["memories"]),
                "skill_count": len(result["skills"]),
            },
        )
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
    service = current_presentation_service()
    task = service.create_task(arguments)
    runtime_metrics.increment("presentation_tasks_started")
    started = _start_presentation_thread(task["task_id"])
    return {**service.get_task(task["task_id"]), "worker_started": started}


tool_registry = build_default_tool_registry(
    runtime_status_handler=lambda _arguments: public_runtime_state(),
    audit_recent_handler=lambda arguments: {
        "events": current_audit_logger().read_recent(limit=max(1, min(int(arguments.get("limit", 30)), 100)))
    },
    workspace_diagnostics_handler=lambda _arguments: workspace_diagnostics(),
    product_diagnostics_handler=lambda _arguments: product_diagnostics(),
    audit_verify_handler=lambda arguments: audit_verify_report(arguments.get("limit")),
    presentation_generate_handler=_presentation_tool_generate,
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
    current_audit_logger().append_event("sidecar.shutdown_requested", {})
    handler = getattr(app.state, "shutdown_handler", None)
    if callable(handler):
        await _call_shutdown_handler(handler)
        return {"status": "shutdown_requested"}
    return JSONResponse({"status": "shutdown_unavailable"}, status_code=503)

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
