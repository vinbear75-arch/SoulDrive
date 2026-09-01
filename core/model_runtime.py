import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.cloud_model import load_cloud_model_config
from core.embedding_models import (
    DEFAULT_EMBEDDING_MODEL,
    preferred_embedding_model_path,
)
from core.paths import model_search_dirs, resolve_model_path
from core.reranker import reranker_runtime_diagnostics


DEFAULT_CHAT_MODEL = "qwen2.5-3b-instruct-q4_k_m.gguf"
EXPERIMENT_7B_CHAT_MODEL = "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
EXPERIMENT_7B_CHAT_MODEL_PARTS = (
    "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
    "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf",
)
CHAT_MODEL_REQUIRED_FILES = {
    EXPERIMENT_7B_CHAT_MODEL: EXPERIMENT_7B_CHAT_MODEL_PARTS,
}
PREFERRED_CHAT_MODELS = (
    EXPERIMENT_7B_CHAT_MODEL,
    "qwen2.5-7b-instruct-q4_k_m.gguf",
    DEFAULT_CHAT_MODEL,
)
@dataclass(frozen=True)
class LlamaRuntimeConfig:
    model_filename: str = DEFAULT_CHAT_MODEL
    chat_n_ctx: int = 8192
    graph_n_ctx: int = 4096
    temperature: float = 0.01
    top_p: float = 0.85
    repeat_penalty: float = 1.12
    max_tokens: int = 900
    graph_temperature: float = 0.1
    graph_max_tokens: int = 2048
    graph_repeat_penalty: float = 1.1

    def public_dict(self) -> dict[str, Any]:
        return {
            "model_filename": self.model_filename,
            "chat_n_ctx": self.chat_n_ctx,
            "graph_n_ctx": self.graph_n_ctx,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            "max_tokens": self.max_tokens,
            "graph_temperature": self.graph_temperature,
            "graph_max_tokens": self.graph_max_tokens,
            "graph_repeat_penalty": self.graph_repeat_penalty,
        }


def llama_runtime_config(workspace_path: str | None = None) -> LlamaRuntimeConfig:
    model_filename = preferred_chat_model_filename(workspace_path)
    return LlamaRuntimeConfig(
        model_filename=model_filename,
        chat_n_ctx=_bounded_int("SOULDRIVE_LLAMA_CHAT_CTX", 8192, 1024, 32768),
        graph_n_ctx=_bounded_int("SOULDRIVE_LLAMA_GRAPH_CTX", 8192, 1024, 16384),
        temperature=_bounded_float("SOULDRIVE_LLAMA_TEMPERATURE", 0.01, 0.0, 1.0),
        top_p=_bounded_float("SOULDRIVE_LLAMA_TOP_P", 0.85, 0.05, 1.0),
        repeat_penalty=_bounded_float("SOULDRIVE_LLAMA_REPEAT_PENALTY", 1.12, 1.0, 2.0),
        max_tokens=_bounded_int("SOULDRIVE_LLAMA_MAX_TOKENS", 900, 128, 4096),
        graph_temperature=_bounded_float("SOULDRIVE_GRAPH_TEMPERATURE", 0.1, 0.0, 1.0),
        graph_max_tokens=_bounded_int("SOULDRIVE_GRAPH_MAX_TOKENS", 2048, 256, 4096),
        graph_repeat_penalty=_bounded_float("SOULDRIVE_GRAPH_REPEAT_PENALTY", 1.1, 1.0, 2.0),
    )


@dataclass(frozen=True)
class LlamaLoadResult:
    model: Any
    load_ms: int
    effective_config: LlamaRuntimeConfig
    gpu_layers: int


def resolve_chat_model_path(workspace_path: str | None = None, config: LlamaRuntimeConfig | None = None) -> str:
    runtime_config = config or llama_runtime_config(workspace_path)
    return resolve_model_path(runtime_config.model_filename, workspace_path)


def preferred_chat_model_filename(workspace_path: str | None = None) -> str:
    configured = os.environ.get("SOULDRIVE_CHAT_MODEL")
    if configured:
        return configured
    search_dirs = model_search_dirs(workspace_path)
    for model_filename in PREFERRED_CHAT_MODELS:
        for directory in search_dirs:
            if _chat_model_candidate_ready(directory, model_filename):
                return model_filename
    return PREFERRED_CHAT_MODELS[0]


def load_llama_model(
    model_path: str,
    config: LlamaRuntimeConfig,
    n_ctx: int,
) -> LlamaLoadResult:
    from llama_cpp import Llama

    gpu_layers = _gpu_layers()

    def load_cpu_model():
        return Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_gpu_layers=gpu_layers,
            verbose=False,
        )

    model, load_ms = timed_model_load(load_cpu_model)
    return LlamaLoadResult(model=model, load_ms=load_ms, effective_config=config, gpu_layers=gpu_layers)


def _gpu_layers() -> int:
    try:
        from llama_cpp import llama_supports_gpu_offload

        return -1 if llama_supports_gpu_offload() else 0
    except Exception:
        return 0


def model_runtime_diagnostics(workspace_path: str | None = None) -> dict[str, Any]:
    config = llama_runtime_config(workspace_path)
    search_roots = model_search_dirs(workspace_path)
    reranker_report = reranker_runtime_diagnostics(workspace_path)
    chat_provider = str(os.environ.get("SOULDRIVE_CHAT_PROVIDER") or "").strip().lower()
    embedding_provider = str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower()
    cloud_config = load_cloud_model_config()
    if cloud_config is not None and cloud_config.enabled:
        chat_report = {
            "ready": bool(cloud_config.model),
            "name": cloud_config.model or "未选择模型",
            "selected": cloud_config.model,
            "provider": "openai-compatible",
            "mode": "cloud",
        }
    elif chat_provider == "ollama":
        chat_model = str(os.environ.get("SOULDRIVE_OLLAMA_MODEL") or "qwen3.5:9b").strip()
        chat_report = {
            "ready": True,
            "name": chat_model,
            "selected": chat_model,
            "provider": "ollama",
            "mode": "ollama",
        }
    else:
        chat_path = Path(resolve_chat_model_path(workspace_path, config))
        chat_report = {
            **_chat_model_status(chat_path, config.model_filename),
            "selected": config.model_filename,
            "provider": "llama.cpp",
            "mode": "gguf",
        }
    if embedding_provider == "ollama":
        embedding_report = {
            "ready": True,
            "name": str(os.environ.get("SOULDRIVE_OLLAMA_EMBEDDING_MODEL") or "qwen3-embedding:4b").strip(),
            "provider": "ollama",
        }
    else:
        embedding_path = Path(preferred_embedding_model_path(workspace_path))
        embedding_report = _path_status(embedding_path)
        embedding_report["provider"] = "sentence-transformers"
    return {
        "ready": chat_report["ready"] and embedding_report["ready"],
        "config": config.public_dict(),
        "gpu_layers": _gpu_layers() if chat_provider != "ollama" else 0,
        "chat_model": chat_report,
        "embedding_model": embedding_report,
        "reranker": {
            "ready": reranker_report["ready"],
            "selected": reranker_report["model_name"],
            "mode": reranker_report["mode"],
        },
        "search_roots": [_redact_path(path) for path in search_roots],
    }


def timed_model_load(load_fn):
    started_at = time.time()
    result = load_fn()
    return result, int((time.time() - started_at) * 1000)


def _path_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "ready": False,
            "name": path.name,
            "size": 0,
        }
    if path.is_file():
        size = path.stat().st_size
    else:
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return {
        "ready": True,
        "name": path.name,
        "size": size,
    }


def _chat_model_candidate_ready(directory: Path, model_filename: str) -> bool:
    return all((directory / required_file).exists() for required_file in _required_chat_model_files(model_filename))


def _required_chat_model_files(model_filename: str) -> tuple[str, ...]:
    return CHAT_MODEL_REQUIRED_FILES.get(model_filename, (model_filename,))


def _chat_model_status(path: Path, model_filename: str) -> dict[str, Any]:
    status = _path_status(path)
    missing_parts = [
        required_file
        for required_file in _required_chat_model_files(model_filename)
        if not (path.parent / required_file).exists()
    ]
    if missing_parts:
        status["ready"] = False
        status["missing_parts"] = missing_parts
    return status


def _redact_path(path: Path) -> str:
    return path.name if path.name else str(path)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
