import os
from pathlib import Path
from typing import Any

from core.paths import model_search_dirs
from core.torch_device import resolve_torch_device


PREFERRED_RERANKER_MODELS = (
    "mmarco-mMiniLMv2-L6-H384-v1",
    "bge-reranker-v2-m3",
    "mmarco-mMiniLMv2-L12-H384-v1",
    "bge-reranker-base",
)


def preferred_reranker_model_name(workspace_path: str | None = None) -> str | None:
    configured = os.environ.get("SOULDRIVE_RERANKER_MODEL")
    if configured:
        return configured
    for directory in model_search_dirs(workspace_path):
        for model_name in PREFERRED_RERANKER_MODELS:
            if (directory / model_name).exists():
                return model_name
    return None


def resolve_reranker_model_path(workspace_path: str | None = None) -> str | None:
    model_name = preferred_reranker_model_name(workspace_path)
    if not model_name:
        return None
    for directory in model_search_dirs(workspace_path):
        candidate = directory / model_name
        if candidate.exists():
            return str(candidate)
    return str(model_search_dirs(workspace_path)[0] / model_name)


def reranker_runtime_diagnostics(workspace_path: str | None = None) -> dict[str, Any]:
    model_name = preferred_reranker_model_name(workspace_path)
    model_path = resolve_reranker_model_path(workspace_path)
    ready = bool(model_name and model_path and Path(model_path).exists())
    return {
        "ready": ready,
        "mode": "enabled" if ready else "disabled",
        "model_name": model_name,
        "path": Path(model_path).name if ready and model_path else None,
    }


class LocalReranker:
    def __init__(self, workspace_path: str | None = None):
        self.workspace_path = workspace_path
        self.model_name = preferred_reranker_model_name(workspace_path)
        self.model_path = resolve_reranker_model_path(workspace_path)
        self.encoder = None

        if self.model_name and self.model_path and Path(self.model_path).exists():
            from sentence_transformers import CrossEncoder

            self.encoder = CrossEncoder(
                self.model_path,
                max_length=512,
                device=resolve_torch_device(),
                local_files_only=True,
            )

    @property
    def ready(self) -> bool:
        return self.encoder is not None

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if not self.ready:
            return [0.0 for _ in passages]
        pairs = [(query, passage) for passage in passages]
        scores = self.encoder.predict(pairs)
        return [float(score) for score in scores]
