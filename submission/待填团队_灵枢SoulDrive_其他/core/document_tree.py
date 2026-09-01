"""Deterministic, local-only section tree metadata for long documents."""

from __future__ import annotations

import hashlib
import re
from typing import Any


SECTION_TREE_VERSION = "section_tree_v1"


def normalize_section_path(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
    elif value:
        parts = [part.strip() for part in str(value).split(">") if part.strip()]
    else:
        parts = []
    return tuple(parts)


def section_node_id(document_id: str, path: tuple[str, ...] | list[str]) -> str:
    clean_path = normalize_section_path(path)
    key = f"{document_id}\x00{' > '.join(clean_path)}".encode("utf-8")
    return f"section_{hashlib.sha256(key).hexdigest()[:20]}"


def build_section_tree(document_id: str, chunks: list[Any]) -> dict[str, Any]:
    """Build stable node/parent metadata without an extra model call.

    The resulting fields are copied onto parent and child records so the tree
    can be reconstructed from either the ordinary or encrypted local index.
    """
    nodes_by_path: dict[tuple[str, ...], dict[str, Any]] = {}
    parent_paths: dict[int, tuple[str, ...]] = {}

    for parent_index, chunk in enumerate(chunks):
        metadata = getattr(chunk, "metadata", {}) or {}
        path = normalize_section_path(metadata.get("section_path") or metadata.get("section"))
        if not path:
            continue
        parent_paths[parent_index] = path
        content = str(getattr(chunk, "page_content", "") or "")
        for depth in range(1, len(path) + 1):
            node_path = path[:depth]
            node = nodes_by_path.setdefault(
                node_path,
                {
                    "path": node_path,
                    "summary": "",
                    "direct_parent_indexes": [],
                    "child_paths": set(),
                },
            )
            if not node["summary"]:
                node["summary"] = _extractive_summary(content)
            if depth == len(path):
                node["direct_parent_indexes"].append(parent_index)
            if depth > 1:
                nodes_by_path[node_path[:-1]]["child_paths"].add(node_path)

    finalized: dict[tuple[str, ...], dict[str, Any]] = {}
    for path, node in nodes_by_path.items():
        parent_path = path[:-1]
        child_paths = sorted(node["child_paths"])
        finalized[path] = {
            "node_id": section_node_id(document_id, path),
            "parent_node_id": section_node_id(document_id, parent_path) if parent_path else "",
            "title": path[-1],
            "depth": len(path),
            "path": list(path),
            "path_key": " > ".join(path),
            "summary": node["summary"],
            "summary_source": "extractive_first_sentence_v1",
            "child_node_ids": [section_node_id(document_id, child) for child in child_paths],
            "direct_parent_indexes": list(node["direct_parent_indexes"]),
        }

    by_parent_index: dict[int, dict[str, Any]] = {}
    for parent_index, path in parent_paths.items():
        node = finalized[path]
        ancestor_ids = []
        for depth in range(1, len(path)):
            ancestor = path[:depth]
            if ancestor in finalized:
                ancestor_ids.append(finalized[ancestor]["node_id"])
        by_parent_index[parent_index] = {
            "section_tree_version": SECTION_TREE_VERSION,
            "section_tree_document_id": document_id,
            "section_node_id": node["node_id"],
            "section_parent_node_id": node["parent_node_id"],
            "section_node_depth": node["depth"],
            "section_node_title": node["title"],
            "section_node_summary": node["summary"],
            "section_node_summary_source": node["summary_source"],
            "section_node_child_ids": "|".join(node["child_node_ids"]),
            "section_node_child_count": len(node["child_node_ids"]),
            "section_node_is_leaf": not node["child_node_ids"],
            "section_path_key": node["path_key"],
            "section_ancestor_node_ids": "|".join(ancestor_ids),
        }

    return {
        "version": SECTION_TREE_VERSION,
        "nodes": list(finalized.values()),
        "by_parent_index": by_parent_index,
    }


def _extractive_summary(content: str, max_chars: int = 240) -> str:
    lines = [
        line.strip()
        for line in str(content or "").splitlines()
        if line.strip() and not re.match(r"^\s*#{1,6}\s+", line)
    ]
    normalized = " ".join(lines)
    if not normalized:
        return ""
    first = re.split(r"(?<=[。！？!?；;.])\s*", normalized, maxsplit=1)[0].strip()
    return first[:max_chars].rstrip("，,；;：:")
