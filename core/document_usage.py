"""Small local usage ledger for retrieval freshness and evidence reuse."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any


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
    fd, temporary = tempfile.mkstemp(prefix="document-usage-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(usage, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def forget_document(workspace_path: str, source_name: str) -> None:
    path = _path(workspace_path)
    usage = _read(workspace_path)
    if source_name not in usage:
        return
    usage.pop(source_name, None)
    path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")


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
