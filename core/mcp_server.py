import time
import uuid
import sqlite3
import inspect
import os
import json
import re
import subprocess
import sys
import threading
import shutil
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from core.api_security import TOKEN_HEADER, is_authorized_token
from core.audit_log import AuditLogger, default_audit_logger
from core.diagnostics import product_diagnostics
from core.enterprise_security import (
    SlidingWindowRateLimiter,
    allowed_origins,
    clamp_chat_top_k,
    is_loopback_client,
    max_body_bytes,
    rate_limit_per_minute,
)
from core.learning_store import LearningStore
from core.learning_validator import validate_learning_candidates
from core.memory_runtime import gate_memory_context
from core.observability import runtime_metrics
from core.paper_importer import import_paper_into_workspace
from core.presentation_service import PresentationService
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
from core.secure_vector_store import SecureVectorStore
from core.reflection import build_reflection_candidates, build_reflection_prompt, parse_reflection_response
from core.skill_runtime import create_skill_execution_plan, match_skills
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
PUBLIC_PATHS = {"/ping", "/health", "/ready", "/runtime/status"}
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
indexer_process: subprocess.Popen | None = None
indexer_lock = threading.Lock()
presentation_threads: dict[str, threading.Thread] = {}
presentation_thread_lock = threading.Lock()


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


def ensure_runtime_loaded():
    global kb, rag, loaded_workspace_path
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
    if rag is None:
        from core.rag_engine import RAGEngine

        rag = RAGEngine(
            kb=kb,
            graph_db_path=workspace.graph_db_path,
            workspace_path=workspace.root_path,
            audit_logger=current_audit_logger(),
        )
    loaded_workspace_path = workspace.root_path


def cleanup_runtime():
    global kb, rag, loaded_workspace_path
    if rag is not None:
        rag.close()
        rag = None
    if kb is not None:
        close_fn = getattr(kb, "close", None)
        if callable(close_fn):
            close_fn()
    kb = None
    loaded_workspace_path = None


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
    state = effective_runtime_state()
    public_state = dict(state)
    if public_state.get("active_drive"):
        public_state["active_drive"] = "mounted removable storage"
    if public_state.get("workspace_path"):
        public_state["workspace_path"] = "SoulDrive workspace mounted"
    if public_state.get("hardware_sn"):
        public_state["hardware_sn"] = "HASHED"
    return public_state


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
class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k: int = Field(default=DEFAULT_RAG_TOP_K, ge=1, le=20)


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


class RuntimeRequest(BaseModel):
    reason: str = Field(default="manual runtime update", max_length=200)
    auth_level: str = Field(default="DEV", max_length=16)
    hardware_sn: str | None = Field(default=None, max_length=256)
    active_drive: str | None = Field(default=None, max_length=260)

class PaperImportRequest(BaseModel):
    source_paths: list[str] = Field(min_length=1, max_length=100)


class PresentationGenerateRequest(BaseModel):
    source_filenames: list[str] = Field(min_length=1, max_length=12)
    title: str = Field(default="", max_length=120)
    audience: str = Field(default="management", pattern="^(management|technical|government|general)$")
    max_slides: int = Field(default=10, ge=4, le=16)
    open_in_wps: bool = True

class SecurityInitRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=256)
    acknowledge_no_recovery: bool = False

class SecurityUnlockRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=256)

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
    trace_id = str(uuid.uuid4())
    runtime_metrics.increment("chat_requests")
    audit_logger = current_audit_logger()
    audit_logger.append_event("chat.requested", {
        "query_chars": len(request.query),
        "top_k": clamp_chat_top_k(request.top_k),
    }, trace_id=trace_id)

    # 工业标准的流式返回闭包
    def event_generator():
        locked_response = require_software_unlock()
        if locked_response is not None:
            cleanup_runtime()
            state = get_runtime_state()
            current_audit_logger().append_event("chat.rejected_locked", {
                "reason": state.get("reason", "storage device removed"),
            }, trace_id=trace_id)
            yield f"本地知识引擎已锁定：{state.get('reason', 'storage device removed')}。请重新插入授权存储设备后再试。"
            return

        try:
            ensure_runtime_loaded()
            learning_context = _learning_context_for_query(request.query)
            for chunk in rag.generate_response_stream(
                query=request.query,
                top_k=clamp_chat_top_k(request.top_k),
                trace_id=trace_id,
                learning_context=learning_context,
            ):
                # 采用标准文本流输出，适配主流前端打字机组件
                yield chunk
        except Exception as exc:
            runtime_metrics.increment("chat_failures")
            runtime_metrics.record_error(str(exc))
            current_audit_logger().append_event("chat.failed", {
                "query_chars": len(request.query),
                "error": str(exc),
            }, trace_id=trace_id)
            yield "本地推理服务暂时不可用，已记录诊断事件。"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream" # 声明 SSE 协议
    )

# =====================================================================
# --- 备用/拓展 MCP 接口 ---
# =====================================================================
@app.get("/ping")
async def ping():
    return {"status": "SoulDrive MCP Server is fully operational."}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "souldrive-sidecar"}

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
    return {
        "sources": [
            document
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
        task = service.create_task(request.model_dump())
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
        return current_presentation_service().get_task(task_id)
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
    return _import_documents(request)


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


def _document_library_payload(workspace: SoulDriveWorkspace) -> dict:
    indexed_sources = _indexed_source_filenames(workspace)
    documents = []
    seen_names = set()

    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        store = SecureDocumentStore(workspace, workspace_keys)
        try:
            for item in store.list_documents():
                documents.append({
                    **item,
                    "indexed": item["name"] in indexed_sources,
                })
                seen_names.add(item["name"])
        finally:
            store.close()

    documents_dir = Path(workspace.papers_path)

    for path in sorted(documents_dir.rglob("*.pdf"), key=lambda item: item.name.lower()):
        if path.name in seen_names:
            continue
        stat = path.stat()
        documents.append({
            "name": path.name,
            "relative_path": path.relative_to(documents_dir).as_posix(),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            "indexed": path.name in indexed_sources,
        })

    return {
        "ready": True,
        "document_count": len(documents),
        "indexed_count": sum(1 for document in documents if document["indexed"]),
        "workspace": "SoulDrive workspace mounted",
        "documents": documents,
    }


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
    )


def _presentation_outline_with_local_model(records: list[dict], inputs: dict) -> dict:
    from core.presentation_service import build_extractable_presentation_plan

    # The deterministic evidence-bound plan remains the fallback and the validation baseline.
    baseline = build_extractable_presentation_plan(records, inputs)
    try:
        ensure_runtime_loaded()
    except Exception:
        return baseline
    if rag is None:
        return baseline

    evidence_lines = []
    for record in records[:24]:
        content = " ".join(str(record.get("content") or "").split())[:700]
        evidence_lines.append(
            f"[{record['source_filename']} | {record.get('section') or '未标注章节'} | "
            f"第{record.get('page_number') or '?'}页] {content}"
        )
    prompt = [
        {
            "role": "system",
            "content": (
                "你是企业正式汇报材料编辑。只依据用户提供的证据摘录生成 PPT 提纲。"
                "不要引入外部事实，不要写营销空话。输出严格 JSON："
                '{"title":"...","slides":[{"kind":"cover|content|sources",'
                '"title":"...","subtitle":"...","points":["..."],'
                '"sources":["必须是给定文件名"],"footer":"..."}]}。'
                "每页最多 5 个要点，每个要点不超过 55 个汉字。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"标题：{inputs.get('title') or '自动归纳'}\n"
                f"受众：{inputs['audience']}\n"
                f"最多页数：{inputs['max_slides']}\n"
                "证据摘录：\n" + "\n\n".join(evidence_lines)
            ),
        },
    ]
    raw = rag._generate_answer_text(prompt)
    cleaned = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return baseline
    payload.setdefault("schema_version", "souldrive_presentation_plan_v1")
    payload.setdefault("generated_at_label", time.strftime("%Y-%m-%d %H:%M"))
    return payload


def _import_documents(request: PaperImportRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    workspace = current_workspace()
    items = [import_paper_into_workspace(workspace, source_path) for source_path in request.source_paths]
    return {
        "ready": True,
        "imported_count": sum(1 for item in items if item["status"] == "imported"),
        "items": items,
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


def _learning_context_for_query(query: str) -> dict:
    try:
        store = current_learning_store()
        try:
            memories = store.list_memories()
            result = store.search(query, top_k_memory=3, top_k_skill=5)
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
            runtime_metrics.record_memory_gate(
                memory_context["gate"]["decision"],
                memory_context["gate"]["accepted_count"],
                memory_context["gate"]["rejected_count"],
            )
            skill_matches = match_skills(query, result["skills"], top_k=2)
            skill_execution = None
            if skill_matches:
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
        result = store.decide_review(review_id, [decision.model_dump(exclude_none=True) for decision in request.decisions])
        current_audit_logger().append_event(
            "reflection.review_completed",
            {
                "review_id": review_id,
                "accepted_count": result["accepted_count"],
                "rejected_count": result["rejected_count"],
            },
        )
        return result
    finally:
        store.close()


@app.get("/learning/memories")
async def learning_memories():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"memories": store.list_memories()}
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
    store = current_learning_store()
    try:
        return {"skills": store.list_skills()}
    finally:
        store.close()


@app.post("/learning/skills/match")
async def learning_skill_match(request: SkillMatchRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        result = store.search(request.query, top_k_memory=0, top_k_skill=max(request.top_k, 5))
        matches = match_skills(request.query, result["skills"], top_k=request.top_k)
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


@app.post("/learning/skills/{skill_id}/runs")
async def learning_skill_run(skill_id: str, request: SkillRunRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        skills = [skill for skill in store.list_skills() if skill["skill_id"] == skill_id]
        if not skills:
            return JSONResponse({"error": "unknown active skill"}, status_code=404)
        plan = create_skill_execution_plan(
            skills[0],
            request.query,
            matched_score=request.matched_score,
            inputs=request.inputs,
        )
        run = store.record_skill_run(
            skill_id,
            query=request.query,
            plan=plan,
            matched_score=request.matched_score,
            inputs=request.inputs,
        )
        current_audit_logger().append_event(
            "learning.skill_run_planned",
            {
                "skill_id": skill_id,
                "query_chars": len(request.query),
            },
        )
        return {"run_id": run["run_id"], "plan": plan, "status": run["status"]}
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
