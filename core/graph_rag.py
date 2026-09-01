"""GraphRAG 的索引期提炼、Leiden 社区和成本感知查询路由。

原文、向量索引和用户记忆不由本模块替代：GraphRAG 只保存文档派生的专项
索引。社区报告没有原文证据时，查询层仍会把它和原始证据一起交给引用门控。
"""

from __future__ import annotations

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
    chunk_project_ids = {
        str(chunk_id): str(
            (value.get("project_id") if isinstance(value, dict) else value) or ""
        ).strip()
        for chunk_id, value in (chunk_project_ids or {}).items()
        if str(chunk_id)
    }

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
            elif document_project_id:
                # With incomplete metadata, fail closed for an explicitly
                # project-scoped graph document.
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
        chunk_ids = scoped_chunks(item)
        if chunk_ids:
            entities.append({**item, "chunk_ids": chunk_ids})
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
    source_hits = tuple(marker for marker in SOURCE_DIVERSITY_MARKERS if marker in normalized)
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
        self.index_name = str(index_name or (
            "global_shared"
            if self.global_only
            else f"project:{self.project_id}" if self.project_id else "default"
        ))

    def build(self) -> dict[str, Any]:
        snapshot = _scope_graph_snapshot(
            self.graph_store.get_graph_snapshot(),
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
            "schema_version": "souldrive_graphrag_index_v2",
            "algorithm": "leiden_v1_local_move_refine_hierarchy",
            "seed": self.seed,
            "resolution": resolution,
            "built_at": time.time(),
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
                if generated:
                    community["report_method"] = "llm"
                    return generated
            except Exception:
                pass
        community["report_method"] = "structured_template"
        return _structured_report(community)


class GraphRAGIndex:
    def __init__(self, graph_store):
        self.graph_store = graph_store

    def _load(self, index_name: str = "default"):
        try:
            return self.graph_store.load_graphrag_index(index_name=index_name)
        except TypeError:
            if index_name != "default":
                return None
            return self.graph_store.load_graphrag_index()

    def _load_for_project(self, project_id: str | None):
        normalized_project_id = str(project_id or "").strip() or None
        index_name = f"project:{normalized_project_id}" if normalized_project_id else "default"
        payload = self._load(index_name)
        if normalized_project_id and not (payload and payload.get("communities")):
            shared_payload = self._load("global_shared")
            if shared_payload and shared_payload.get("communities"):
                return shared_payload, "global_shared", True
        return payload, index_name, False

    def diagnostics(self, project_id: str | None = None) -> dict[str, Any]:
        payload, index_name, shared_fallback = self._load_for_project(project_id)
        if not payload:
            return {
                "ready": False,
                "status": "not_built",
                "reason": "graphrag_index_not_built",
                "algorithm": "leiden_v1_local_move_refine",
                "memory_policy": "user_memory_excluded",
                "project_id": project_id,
                "index_name": index_name,
            }
        communities = list(payload.get("communities") or [])
        return {
            "ready": bool(communities),
            "status": "ready" if communities else "not_built",
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
                    "source_count": len(item.get("source_filenames") or []),
                    "entity_names": list(item.get("entity_names") or [])[:24],
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
        }

    def retrieve(
        self,
        query: str,
        mode: str = "global",
        limit: int = 4,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        payload, index_name, shared_fallback = self._load_for_project(project_id)
        return self._retrieve_payload(
            payload,
            query=query,
            mode=mode,
            limit=limit,
            index_name=index_name,
            shared_fallback=shared_fallback,
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

        shared_payload = self._load("global_shared")
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
                " ".join(str(item) for item in community.get("source_filenames") or []),
            ])
            score = _term_overlap(query_terms, _terms(search_text))
            scored.append((score, str(community.get("community_id") or ""), community))

        scored.sort(key=lambda item: (-item[0], item[1]))
        if mode == "global":
            selected = [item[2] for item in scored[:max(1, min(limit, len(scored)))]]
            root = next((item for item in (payload.get("levels") or []) if item.get("community_id") == "ROOT"), None)
            if root and not selected and communities:
                selected = communities[:1]
        else:
            selected = [item[2] for item in scored if item[0] > 0][:max(1, min(limit, len(scored)))]
            if not selected and scored:
                selected = [scored[0][2]]

        evidence = []
        graph_context = []
        matched_entities = []
        for community in selected:
            community_id = str(community.get("community_id") or "")
            report = _clean_report(community.get("report"))
            title = str(community.get("title") or community_id)
            sources = [str(item) for item in community.get("source_filenames") or []]
            entities = [str(item) for item in community.get("entity_names") or []]
            matched_entities.extend(item for item in entities if item in query and item not in matched_entities)
            graph_context.append(
                f"社区报告 {community_id}（{title}）\n"
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
                "chunk_ids": list(community.get("chunk_ids") or []),
                "derived_index": True,
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
    source_filenames = sorted({
        str(document.get("source_filename") or "")
        for document in documents
        if set(str(item) for item in document.get("chunk_ids") or []) & set(chunk_ids)
    })
    if not source_filenames:
        source_filenames = sorted({
            str(document.get("source_filename") or "")
            for document in documents[:4]
            if document.get("source_filename")
        })
    inside_relations = [
        f"{item.get('source')} --({item.get('relation')})--> {item.get('target')}"
        for item in relationships
        if item.get("source") in entity_names and item.get("target") in entity_names
    ]
    related_docs = [
        document for document in documents
        if str(document.get("source_filename") or "") in source_filenames
    ]
    title = " / ".join(entity_names[:3]) or "未命名主题"
    return {
        "community_id": community_id,
        "level": 1,
        "parent_id": "ROOT",
        "title": title[:180],
        "entity_names": entity_names,
        "source_filenames": source_filenames,
        "chunk_ids": chunk_ids,
        "relationships": inside_relations[:24],
        "document_summaries": [
            {
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
