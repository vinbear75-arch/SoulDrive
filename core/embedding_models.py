import os
from pathlib import Path

from core.paths import model_search_dirs, resolve_model_path


DEFAULT_EMBEDDING_MODEL = "bge-small-zh-v1.5"
PREFERRED_EMBEDDING_MODELS = (
    "Qwen3-Embedding-0.6B",
    "qwen3-embedding-0.6b",
    DEFAULT_EMBEDDING_MODEL,
)
BGE_ZH_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
QWEN3_QUERY_INSTRUCTION = (
    "Instruct: Given a Chinese enterprise, technical, project, or government "
    "document query, retrieve passages that answer the query\nQuery: "
)


def preferred_embedding_model_path(workspace_path: str | None = None) -> str:
    configured = str(os.environ.get("SOULDRIVE_EMBEDDING_MODEL") or "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_dir():
            return str(configured_path.resolve())
        return resolve_model_path(configured, workspace_path)

    for model_name in PREFERRED_EMBEDDING_MODELS:
        for directory in model_search_dirs(workspace_path):
            candidate = directory / model_name
            if candidate.is_dir():
                return str(candidate)
    return resolve_model_path(DEFAULT_EMBEDDING_MODEL, workspace_path)


def embedding_query_instruction(model_name: str) -> str:
    normalized = str(model_name or "").lower()
    if "qwen3" in normalized and "embedding" in normalized:
        return QWEN3_QUERY_INSTRUCTION
    if "bge-" in normalized and "-zh" in normalized:
        return BGE_ZH_QUERY_INSTRUCTION
    return ""


def required_embedding_files(model_name: str) -> tuple[str, ...]:
    normalized = str(model_name or "").lower()
    if "qwen3" in normalized and "embedding" in normalized:
        return ("config.json", "model.safetensors", "tokenizer.json")
    return ("config.json", "model.safetensors", "tokenizer.json", "vocab.txt")
