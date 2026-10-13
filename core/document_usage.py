"""Small local usage ledger for retrieval freshness and evidence reuse."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from core.local_file_lock import local_file_lock


def _path(workspace_path: str) -> Path:
    return Path(workspace_path).resolve() / "runtime" / "document_usage.json"


def _read(workspace_path: str) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_path(workspace_path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def record_document_hits(workspace_path: str, sources: list[Any]) -> None:
    if not sources:
        return
    path = _path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with local_file_lock(f"{path}.lock"):
        usage = _read(workspace_path)
        now = time.time()
        normalized_sources = []
        for source in sources:
            if isinstance(source, dict):
                key = str(source.get("document_id") or source.get("source_filename") or "")
            else:
                key = str(source or "")
            if key:
                normalized_sources.append(key)
        for source_name in dict.fromkeys(normalized_sources):
            item = usage.setdefault(source_name, {"citation_count": 0})
            item["citation_count"] = int(item.get("citation_count") or 0) + 1
            item["last_cited_at"] = now
        _write_usage(path, usage)


def forget_document(
    workspace_path: str,
    source_name: str,
    *,
    document_id: str | None = None,
) -> None:
    path = _path(workspace_path)
    with local_file_lock(f"{path}.lock"):
        usage = _read(workspace_path)
        keys = {str(source_name or "").strip()}
        normalized_id = str(document_id or "").strip()
        if normalized_id:
            keys.add(normalized_id)
            if normalized_id.startswith("secure_"):
                keys.add(normalized_id.removeprefix("secure_"))
            else:
                keys.add(f"secure_{normalized_id}")
        if not any(key in usage for key in keys if key):
            return
        for key in keys:
            if key:
                usage.pop(key, None)
        _write_usage(path, usage)


def _write_usage(path: Path, usage: dict[str, dict[str, Any]]) -> None:
    """Publish derived usage state as one complete JSON snapshot."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(usage, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def document_weight(workspace_path: str, metadata: dict[str, Any]) -> float:
    source_name = str(metadata.get("document_id") or metadata.get("source_filename") or "")
    usage = _read(workspace_path).get(source_name) or {}
    try:
        indexed_at = float(metadata.get("indexed_at") or time.time())
    except (TypeError, ValueError):
        indexed_at = time.time()
    age_days = max(0.0, (time.time() - indexed_at) / 86400.0)
    freshness = math.exp(-age_days / 180.0)
    citations = min(1.0, math.log1p(int(usage.get("citation_count") or 0)) / math.log1p(20.0))
    return round(0.85 + 0.10 * freshness + 0.05 * citations, 6)
