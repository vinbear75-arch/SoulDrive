import os
from pathlib import Path
from typing import Any

from core.paths import model_search_dirs


PREFERRED_RERANKER_MODELS = (
    "Qwen3-Reranker-0.6B",
    "qwen3-reranker-0.6b",
    "mmarco-mMiniLMv2-L6-H384-v1",
    "bge-reranker-v2-m3",
    "mmarco-mMiniLMv2-L12-H384-v1",
    "bge-reranker-base",
)

QWEN3_RERANKER_INSTRUCTION = (
    "Given a Chinese enterprise, technical, project, or government document query, "
    "retrieve passages that contain evidence needed to answer the query"
)


def _is_qwen3_reranker(model_name: str | None) -> bool:
    return bool(model_name and model_name.lower().startswith("qwen3-reranker"))


def preferred_reranker_model_name(workspace_path: str | None = None) -> str | None:
    configured = os.environ.get("SOULDRIVE_RERANKER_MODEL")
    if configured:
        return configured
    for model_name in PREFERRED_RERANKER_MODELS:
        for directory in model_search_dirs(workspace_path):
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
        "instruction_aware": _is_qwen3_reranker(model_name),
    }


class LocalReranker:
    def __init__(self, workspace_path: str | None = None):
        self.workspace_path = workspace_path
        self.model_name = preferred_reranker_model_name(workspace_path)
        self.model_path = resolve_reranker_model_path(workspace_path)
        self.encoder = None
        self.is_qwen3 = _is_qwen3_reranker(self.model_name)

        if self.model_name and self.model_path and Path(self.model_path).exists():
            from sentence_transformers import CrossEncoder

            options: dict[str, Any] = {
                "max_length": 2048 if self.is_qwen3 else 512,
                "device": "cpu",
                "local_files_only": True,
            }
            if self.is_qwen3:
                options.update({
                    "prompts": {"souldrive": QWEN3_RERANKER_INSTRUCTION},
                    "default_prompt_name": "souldrive",
                })
            self.encoder = CrossEncoder(self.model_path, **options)

    @property
    def ready(self) -> bool:
        return self.encoder is not None

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if not self.ready:
            return [0.0 for _ in passages]
        pairs = [(query, passage) for passage in passages]
        if self.is_qwen3:
            from torch.nn import Sigmoid

            scores = self.encoder.predict(pairs, activation_fn=Sigmoid())
        else:
            scores = self.encoder.predict(pairs)
        return [float(score) for score in scores]
