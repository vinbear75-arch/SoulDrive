"""GraphRAG 的索引期提炼、Leiden 社区和成本感知查询路由。

原文、向量索引和用户记忆不由本模块替代：GraphRAG 只保存文档派生的专项
索引。社区报告没有原文证据时，查询层仍会把它和原始证据一起交给引用门控。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable


GLOBAL_MARKERS = (
    "整体",
    "总体",
    "全局",
    "全库",
    "全貌",
    "跨文档",
    "多文档",
    "这几篇",
    "这些论文",
    "这些文档",
    "共同思路",
    "共同点",
    "研究脉络",
    "主要主题",
    "有哪些方向",
    "归纳",
    "汇总",
    "综合分析",
    "趋势",
)


def _error_observation(exc: BaseException) -> dict[str, Any]:
    """保留 freshness 失败的关联信息，但不把异常原文放入诊断结果。"""
    message = str(exc or "")
    return {
        "error_type": type(exc).__name__,
        "error_digest": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(message),
    }
LOCAL_GRAPH_MARKERS = (
    "关系",
    "关联",
    "影响链",
    "路径",
    "多跳",
    "上下游",
    "依赖",
    "为什么会导致",
    "如何联系",
    "社区",
    "图谱",
    "graphrag",
)


def _graph_snapshot_digest(snapshot: dict[str, Any] | None) -> str:
    """Return a deterministic digest for the graph facts behind a derived index."""
    encoded = json.dumps(
        snapshot or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_chunk_project_ids(chunk_project_ids: dict[str, Any] | None) -> dict[str, str]:
    return {
        str(chunk_id): str(
            (value.get("project_id") if isinstance(value, dict) else value) or ""
        ).strip()
        for chunk_id, value in (chunk_project_ids or {}).items()
        if str(chunk_id)
    }


def _graph_scope_digest(
    snapshot: dict[str, Any] | None,
    chunk_project_ids: dict[str, Any] | None,
    *,
    project_id: str | None,
    global_only: bool,
) -> str:
    """Digest graph facts plus the external chunk-scope input used by indexing."""
    return _graph_snapshot_digest({
        "snapshot": snapshot or {},
        "chunk_project_ids": _normalize_chunk_project_ids(chunk_project_ids),
        "project_id": str(project_id or "").strip() or None,
        "global_only": bool(global_only),
    })


# A broad word such as "整体" can appear together with a concrete entity
# relation request. Keep those requests on local search: GraphRAG local search
# is the entity/relation path, while global search is corpus-wide synthesis.
LOCAL_RELATION_MARKERS = (
    "影响链",
    "路径",
    "多跳",
    "上下游",
    "依赖",
    "为什么会导致",
    "如何联系",
)
SOURCE_DIVERSITY_MARKERS = (
    "对比",
    "比较",
    "异同",
    "分别",
    "共同",
    "多篇",
    "跨文档",
    "这几篇",
    "这些论文",
)
STOP_TERMS = {
    "什么", "如何", "为什么", "哪些", "请问", "基于", "说明", "介绍",
    "主要", "相关", "一下", "文档", "论文", "内容", "问题", "可以",
}
DEFAULT_GRAPH_DOCUMENT_LIMIT = 10


def _graph_document_priority(document: dict[str, Any]) -> tuple[int, str]:
    source = str(document.get("source_filename") or "")
    text = " ".join(
        str(document.get(field) or "")
        for field in ("source_filename", "title", "summary", "keywords")
    )
    source_and_title = " ".join(
        str(document.get(field) or "")
        for field in ("source_filename", "title")
    )
    score = 0
    if re.search(r"[\u3400-\u9fff]", source_and_title):
        score += 3
    if any(marker in source.casefold() for marker in ("requirement", "architecture", "dataset_audit", "souldrive-local")):
        score += 2
    if any(marker in text.casefold() for marker in ("rag", "graphrag", "检索", "知识")):
        score += 1
    return score, source


def _graph_document_identity(document: dict[str, Any]) -> str:
    """Return a stable document identity without using the display filename as a key."""
    explicit = str(document.get("document_id") or "").strip()
    if explicit:
        return explicit
    source = str(document.get("source_filename") or "").strip()
    if not source:
        return ""
    project_id = str(document.get("project_id") or "").strip()
    value = f"document:{project_id}\0{source}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _apply_scoped_entity_attributes(
    item: dict[str, Any],
    scoped_chunk_ids: list[str],
    *,
    scope_requested: bool,
) -> dict[str, Any]:
    """Keep entity attributes only when their chunk provenance covers the scope."""
    if not scope_requested:
        return dict(item)

    scoped_item = dict(item)
    source_attributes = []
    for source in item.get("source_attributes") or []:
        if not isinstance(source, dict):
            continue
        chunk_id = str(source.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        source_attributes.append({
            "chunk_id": chunk_id,
            "type": str(source.get("type") or ""),
            "description": str(source.get("description") or ""),
        })
    source_by_chunk = {source["chunk_id"]: source for source in source_attributes}
    if source_by_chunk:
        if not set(scoped_chunk_ids).issubset(source_by_chunk):
            # A partially migrated entity cannot prove that its aggregate
            # description belongs to the current project/document scope.
            scoped_item["type"] = ""
            scoped_item["description"] = ""
            scoped_item["source_attributes"] = []
            return scoped_item
        selected = [source_by_chunk[chunk_id] for chunk_id in scoped_chunk_ids]
        scoped_item["source_attributes"] = selected
        types = list(dict.fromkeys(
            source["type"] for source in selected if source["type"]
        ))
        descriptions = list(dict.fromkeys(
            source["description"] for source in selected if source["description"]
        ))
        scoped_item["type"] = " / ".join(types)
        scoped_item["description"] = "；".join(descriptions)[:4000]
        return scoped_item

    return scoped_item


def _is_graph_entity_noise(name: str) -> bool:
    normalized = " ".join(str(name or "").split())
    if not normalized or len(normalized) < 2:
        return True
    lowered = normalized.casefold()
    if lowered.endswith((".pdf", ".docx", ".txt", ".md")):
        return True
    if re.fullmatch(r"\d+(?:/\d+)?", normalized) or re.fullmatch(r"\d{5,}", normalized):
        return True
    # References frequently expose authors as all-capital initial strings; they
    # are bibliographic noise for a topic community, not document concepts.
    return bool(re.fullmatch(r"[A-Z][A-Z .-]{5,}", normalized))


def _scope_graph_snapshot(
    snapshot: dict[str, Any],
    max_documents: int,
    *,
    project_id: str | None = None,
    global_only: bool = False,
    chunk_project_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    project_id = str(project_id or "").strip()
    chunk_project_ids = _normalize_chunk_project_ids(chunk_project_ids)

    source_documents = list(snapshot.get("documents") or [])
    if project_id:
        scoped_documents = []
        for document in source_documents:
            document_project_id = str(document.get("project_id") or "").strip()
            original_chunk_ids = [str(chunk_id) for chunk_id in document.get("chunk_ids") or [] if str(chunk_id)]
            scoped_chunk_ids = [
                chunk_id
                for chunk_id in original_chunk_ids
                if chunk_project_ids.get(chunk_id) == project_id
            ]
            if document_project_id == project_id and not chunk_project_ids:
                scoped_chunk_ids = scoped_chunk_ids or original_chunk_ids
            if not scoped_chunk_ids:
                continue
            scoped_documents.append({
                **document,
                "project_id": project_id,
                "chunk_ids": scoped_chunk_ids,
            })
        source_documents = scoped_documents
    elif global_only:
        scoped_documents = []
        for document in source_documents:
            original_chunk_ids = [
                str(chunk_id)
                for chunk_id in document.get("chunk_ids") or []
                if str(chunk_id)
            ]
            document_project_id = str(document.get("project_id") or "").strip()
            known_chunk_ids = [chunk_id for chunk_id in original_chunk_ids if chunk_id in chunk_project_ids]
            if known_chunk_ids:
                # Current child metadata is authoritative after a document is
                # rebound. The graph document's project_id may be stale.
                scoped_chunk_ids = [
                    chunk_id
                    for chunk_id in known_chunk_ids
                    if not chunk_project_ids.get(chunk_id)
                ]
            elif chunk_project_ids:
                # Once a child-scope manifest is present, an omitted chunk is
                # unknown rather than global.  Treating it as shared would
                # silently widen the global index when metadata is partial.
                continue
            else:
                scoped_chunk_ids = original_chunk_ids
            if not scoped_chunk_ids:
                continue
            scoped_documents.append({
                **document,
                "project_id": "",
                "chunk_ids": scoped_chunk_ids,
            })
        source_documents = scoped_documents

    documents = sorted(
        source_documents,
        key=lambda item: (-_graph_document_priority(item)[0], _graph_document_priority(item)[1]),
    )[:max(1, int(max_documents))]
    allowed_chunk_ids = {
        str(chunk_id)
        for document in documents
        for chunk_id in document.get("chunk_ids") or []
        if str(chunk_id)
    }

    def scoped_chunks(item: dict[str, Any]) -> list[str]:
        return [
            str(chunk_id)
            for chunk_id in item.get("chunk_ids") or []
            if str(chunk_id) in allowed_chunk_ids
        ]

    # ponytail: deterministic filename/content priority; use a user-selected
    # corpus manifest when multiple independent GraphRAG indexes are needed.
    entities = []
    for item in snapshot.get("entities") or []:
        original_chunk_ids = {
            str(chunk_id)
            for chunk_id in item.get("chunk_ids") or []
            if str(chunk_id)
        }
        chunk_ids = scoped_chunks(item)
        if chunk_ids:
            scoped_item = _apply_scoped_entity_attributes(
                {**item, "chunk_ids": chunk_ids},
                chunk_ids,
                scope_requested=bool(project_id or global_only),
            )
            if (project_id or global_only) and original_chunk_ids - set(chunk_ids):
                # Entity descriptions are currently stored as one aggregate
                # value, while chunk provenance is finer grained.  When a
                # scoped index keeps only part of an entity's evidence, fail
                # closed instead of carrying an unbound description from
                # another project/document into the community index.
                if not scoped_item.get("source_attributes"):
                    scoped_item["type"] = ""
                    scoped_item["description"] = ""
            entities.append(scoped_item)
    relationships = []
    for item in snapshot.get("relationships") or []:
        chunk_ids = scoped_chunks(item)
        if chunk_ids:
            relationships.append({**item, "chunk_ids": chunk_ids})
    return {"entities": entities, "relationships": relationships, "documents": documents}


@dataclass(frozen=True)
class GraphRoute:
    use_graph: bool
    mode: str
    reason: str
    signals: tuple[str, ...] = ()
    estimated_cost: str = "none"

    def public_dict(self) -> dict[str, Any]:
        return {
            "use_graph": self.use_graph,
            "mode": self.mode,
            "reason": self.reason,
            "signals": list(self.signals),
            "estimated_cost": self.estimated_cost,
        }


def route_graph_query(query: str) -> GraphRoute:
    """只为全局归纳和关系型问题启用 GraphRAG，普通事实查询不升级成本。"""
    normalized = " ".join(str(query or "").lower().split())
    if not normalized:
        return GraphRoute(False, "hybrid", "empty_query")

    global_hits = tuple(marker for marker in GLOBAL_MARKERS if marker in normalized)
    local_hits = tuple(marker for marker in LOCAL_GRAPH_MARKERS if marker in normalized)
    local_relation_hits = tuple(
        marker for marker in LOCAL_RELATION_MARKERS if marker in normalized
    )
    source_hits = tuple(marker for marker in SOURCE_DIVERSITY_MARKERS if marker in normalized)
    if local_relation_hits:
        return GraphRoute(
            True,
            "local",
            "entity_relation_or_multi_hop_question",
            tuple(dict.fromkeys(local_relation_hits)),
            "medium_index_lookup",
        )
    if global_hits or (source_hits and ("总结" in normalized or "分析" in normalized)):
        return GraphRoute(
            True,
            "global",
            "cross_document_or_global_synthesis",
            tuple(dict.fromkeys((*global_hits, *source_hits))),
            "high_once_per_indexed_corpus",
        )
    if local_hits:
        return GraphRoute(
            True,
            "local",
            "entity_relation_or_multi_hop_question",
            tuple(dict.fromkeys(local_hits)),
            "medium_index_lookup",
        )
    return GraphRoute(False, "hybrid", "single_evidence_lookup", (), "low")


def leiden_partition(
    adjacency: dict[str, dict[str, float]],
    *,
    resolution: float = 1.0,
    seed: int = 17,
    max_passes: int = 12,
) -> list[set[str]]:
    """运行小图版本的 Leiden：局部移动 + 连通性 refinement。

    该实现避免给本机端侧引入 igraph 二进制依赖；它保留 Leiden 的关键
    refinement 约束，适合本项目不超过十篇资料的实验图。大规模图需要换成
    igraph/leidenalg，并保持同一快照协议。
    """
    nodes = sorted(adjacency)
    if not nodes:
        return []
    degrees = {node: sum(max(0.0, weight) for weight in adjacency[node].values()) for node in nodes}
    total_degree = sum(degrees.values()) or 1.0
    labels = {node: node for node in nodes}

    for pass_index in range(max(1, max_passes)):
        order = list(nodes)
        random.Random(seed + pass_index).shuffle(order)
        community_totals: dict[str, float] = defaultdict(float)
        for node, label in labels.items():
            community_totals[label] += degrees[node]
        moved = False

        for node in order:
            current = labels[node]
            degree = degrees[node]
            community_totals[current] -= degree
            neighbor_weights: dict[str, float] = defaultdict(float)
            for neighbor, weight in adjacency[node].items():
                neighbor_weights[labels[neighbor]] += max(0.0, float(weight))

            best_label = current
            best_gain = 0.0
            for label in sorted(neighbor_weights):
                if label == current:
                    continue
                internal_weight = neighbor_weights[label]
                gain = internal_weight - resolution * degree * community_totals[label] / total_degree
                if gain > best_gain + 1e-9:
                    best_label = label
                    best_gain = gain

            labels[node] = best_label
            community_totals[best_label] += degree
            if best_label != current:
                moved = True

        if not moved:
            break

    groups: dict[str, set[str]] = defaultdict(set)
    for node, label in labels.items():
        groups[label].add(node)
    return _connected_refinement(groups.values(), adjacency)


def _connected_refinement(groups, adjacency: dict[str, dict[str, float]]) -> list[set[str]]:
    refined: list[set[str]] = []
    for group in groups:
        remaining = set(group)
        while remaining:
            start = min(remaining)
            remaining.remove(start)
            component = {start}
            queue = deque([start])
            while queue:
                node = queue.popleft()
                for neighbor, weight in adjacency[node].items():
                    if weight <= 0 or neighbor not in remaining or neighbor not in group:
                        continue
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
            refined.append(component)
    return sorted(refined, key=lambda group: (min(group), len(group)))


class GraphRAGBuilder:
    def __init__(
        self,
        graph_store,
        *,
        report_generator: Callable[[dict[str, Any]], str] | None = None,
        min_communities: int = 3,
        max_communities: int = 4,
        max_documents: int = DEFAULT_GRAPH_DOCUMENT_LIMIT,
        seed: int = 17,
        project_id: str | None = None,
        global_only: bool = False,
        chunk_project_ids: dict[str, str] | None = None,
        index_name: str | None = None,
        source_snapshot: dict[str, Any] | None = None,
    ):
        self.graph_store = graph_store
        self.report_generator = report_generator
        self.min_communities = max(1, int(min_communities))
        self.max_communities = max(self.min_communities, int(max_communities))
        self.max_documents = max(1, int(max_documents))
        self.seed = int(seed)
        self.project_id = str(project_id or "").strip() or None
        self.global_only = bool(global_only)
        self.chunk_project_ids = dict(chunk_project_ids or {})
        self.source_snapshot = source_snapshot
        self.index_name = str(index_name or (
            "global_shared"
            if self.global_only
            else f"project:{self.project_id}" if self.project_id else "default"
        ))

    def build(self) -> dict[str, Any]:
        source_snapshot = (
            self.source_snapshot
            if self.source_snapshot is not None
            else self.graph_store.get_graph_snapshot()
        )
        source_snapshot_digest = _graph_snapshot_digest(source_snapshot)
        source_scope_digest = _graph_scope_digest(
            source_snapshot,
            self.chunk_project_ids,
            project_id=self.project_id,
            global_only=self.global_only,
        )
        snapshot = _scope_graph_snapshot(
            source_snapshot,
            self.max_documents,
            project_id=self.project_id,
            global_only=self.global_only,
            chunk_project_ids=self.chunk_project_ids,
        )
        entity_items = [
            item
            for item in snapshot.get("entities") or []
            if not _is_graph_entity_noise(str(item.get("name") or ""))
        ]
        adjacency = _build_adjacency(snapshot.get("relationships") or [], entity_items)
        resolution, groups = _select_resolution(
            adjacency,
            min_communities=self.min_communities,
            max_communities=self.max_communities,
            seed=self.seed,
        )
        groups = _merge_communities(groups, adjacency, self.max_communities)
        documents = list(snapshot.get("documents") or [])
        entities = {str(item.get("name") or ""): item for item in entity_items}
        relationships = [
            item
            for item in snapshot.get("relationships") or []
            if str(item.get("source") or "") in entities
            and str(item.get("target") or "") in entities
        ]
        communities = []
        for index, group in enumerate(groups, 1):
            community = _community_payload(
                community_id=f"C{index:03d}",
                entity_names=sorted(group),
                entities=entities,
                relationships=relationships,
                documents=documents,
            )
            community["report"] = self._report(community)
            communities.append(community)

        root = {
            "community_id": "ROOT",
            "level": 0,
            "parent_id": None,
            "title": "全库主题总览",
            "entity_names": sorted(entities),
            "source_filenames": sorted({
                source
                for community in communities
                for source in community["source_filenames"]
            }),
            "source_document_ids": sorted({
                document_id
                for community in communities
                for document_id in community.get("source_document_ids") or []
            }),
            "chunk_ids": sorted({
                chunk_id
                for community in communities
                for chunk_id in community["chunk_ids"]
            }),
            "children": [],
        }
        themes, cross_community_relationships = _community_hierarchy(communities, relationships, seed=self.seed)
        root["children"] = [theme["community_id"] for theme in themes]
        root["report"] = _root_report(themes or communities)
        payload = {
            "schema_version": "souldrive_graphrag_index_v4",
            "algorithm": "leiden_v1_local_move_refine_hierarchy",
            "seed": self.seed,
            "resolution": resolution,
            "built_at": time.time(),
            "source_snapshot_digest": source_snapshot_digest,
            "source_scope_digest": source_scope_digest,
            "scope_metadata_required": bool(self.chunk_project_ids),
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "document_count": len(documents),
            "community_count": len(communities),
            "target_community_range": [self.min_communities, self.max_communities],
            "target_range_ok": self.min_communities <= len(communities) <= self.max_communities,
            "levels": [
                root,
                *themes,
                *[
                    {**item, "level": 2, "parent_id": next(
                        (theme["community_id"] for theme in themes if item["community_id"] in theme["children"]),
                        "ROOT",
                    )}
                    for item in communities
                ],
            ],
            "communities": communities,
            "community_relationships": cross_community_relationships,
            "memory_policy": "document_derived_index_only; user_memory_is_not_graph_data",
            "project_id": self.project_id,
            "index_name": self.index_name,
            "global_only": self.global_only,
        }
        try:
            self.graph_store.save_graphrag_index(payload, index_name=self.index_name)
        except TypeError:
            if self.index_name != "default":
                raise
            self.graph_store.save_graphrag_index(payload)
        return payload

    def _report(self, community: dict[str, Any]) -> str:
        if self.report_generator is not None:
            try:
                generated = _clean_report(self.report_generator(community))
                if generated and _community_report_has_grounding_signal(community, generated):
                    community["report_method"] = "llm"
                    return generated
            except Exception:
                pass
        community["report_method"] = "structured_template"
        return _structured_report(community)


class GraphRAGIndex:
    def __init__(
        self,
        graph_store,
        *,
        scope_metadata_provider: Callable[[list[str]], dict[str, Any]] | None = None,
    ):
        self.graph_store = graph_store
        self.scope_metadata_provider = scope_metadata_provider
        self._stale_index_names: set[str] = set()
        self._unverified_index_names: set[str] = set()
        self._freshness_errors: dict[str, dict[str, Any]] = {}
        self._scope_unverified_index_names: set[str] = set()
        self._scope_freshness_errors: dict[str, dict[str, Any]] = {}

    def _load(self, index_name: str = "default"):
        try:
            return self.graph_store.load_graphrag_index(index_name=index_name)
        except TypeError:
            if index_name != "default":
                return None
            return self.graph_store.load_graphrag_index()

    def _load_checked(self, index_name: str):
        payload = self._load(index_name)
        if payload is None:
            return None
        project_scoped = bool(str(payload.get("project_id") or "").strip())
        expected_digest = str(payload.get("source_snapshot_digest") or "").strip()
        if not expected_digest:
            # Indexes written before source-digest support remain readable, but
            # diagnostics must make their freshness limitation explicit.
            self._unverified_index_names.add(index_name)
            self._stale_index_names.discard(index_name)
            if project_scoped:
                # A project index without a source digest cannot prove that
                # its document-to-project binding is still current.
                self._scope_unverified_index_names.add(index_name)
                return None
            return payload
        self._unverified_index_names.discard(index_name)
        try:
            current_snapshot = self.graph_store.get_graph_snapshot()
            current_digest = _graph_snapshot_digest(current_snapshot)
        except Exception as exc:
            self._stale_index_names.discard(index_name)
            self._freshness_errors[index_name] = _error_observation(exc)
            return None
        if current_digest != expected_digest:
            self._stale_index_names.add(index_name)
            self._freshness_errors.pop(index_name, None)
            return None
        self._stale_index_names.discard(index_name)
        self._freshness_errors.pop(index_name, None)

        expected_scope_digest = str(payload.get("source_scope_digest") or "").strip()
        scope_metadata_required = bool(payload.get("scope_metadata_required"))
        if project_scoped and (not expected_scope_digest or not scope_metadata_required):
            # A readable legacy project payload is not evidence that its
            # current chunks still belong to the requested project.
            self._scope_unverified_index_names.add(index_name)
            self._scope_freshness_errors.pop(index_name, None)
            return None
        if expected_scope_digest and bool(payload.get("scope_metadata_required")):
            provider = self.scope_metadata_provider
            if not callable(provider):
                self._scope_unverified_index_names.add(index_name)
                self._scope_freshness_errors.pop(index_name, None)
                # A mixed all-project index has the same isolation risk as a
                # project index when its child-scope manifest cannot be
                # revalidated.  Only the explicitly global shared index keeps
                # the older read-only compatibility behavior.
                if project_scoped or str(index_name) == "all_projects":
                    return None
                # Older callers that only inspect a global graph store remain
                # compatible, but diagnostics show that external metadata was
                # not revalidated.
                return payload
            chunk_ids = sorted({
                str(chunk_id)
                for document in current_snapshot.get("documents") or []
                for chunk_id in document.get("chunk_ids") or []
                if str(chunk_id)
            })
            try:
                current_scope_metadata = provider(chunk_ids)
                current_scope_digest = _graph_scope_digest(
                    current_snapshot,
                    current_scope_metadata,
                    project_id=payload.get("project_id"),
                    global_only=bool(payload.get("global_only")),
                )
            except Exception as exc:
                self._stale_index_names.discard(index_name)
                self._scope_freshness_errors[index_name] = _error_observation(exc)
                return None
            self._scope_unverified_index_names.discard(index_name)
            self._scope_freshness_errors.pop(index_name, None)
            if current_scope_digest != expected_scope_digest:
                self._stale_index_names.add(index_name)
                return None
        self._scope_unverified_index_names.discard(index_name)
        return payload

    def _freshness_status(self, index_name: str) -> str:
        if index_name in self._stale_index_names:
            return "stale"
        if index_name in self._freshness_errors:
            return "unavailable"
        if index_name in self._unverified_index_names:
            return "unverified"
        return "verified"

    def _scope_freshness_status(self, index_name: str) -> str:
        if index_name in self._stale_index_names:
            return "stale"
        if index_name in self._scope_freshness_errors:
            return "unavailable"
        if index_name in self._scope_unverified_index_names:
            return "unverified"
        return "verified"

    def _load_for_project(
        self,
        project_id: str | None,
        *,
        index_name_override: str | None = None,
    ):
        normalized_project_id = str(project_id or "").strip() or None
        if index_name_override:
            index_name = str(index_name_override).strip()
            if not index_name:
                return None, "", False
            return self._load_checked(index_name), index_name, False
        # An unbound request may only read the explicitly global graph index.
        # The historical ``default`` index could contain project documents
        # because it was built before project-scoped indexes were introduced.
        # Do not let a missing project binding silently widen that corpus.
        index_name = f"project:{normalized_project_id}" if normalized_project_id else "global_shared"
        payload = self._load_checked(index_name)
        if not normalized_project_id and payload is None:
            # Keep a narrow compatibility path for callers that explicitly
            # built a global-only legacy index under ``default``.  A mixed or
            # unmarked default payload is intentionally not trusted.
            global_index_blocked = any(
                index_name in registry
                for registry in (
                    self._stale_index_names,
                    self._freshness_errors,
                    self._scope_freshness_errors,
                )
            )
            if not global_index_blocked:
                legacy_payload = self._load_checked("default")
                if legacy_payload and bool(legacy_payload.get("global_only")):
                    return legacy_payload, "default", False
            return None, index_name, False
        if normalized_project_id and index_name in self._scope_unverified_index_names:
            # Do not hide an unverifiable project index behind the shared
            # global fallback: that would silently change the requested scope.
            return None, index_name, False
        if normalized_project_id and not (payload and payload.get("communities")):
            shared_payload = self._load_checked("global_shared")
            if shared_payload and shared_payload.get("communities"):
                return shared_payload, "global_shared", True
        return payload, index_name, False

    def diagnostics(
        self,
        project_id: str | None = None,
        *,
        index_name: str | None = None,
    ) -> dict[str, Any]:
        """Return health for the requested project or an explicit index.

        The default remains the project/shared read path for backwards
        compatibility.  Operators and cross-project callers can select a
        named derived index (notably ``all_projects``) without accidentally
        reporting the shared-only index as its health.
        """
        payload, resolved_index_name, shared_fallback = self._load_for_project(
            project_id,
            index_name_override=index_name,
        )
        index_name = resolved_index_name
        if not payload:
            if index_name in self._stale_index_names:
                return {
                    "ready": False,
                    "status": "stale",
                    "reason": "graphrag_index_stale",
                    "algorithm": "leiden_v1_local_move_refine",
                    "memory_policy": "user_memory_excluded",
                    "project_id": project_id,
                    "index_name": index_name,
                    "source_freshness": self._freshness_status(index_name),
                    "scope_freshness": self._scope_freshness_status(index_name),
                }
            if index_name in self._freshness_errors:
                return {
                    "ready": False,
                    "status": "unavailable",
                    "reason": "graphrag_source_snapshot_unavailable",
                    "algorithm": "leiden_v1_local_move_refine",
                    "memory_policy": "user_memory_excluded",
                    "project_id": project_id,
                    "index_name": index_name,
                    "source_freshness": self._freshness_status(index_name),
                    "scope_freshness": self._scope_freshness_status(index_name),
                    **self._freshness_errors[index_name],
                }
            if index_name in self._scope_freshness_errors:
                return {
                    "ready": False,
                    "status": "unavailable",
                    "reason": "graphrag_scope_metadata_unavailable",
                    "algorithm": "leiden_v1_local_move_refine",
                    "memory_policy": "user_memory_excluded",
                    "project_id": project_id,
                    "index_name": index_name,
                    "source_freshness": self._freshness_status(index_name),
                    "scope_freshness": self._scope_freshness_status(index_name),
                    **self._scope_freshness_errors[index_name],
                }
            if index_name in self._scope_unverified_index_names:
                return {
                    "ready": False,
                    "status": "unverified",
                    "reason": "graphrag_scope_metadata_unverified",
                    "algorithm": "leiden_v1_local_move_refine",
                    "memory_policy": "user_memory_excluded",
                    "project_id": project_id,
                    "index_name": index_name,
                    "source_freshness": self._freshness_status(index_name),
                    "scope_freshness": self._scope_freshness_status(index_name),
                }
            return {
                "ready": False,
                "status": "not_built",
                "reason": "graphrag_index_not_built",
                "algorithm": "leiden_v1_local_move_refine",
                "memory_policy": "user_memory_excluded",
                "project_id": project_id,
                "index_name": index_name,
                "source_freshness": self._freshness_status(index_name),
            }
        communities = list(payload.get("communities") or [])
        scope_freshness = self._scope_freshness_status(index_name)
        scope_unverified = (
            bool(payload.get("scope_metadata_required"))
            and scope_freshness == "unverified"
        )
        return {
            "ready": bool(communities) and not scope_unverified,
            "status": (
                "unverified"
                if scope_unverified
                else "ready" if communities else "not_built"
            ),
            **({"reason": "graphrag_scope_metadata_unverified"} if scope_unverified else {}),
            "schema_version": payload.get("schema_version"),
            "algorithm": payload.get("algorithm"),
            "resolution": payload.get("resolution"),
            "entity_count": payload.get("entity_count", 0),
            "relationship_count": payload.get("relationship_count", 0),
            "document_count": payload.get("document_count", 0),
            "community_count": len(communities),
            "target_community_range": payload.get("target_community_range"),
            "target_range_ok": payload.get("target_range_ok", False),
            "communities": [
                {
                    "community_id": item.get("community_id"),
                    "title": item.get("title"),
                    "entity_count": len(item.get("entity_names") or []),
                    "source_count": len(
                        item.get("source_document_ids")
                        or item.get("source_filenames")
                        or []
                    ),
                    "entity_names": list(item.get("entity_names") or [])[:24],
                    "source_document_ids": list(item.get("source_document_ids") or [])[:12],
                    "source_filenames": list(item.get("source_filenames") or [])[:12],
                    "report": _clean_report(item.get("report"))[:1200],
                    "report_method": item.get("report_method"),
                }
                for item in communities
            ],
            "built_at": payload.get("built_at"),
            "memory_policy": payload.get("memory_policy"),
            "project_id": project_id or payload.get("project_id"),
            "index_name": payload.get("index_name", index_name),
            "scope_fallback": "global_shared" if shared_fallback else None,
            "source_freshness": self._freshness_status(index_name),
            "scope_freshness": scope_freshness,
        }

    def retrieve(
        self,
        query: str,
        mode: str = "global",
        limit: int = 4,
        project_id: str | None = None,
        index_name: str | None = None,
    ) -> dict[str, Any]:
        payload, index_name, shared_fallback = self._load_for_project(
            project_id,
            index_name_override=index_name,
        )
        if not payload and index_name in self._stale_index_names:
            return {
                "mode": mode,
                "communities": [],
                "graph_context": [],
                "evidence": [],
                "matched_entities": [],
                "chunk_ids": [],
                "index_name": index_name,
                "scope_fallback": "global_shared" if shared_fallback else None,
                "reason": "graphrag_index_stale",
                "source_freshness": self._freshness_status(index_name),
                "scope_freshness": self._scope_freshness_status(index_name),
            }
        if not payload and index_name in self._freshness_errors:
            return {
                "mode": mode,
                "communities": [],
                "graph_context": [],
                "evidence": [],
                "matched_entities": [],
                "chunk_ids": [],
                "index_name": index_name,
                "scope_fallback": "global_shared" if shared_fallback else None,
                "reason": "graphrag_source_snapshot_unavailable",
                "source_freshness": self._freshness_status(index_name),
                "scope_freshness": self._scope_freshness_status(index_name),
            }
        if not payload and index_name in self._scope_freshness_errors:
            return {
                "mode": mode,
                "communities": [],
                "graph_context": [],
                "evidence": [],
                "matched_entities": [],
                "chunk_ids": [],
                "index_name": index_name,
                "scope_fallback": "global_shared" if shared_fallback else None,
                "reason": "graphrag_scope_metadata_unavailable",
                "source_freshness": self._freshness_status(index_name),
                "scope_freshness": self._scope_freshness_status(index_name),
            }
        if not payload and index_name in self._scope_unverified_index_names:
            return {
                "mode": mode,
                "communities": [],
                "graph_context": [],
                "evidence": [],
                "matched_entities": [],
                "chunk_ids": [],
                "index_name": index_name,
                "scope_fallback": "global_shared" if shared_fallback else None,
                "reason": "graphrag_scope_metadata_unverified",
                "source_freshness": self._freshness_status(index_name),
                "scope_freshness": self._scope_freshness_status(index_name),
            }
        if not payload and index_name == "all_projects":
            return {
                "mode": mode,
                "communities": [],
                "graph_context": [],
                "evidence": [],
                "matched_entities": [],
                "chunk_ids": [],
                "index_name": index_name,
                "scope_fallback": "global_shared" if shared_fallback else None,
                "reason": "cross_project_graph_index_not_built",
                "source_freshness": self._freshness_status(index_name),
                "scope_freshness": self._scope_freshness_status(index_name),
            }
        result = self._retrieve_payload(
            payload,
            query=query,
            mode=mode,
            limit=limit,
            index_name=index_name,
            shared_fallback=shared_fallback,
        )
        result["source_freshness"] = self._freshness_status(index_name)
        result["scope_freshness"] = self._scope_freshness_status(index_name)
        return result

    def retrieve_all_projects(
        self,
        query: str,
        mode: str = "global",
        limit: int = 4,
    ) -> dict[str, Any]:
        """Read the explicitly built all-project graph index.

        ``global_shared`` contains only unbound shared material.  An explicit
        cross-project query needs a separate mixed-corpus index so that its
        graph leg has the same scope as the vector leg.
        """
        return self.retrieve(
            query,
            mode=mode,
            limit=limit,
            project_id=None,
            index_name="all_projects",
        )

    def retrieve_project_and_global(
        self,
        query: str,
        mode: str = "global",
        limit: int = 4,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the project index and the shared global index together.

        A project GraphRAG index is not a replacement for the shared corpus:
        a project may contain local material while the answer still depends on
        a globally imported standard or policy document.
        """
        normalized_project_id = str(project_id or "").strip() or None
        if not normalized_project_id:
            return self.retrieve(query, mode=mode, limit=limit, project_id=None)

        project_result = self.retrieve(
            query,
            mode=mode,
            limit=limit,
            project_id=normalized_project_id,
        )
        # When the project index is absent, retrieve() already falls back to
        # global_shared. Do not duplicate the same communities and pointers.
        if project_result.get("scope_fallback") == "global_shared":
            return project_result
        if project_result.get("reason"):
            # A project scope failure must not be converted into a seemingly
            # successful project-plus-global answer.
            return project_result

        shared_payload = self._load_checked("global_shared")
        if not (shared_payload and shared_payload.get("communities")):
            return project_result
        shared_result = self._retrieve_payload(
            shared_payload,
            query=query,
            mode=mode,
            limit=limit,
            index_name="global_shared",
            shared_fallback=False,
        )

        project_evidence = [
            {**item, "graph_scope": "project"}
            for item in project_result.get("evidence") or []
        ]
        shared_evidence = [
            {
                **item,
                "candidate_id": f"{item.get('candidate_id') or 'graphrag'}@global_shared",
                "graph_scope": "global_shared",
            }
            for item in shared_result.get("evidence") or []
        ]
        project_communities = [
            f"project:{community_id}"
            for community_id in project_result.get("communities") or []
        ]
        shared_communities = [
            f"global_shared:{community_id}"
            for community_id in shared_result.get("communities") or []
        ]
        project_context = list(project_result.get("graph_context") or [])
        shared_context = [
            "[全局共享知识]" + context
            for context in shared_result.get("graph_context") or []
        ]
        project_expansion = dict(project_result.get("graph_expansion") or {})
        shared_expansion = dict(shared_result.get("graph_expansion") or {})
        expanded_communities = [
            *(f"project:{item}" for item in project_expansion.get("expanded_communities") or []),
            *(f"global_shared:{item}" for item in shared_expansion.get("expanded_communities") or []),
        ]
        project_chunk_ids = list(project_result.get("chunk_ids") or [])
        global_chunk_ids = list(shared_result.get("chunk_ids") or [])
        return {
            "mode": mode,
            "communities": project_communities + shared_communities,
            "graph_context": project_context + shared_context,
            "evidence": project_evidence + shared_evidence,
            "matched_entities": list(dict.fromkeys([
                *(project_result.get("matched_entities") or []),
                *(shared_result.get("matched_entities") or []),
            ]))[:12],
            "chunk_ids": list(dict.fromkeys([*project_chunk_ids, *global_chunk_ids])),
            "project_chunk_ids": project_chunk_ids,
            "global_chunk_ids": global_chunk_ids,
            "index_name": f"{project_result.get('index_name') or f'project:{normalized_project_id}'}+global_shared",
            "scope_fallback": "project_plus_global",
            "source_freshness": (
                "verified"
                if project_result.get("source_freshness") == "verified"
                and self._freshness_status("global_shared") == "verified"
                else "unverified"
            ),
            "scope_freshness": (
                "verified"
                if project_result.get("scope_freshness") == "verified"
                and self._scope_freshness_status("global_shared") == "verified"
                else "unverified"
            ),
            "graph_expansion": {
                "mode": "one_hop" if expanded_communities else None,
                "expanded_communities": expanded_communities,
                "expanded_count": len(expanded_communities),
            },
        }

    @staticmethod
    def _retrieve_payload(
        payload: dict[str, Any] | None,
        *,
        query: str,
        mode: str,
        limit: int,
        index_name: str,
        shared_fallback: bool,
    ) -> dict[str, Any]:
        payload = payload or {}
        communities = list(payload.get("communities") or [])
        query_terms = _terms(query)
        scored = []
        for community in communities:
            search_text = " ".join([
                str(community.get("title") or ""),
                str(community.get("report") or ""),
                " ".join(str(item) for item in community.get("entity_names") or []),
                " ".join(str(item) for item in community.get("relationships") or []),
                " ".join(str(item) for item in community.get("source_filenames") or []),
            ])
            score = _term_overlap(query_terms, _terms(search_text))
            scored.append((score, str(community.get("community_id") or ""), community))

        scored.sort(key=lambda item: (-item[0], item[1]))
        expanded_communities: list[str] = []
        if mode == "global":
            selected = [item[2] for item in scored[:max(1, min(limit, len(scored)))]]
            root = next((item for item in (payload.get("levels") or []) if item.get("community_id") == "ROOT"), None)
            if root and not selected and communities:
                selected = communities[:1]
        else:
            selected = [item[2] for item in scored if item[0] > 0][:max(1, min(limit, len(scored)))]
            if not selected and scored:
                selected = [scored[0][2]]
            selected_ids = {
                str(item.get("community_id") or "")
                for item in selected
                if str(item.get("community_id") or "")
            }
            community_by_id = {
                str(item.get("community_id") or ""): item
                for item in communities
                if str(item.get("community_id") or "")
            }
            neighbor_candidates: list[tuple[float, str, dict[str, Any]]] = []
            for edge in payload.get("community_relationships") or []:
                source_id = str(edge.get("source_community_id") or "")
                target_id = str(edge.get("target_community_id") or "")
                if not source_id or not target_id:
                    continue
                if source_id in selected_ids and target_id not in selected_ids:
                    neighbor_id = target_id
                elif target_id in selected_ids and source_id not in selected_ids:
                    neighbor_id = source_id
                else:
                    continue
                neighbor = community_by_id.get(neighbor_id)
                if neighbor is None:
                    continue
                relation_text = " ".join(str(item) for item in edge.get("relationships") or [])
                neighbor_text = " ".join([
                    relation_text,
                    str(neighbor.get("title") or ""),
                    " ".join(str(item) for item in neighbor.get("entity_names") or []),
                ])
                relation_score = _term_overlap(query_terms, _terms(neighbor_text))
                if relation_score <= 0:
                    continue
                neighbor_candidates.append((relation_score, neighbor_id, neighbor))
            for _, neighbor_id, neighbor in sorted(
                neighbor_candidates,
                key=lambda item: (-item[0], item[1]),
            ):
                if len(selected) >= max(1, min(limit, len(communities))):
                    break
                if neighbor_id in selected_ids:
                    continue
                selected.append(neighbor)
                selected_ids.add(neighbor_id)
                expanded_communities.append(neighbor_id)

        evidence = []
        graph_context = []
        matched_entities = []
        for community in selected:
            community_id = str(community.get("community_id") or "")
            expanded = community_id in expanded_communities
            report = _clean_report(community.get("report"))
            title = str(community.get("title") or community_id)
            sources = [str(item) for item in community.get("source_filenames") or []]
            source_document_ids = [
                str(item) for item in community.get("source_document_ids") or []
            ]
            entities = [str(item) for item in community.get("entity_names") or []]
            matched_entities.extend(item for item in entities if item in query and item not in matched_entities)
            graph_context.append(
                f"{'关系邻居扩展 ' if expanded else ''}社区报告 {community_id}（{title}）\n"
                f"核心实体：{'、'.join(entities[:12])}\n"
                f"覆盖资料：{'、'.join(sources[:8]) or '未登记来源'}\n"
                f"摘要：{report}"
            )
            evidence.append({
                "candidate_id": f"graphrag:{community_id}",
                "source_filename": f"GraphRAG社区报告/{community_id}",
                "section": title,
                "snippet": report,
                "score": round(0.2 + min(0.5, _term_overlap(query_terms, _terms(report))), 6),
                "graph_community_id": community_id,
                "source_filenames": sources,
                "source_document_ids": source_document_ids,
                "chunk_ids": list(community.get("chunk_ids") or []),
                "derived_index": True,
                "graph_expansion": "one_hop" if expanded else None,
            })

        return {
            "mode": mode,
            "communities": [str(item.get("community_id") or "") for item in selected],
            "graph_context": graph_context,
            "evidence": evidence,
            "matched_entities": matched_entities[:12],
            "chunk_ids": sorted({
                str(chunk_id)
                for community in selected
                for chunk_id in community.get("chunk_ids") or []
            }),
            "graph_expansion": {
                "mode": "one_hop" if expanded_communities else None,
                "expanded_communities": expanded_communities,
                "expanded_count": len(expanded_communities),
            },
            "index_name": index_name,
            "scope_fallback": "global_shared" if shared_fallback else None,
        }


def _build_adjacency(
    relationships: list[dict[str, Any]],
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = defaultdict(dict)
    for item in entities or []:
        name = str(item.get("name") or "").strip()
        if name:
            adjacency.setdefault(name, {})
    for item in relationships:
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target or source == target:
            continue
        adjacency.setdefault(source, {})
        adjacency.setdefault(target, {})
        adjacency[source][target] = adjacency[source].get(target, 0.0) + 1.0
        adjacency[target][source] = adjacency[target].get(source, 0.0) + 1.0
    return {node: dict(neighbors) for node, neighbors in adjacency.items()}


def _select_resolution(adjacency, *, min_communities: int, max_communities: int, seed: int):
    candidates = []
    for resolution in (0.6, 0.8, 1.0, 1.2, 1.5, 2.0):
        groups = leiden_partition(adjacency, resolution=resolution, seed=seed)
        count = len(groups)
        distance = 0 if min_communities <= count <= max_communities else min(
            abs(count - min_communities), abs(count - max_communities)
        )
        quality = _partition_quality(groups, adjacency)
        candidates.append((distance, -quality, abs(resolution - 1.0), resolution, groups))
    _, _, _, resolution, groups = min(candidates, key=lambda item: item[:4])
    return resolution, groups


def _partition_quality(groups: list[set[str]], adjacency: dict[str, dict[str, float]]) -> float:
    if not groups:
        return 0.0
    total = 0.0
    inside = 0.0
    for node, neighbors in adjacency.items():
        total += sum(neighbors.values())
        for neighbor, weight in neighbors.items():
            if any(node in group and neighbor in group for group in groups):
                inside += weight
    return inside / total if total else 0.0


def _merge_communities(groups, adjacency, max_communities: int):
    groups = [set(group) for group in groups if group]
    while len(groups) > max_communities:
        smallest_index = min(range(len(groups)), key=lambda index: (len(groups[index]), min(groups[index])))
        smallest = groups.pop(smallest_index)
        best_index = 0
        best_weight = -1.0
        for index, target in enumerate(groups):
            weight = sum(adjacency[node].get(neighbor, 0.0) for node in smallest for neighbor in target)
            if weight > best_weight:
                best_weight = weight
                best_index = index
        groups[best_index].update(smallest)
    return sorted(groups, key=lambda group: (min(group), len(group)))


def _community_payload(*, community_id, entity_names, entities, relationships, documents):
    entity_chunks = {
        name: {str(item) for item in entities.get(name, {}).get("chunk_ids") or []}
        for name in entity_names
    }
    chunk_ids = sorted({chunk_id for values in entity_chunks.values() for chunk_id in values})
    chunk_id_set = set(chunk_ids)
    related_docs = [
        document
        for document in documents
        if chunk_id_set & {
            str(item) for item in document.get("chunk_ids") or []
        }
        and str(document.get("source_filename") or "").strip()
    ]
    if not related_docs:
        related_docs = [
            document
            for document in documents[:4]
            if str(document.get("source_filename") or "").strip()
        ]
    source_filenames = sorted({
        str(document.get("source_filename") or "")
        for document in related_docs
    })
    inside_relations = [
        f"{item.get('source')} --({item.get('relation')})--> {item.get('target')}"
        for item in relationships
        if item.get("source") in entity_names and item.get("target") in entity_names
    ]
    source_document_ids = sorted({
        document_id
        for document in related_docs
        for document_id in [_graph_document_identity(document)]
        if document_id
    })
    title = " / ".join(entity_names[:3]) or "未命名主题"
    entity_attributes = []
    for name in entity_names:
        entity = entities.get(name) or {}
        entity_type = str(entity.get("type") or "").strip()
        description = str(entity.get("description") or "").strip()
        if not entity_type and not description:
            continue
        entity_attributes.append({
            "name": name,
            "type": entity_type[:120],
            "description": description[:900],
        })
        if len(entity_attributes) >= 24:
            break
    return {
        "community_id": community_id,
        "level": 1,
        "parent_id": "ROOT",
        "title": title[:180],
        "entity_names": entity_names,
        "entity_attributes": entity_attributes,
        "source_document_ids": source_document_ids,
        "source_filenames": source_filenames,
        "chunk_ids": chunk_ids,
        "relationships": inside_relations[:24],
        "document_summaries": [
            {
                "document_id": _graph_document_identity(document),
                "source_filename": document.get("source_filename"),
                "summary": str(document.get("summary") or "")[:900],
                "keywords": list(document.get("keywords") or [])[:12],
            }
            for document in related_docs[:6]
        ],
    }


def _community_hierarchy(
    communities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """以跨社区关系构造 L1 主题，叶子社区保持为 L2。"""
    by_entity = {
        entity: str(community["community_id"])
        for community in communities
        for entity in community.get("entity_names") or []
    }
    adjacency = {str(community["community_id"]): {} for community in communities}
    relation_details: dict[tuple[str, str], list[str]] = defaultdict(list)
    for relation in relationships:
        source = by_entity.get(str(relation.get("source") or ""))
        target = by_entity.get(str(relation.get("target") or ""))
        if not source or not target or source == target:
            continue
        key = tuple(sorted((source, target)))
        adjacency[source][target] = adjacency[source].get(target, 0.0) + 1.0
        adjacency[target][source] = adjacency[target].get(source, 0.0) + 1.0
        relation_details[key].append(
            f"{relation.get('source')} --({relation.get('relation')})--> {relation.get('target')}"
        )
    groups = leiden_partition(adjacency, seed=seed) if adjacency else []
    themes = []
    for index, group in enumerate(groups, 1):
        children = sorted(group)
        members = [item for item in communities if item["community_id"] in group]
        title = " / ".join(item["title"] for item in members[:2])[:180] or "跨社区主题"
        sources = sorted({source for item in members for source in item.get("source_filenames") or []})
        source_document_ids = sorted({
            document_id
            for item in members
            for document_id in item.get("source_document_ids") or []
        })
        entities = sorted({name for item in members for name in item.get("entity_names") or []})
        chunk_ids = sorted({chunk_id for item in members for chunk_id in item.get("chunk_ids") or []})
        bridge_relations = [
            detail
            for (left, right), details in relation_details.items()
            if left in group and right in group
            for detail in details
        ][:12]
        theme = {
            "community_id": f"T{index:03d}", "level": 1, "parent_id": "ROOT",
            "title": title, "children": children, "entity_names": entities,
            "source_filenames": sources, "chunk_ids": chunk_ids,
            "source_document_ids": source_document_ids,
            "relationships": bridge_relations,
        }
        theme["report"] = _structured_report({
            **theme,
            "document_summaries": [
                summary for item in members for summary in item.get("document_summaries") or []
            ],
        })
        theme["report_method"] = "hierarchical_template"
        themes.append(theme)
    cross_relations = [
        {"source_community_id": left, "target_community_id": right, "relationships": details[:12]}
        for (left, right), details in sorted(relation_details.items())
    ]
    return themes, cross_relations


def _structured_report(community: dict[str, Any]) -> str:
    lines = [
        f"该社区围绕“{community.get('title') or '未命名主题'}”形成关系密集的知识单元。",
        "核心实体：" + "、".join(list(community.get("entity_names") or [])[:10]) + "。",
    ]
    relationships = list(community.get("relationships") or [])[:6]
    if relationships:
        lines.append("主要关系：" + "；".join(relationships) + "。")
    entity_attributes = [
        item for item in community.get("entity_attributes") or []
        if isinstance(item, dict)
        and (str(item.get("type") or "").strip() or str(item.get("description") or "").strip())
    ]
    if entity_attributes:
        lines.append(
            "实体属性："
            + "；".join(
                f"{item.get('name')}（{item.get('type') or '未分类'}）：{item.get('description') or '无描述'}"
                for item in entity_attributes[:6]
            )
            + "。"
        )
    summaries = [
        str(item.get("summary") or "").strip()
        for item in community.get("document_summaries") or []
        if str(item.get("summary") or "").strip()
    ]
    if summaries:
        lines.append("资料提炼：" + "；".join(summaries[:2]))
    return _clean_report("".join(lines))


def _root_report(communities: list[dict[str, Any]]) -> str:
    if not communities:
        return "当前知识图谱没有形成可用的社区报告。"
    parts = [
        f"全库可分为 {len(communities)} 个关系社区：",
        "；".join(
            f"{item.get('community_id')}={item.get('title')}"
            for item in communities
        ) + "。",
        "社区报告用于全局检索压缩上下文，回答具体事实时仍需回到原始证据块。",
    ]
    return _clean_report("".join(parts))


def _terms(text: Any) -> set[str]:
    normalized = str(text or "").lower()
    terms = {item for item in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", normalized)}
    for block in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(block) <= 4:
            terms.add(block)
        for size in (2, 3, 4):
            for index in range(0, max(0, len(block) - size + 1)):
                terms.add(block[index : index + size])
    return {term for term in terms if term not in STOP_TERMS and len(term) >= 2}


def _term_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _clean_report(value: Any) -> str:
    text = re.sub(r"```(?:text|markdown)?", "", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)
    text = " ".join(text.split()).strip()
    return text[:1800]


def _community_report_has_grounding_signal(community: dict[str, Any], report: str) -> bool:
    """Reject an otherwise non-empty report with no community-derived anchor."""
    anchors = [
        str(community.get("title") or ""),
        *(str(item) for item in community.get("entity_names") or []),
        *(str(item) for item in community.get("relationships") or []),
        *(str(item) for item in community.get("source_filenames") or []),
    ]
    anchor_terms = _terms(" ".join(anchors))
    report_terms = _terms(report)
    return bool(anchor_terms and report_terms and anchor_terms.intersection(report_terms))
