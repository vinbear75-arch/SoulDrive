import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENTITY_AAD = b"SoulDrive secure graph entity v1"
RELATIONSHIP_AAD = b"SoulDrive secure graph relationship v1"
DOCUMENT_AAD = b"SoulDrive secure graph document v1"
GRAPHRAG_INDEX_AAD = b"SoulDrive secure GraphRAG index v1"


@dataclass(frozen=True)
class _EntityRecord:
    name: str
    entity_type: str
    description: str
    chunk_ids: list[str]


@dataclass(frozen=True)
class _RelationshipRecord:
    source: str
    relation: str
    target: str
    chunk_ids: list[str]


@dataclass(frozen=True)
class _DocumentRecord:
    source_filename: str
    title: str
    summary: str
    keywords: list[str]
    chunk_ids: list[str]
    project_id: str


class SecureGraphStore:
    def __init__(self, db_path: str, keys):
        self.db_path = db_path
        self.keys = keys
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS relationships (
                relationship_id TEXT PRIMARY KEY,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS derived_indexes (
                index_id TEXT PRIMARY KEY,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def add_document(
        self,
        source_filename: str,
        title: str,
        summary: str,
        keywords: list[str] | None = None,
        chunk_ids: list[str] | None = None,
        project_id: str | None = None,
    ):
        source = str(source_filename or "").strip()
        if not source:
            return
        document_id = self._document_id(source)
        payload = {
            "source_filename": source,
            "title": str(title or source)[:240],
            "summary": str(summary or "")[:4000],
            "keywords": _dedupe(keywords or []),
            "chunk_ids": _dedupe(chunk_ids or []),
            "project_id": str(project_id or "").strip(),
        }
        nonce, ciphertext = self._encrypt_json(
            payload,
            DOCUMENT_AAD + document_id.encode("ascii"),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO documents (document_id, payload_nonce, payload_ciphertext)
            VALUES (?, ?, ?)
            """,
            (document_id, nonce, ciphertext),
        )
        self.conn.commit()

    def add_entity(self, name: str, entity_type: str, description: str, chunk_ids: list[str] | None = None):
        entity_id = self._entity_id(name)
        existing = self._entity_by_name(name)
        merged_chunk_ids = _dedupe([*(existing.chunk_ids if existing else []), *(chunk_ids or [])])
        payload = {
            "name": name,
            "type": entity_type,
            "description": description,
            "chunk_ids": merged_chunk_ids,
        }
        nonce, ciphertext = self._encrypt_json(payload, ENTITY_AAD + entity_id.encode("ascii"))
        self.conn.execute(
            """
            INSERT OR REPLACE INTO entities (entity_id, payload_nonce, payload_ciphertext)
            VALUES (?, ?, ?)
            """,
            (entity_id, nonce, ciphertext),
        )
        self.conn.commit()

    def add_relationship(self, source: str, target: str, relation: str, evidence_chunk_ids: list[str] | None = None):
        relationship_id = self._relationship_id(source, relation, target)
        existing = self._relationship_by_parts(source, relation, target)
        merged_chunk_ids = _dedupe([*(existing.chunk_ids if existing else []), *(evidence_chunk_ids or [])])
        payload = {
            "source": source,
            "target": target,
            "relation": relation,
            "chunk_ids": merged_chunk_ids,
        }
        nonce, ciphertext = self._encrypt_json(
            payload,
            RELATIONSHIP_AAD + relationship_id.encode("ascii"),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO relationships (relationship_id, payload_nonce, payload_ciphertext)
            VALUES (?, ?, ?)
            """,
            (relationship_id, nonce, ciphertext),
        )
        self.conn.commit()

    def get_all_entities(
        self,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[str]:
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)
        entities = [record.name for record in self._all_entities()]
        return sorted(
            (
                entity
                for entity in entities
                if allowed_chunk_ids is None
                or set(self._entity_chunk_ids(entity)) & allowed_chunk_ids
            ),
            key=lambda value: (-len(value), value),
        )

    def get_related_entities(
        self,
        entity_name: str,
        limit: int = 20,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[tuple[str, str, str]]:
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)
        matches = [
            (record.source, record.relation, record.target)
            for record in self._all_relationships()
            if (record.source == entity_name or record.target == entity_name)
            and (
                allowed_chunk_ids is None
                or set(record.chunk_ids) & allowed_chunk_ids
            )
        ]
        matches.sort(key=lambda item: (item[0], item[1], item[2]))
        return matches[:limit]

    def get_subgraph(
        self,
        entity_name: str,
        depth: int = 1,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[tuple[str, str, str]]:
        if depth < 1:
            return []

        max_edges = 50
        visited_entities = {entity_name}
        visited_edges = set()
        results = []
        frontier = [(entity_name, 0)]

        while frontier and len(results) < max_edges:
            current_entity, current_depth = frontier.pop(0)
            if current_depth >= depth:
                continue

            for source, relation, target in self.get_related_entities(
                current_entity,
                project_id=project_id,
                global_only=global_only,
            ):
                edge_key = (source, relation, target)
                if edge_key not in visited_edges:
                    visited_edges.add(edge_key)
                    results.append(edge_key)

                for next_entity in (source, target):
                    if next_entity not in visited_entities:
                        visited_entities.add(next_entity)
                        frontier.append((next_entity, current_depth + 1))

                if len(results) >= max_edges:
                    break

        return results

    def search_context(
        self,
        query: str,
        depth: int = 1,
        max_entities: int = 8,
        max_edges: int = 24,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        match_space = (query or "").lower()
        matched_entities = []
        for entity in self.get_all_entities(
            project_id=project_id,
            global_only=global_only,
        ):
            if entity and entity.lower() in match_space:
                matched_entities.append(entity)
            if len(matched_entities) >= max_entities:
                break

        graph_context = []
        seen_relations = set()
        for entity in matched_entities:
            for source, relation, target in self.get_subgraph(
                entity,
                depth=depth,
                project_id=project_id,
                global_only=global_only,
            ):
                relation_key = (source, relation, target)
                if relation_key in seen_relations:
                    continue
                seen_relations.add(relation_key)
                graph_context.append(f"宸茬煡閫昏緫鍏崇郴: [{source}] --({relation})--> [{target}]")
                if len(graph_context) >= max_edges:
                    return graph_context

        return graph_context

    def recall_chunk_ids(
        self,
        query: str,
        limit: int = 40,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[str]:
        match_space = (query or "").lower()
        matched = []
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)
        for entity in self._all_entities():
            if entity.name and entity.name.lower() in match_space:
                if (
                    allowed_chunk_ids is None
                    or set(self._entity_chunk_ids(entity.name)) & allowed_chunk_ids
                ):
                    matched.append(entity.name)

        chunk_ids = []
        seen = set()

        def add_chunk_id(chunk_id):
            if allowed_chunk_ids is not None and chunk_id not in allowed_chunk_ids:
                return
            if chunk_id not in seen:
                seen.add(chunk_id)
                chunk_ids.append(chunk_id)

        for entity_name in matched:
            for chunk_id in self._entity_chunk_ids(entity_name):
                add_chunk_id(chunk_id)
            for source, relation, target in self.get_subgraph(
                entity_name,
                depth=1,
                project_id=project_id,
                global_only=global_only,
            ):
                for related_entity in (source, target):
                    for chunk_id in self._entity_chunk_ids(related_entity):
                        add_chunk_id(chunk_id)
                for chunk_id in self._relationship_chunk_ids(source, relation, target):
                    add_chunk_id(chunk_id)
                if len(chunk_ids) >= limit:
                    return chunk_ids[:limit]
        return chunk_ids[:limit]

    def get_graph_snapshot(self) -> dict:
        entities = [
            {
                "name": record.name,
                "type": record.entity_type,
                "description": record.description,
                "chunk_ids": list(record.chunk_ids),
            }
            for record in self._all_entities()
        ]
        relationships = [
            {
                "source": record.source,
                "relation": record.relation,
                "target": record.target,
                "chunk_ids": list(record.chunk_ids),
            }
            for record in self._all_relationships()
        ]
        documents = [
            {
                "source_filename": record.source_filename,
                "title": record.title,
                "summary": record.summary,
                "keywords": list(record.keywords),
                "chunk_ids": list(record.chunk_ids),
                "project_id": record.project_id,
            }
            for record in self._all_documents()
        ]
        entities.sort(key=lambda item: item["name"])
        relationships.sort(key=lambda item: (item["source"], item["relation"], item["target"]))
        documents.sort(key=lambda item: item["source_filename"])
        return {"entities": entities, "relationships": relationships, "documents": documents}

    def save_graphrag_index(self, payload: dict, index_name: str = "default") -> None:
        index_name = str(index_name or "default").strip() or "default"
        index_id = self._index_id(index_name)
        nonce, ciphertext = self._encrypt_json(
            {"updated_at": time.time(), "payload": payload},
            GRAPHRAG_INDEX_AAD + index_id.encode("ascii"),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO derived_indexes (index_id, payload_nonce, payload_ciphertext)
            VALUES (?, ?, ?)
            """,
            (index_id, nonce, ciphertext),
        )
        self.conn.commit()

    def load_graphrag_index(self, index_name: str = "default") -> dict | None:
        index_name = str(index_name or "default").strip() or "default"
        index_id = self._index_id(index_name)
        row = self.conn.execute(
            """
            SELECT payload_nonce, payload_ciphertext
            FROM derived_indexes
            WHERE index_id = ?
            """,
            (index_id,),
        ).fetchone()
        if row is None:
            return None
        payload = self._decrypt_json(
            row[0],
            row[1],
            GRAPHRAG_INDEX_AAD + index_id.encode("ascii"),
        )
        nested = payload.get("payload")
        return nested if isinstance(nested, dict) else None

    def remove_chunk_ids(self, chunk_ids: list[str]) -> None:
        removed = {str(chunk_id) for chunk_id in chunk_ids if str(chunk_id)}
        if not removed:
            return

        for entity_id, entity in self._entity_rows():
            kept_chunk_ids = [chunk_id for chunk_id in entity.chunk_ids if chunk_id not in removed]
            if kept_chunk_ids == entity.chunk_ids:
                continue
            if not kept_chunk_ids:
                self.conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))
                continue
            payload = {
                "name": entity.name,
                "type": entity.entity_type,
                "description": entity.description,
                "chunk_ids": kept_chunk_ids,
            }
            nonce, ciphertext = self._encrypt_json(payload, ENTITY_AAD + entity_id.encode("ascii"))
            self.conn.execute(
                """
                UPDATE entities
                SET payload_nonce = ?, payload_ciphertext = ?
                WHERE entity_id = ?
                """,
                (nonce, ciphertext, entity_id),
            )

        for relationship_id, relationship in self._relationship_rows():
            kept_chunk_ids = [chunk_id for chunk_id in relationship.chunk_ids if chunk_id not in removed]
            if kept_chunk_ids == relationship.chunk_ids:
                continue
            if not kept_chunk_ids:
                self.conn.execute("DELETE FROM relationships WHERE relationship_id = ?", (relationship_id,))
                continue
            payload = {
                "source": relationship.source,
                "target": relationship.target,
                "relation": relationship.relation,
                "chunk_ids": kept_chunk_ids,
            }
            nonce, ciphertext = self._encrypt_json(
                payload,
                RELATIONSHIP_AAD + relationship_id.encode("ascii"),
            )
            self.conn.execute(
                """
                UPDATE relationships
                SET payload_nonce = ?, payload_ciphertext = ?
                WHERE relationship_id = ?
                """,
                (nonce, ciphertext, relationship_id),
            )

        for document_id, document in self._document_rows():
            kept_chunk_ids = [chunk_id for chunk_id in document.chunk_ids if chunk_id not in removed]
            if kept_chunk_ids == document.chunk_ids:
                continue
            if not kept_chunk_ids:
                self.conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
                continue
            payload = {
                "source_filename": document.source_filename,
                "title": document.title,
                "summary": document.summary,
                "keywords": document.keywords,
                "chunk_ids": kept_chunk_ids,
            }
            nonce, ciphertext = self._encrypt_json(
                payload,
                DOCUMENT_AAD + document_id.encode("ascii"),
            )
            self.conn.execute(
                """
                UPDATE documents
                SET payload_nonce = ?, payload_ciphertext = ?
                WHERE document_id = ?
                """,
                (nonce, ciphertext, document_id),
            )

        # 派生社区报告包含已删除 chunk 的指针，必须失效后再由索引阶段重建。
        self.conn.execute("DELETE FROM derived_indexes")
        self.conn.commit()

    def remove_project(self, project_id: str, chunk_ids: list[str] | None = None) -> None:
        project_id = str(project_id or "").strip()
        if not project_id:
            return
        self.remove_chunk_ids(chunk_ids or [])
        for document_id, document in self._document_rows():
            if document.project_id == project_id:
                self.conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        # Global and project GraphRAG reports are derived from the same graph;
        # deleting a project invalidates every report until the next rebuild.
        self.conn.execute("DELETE FROM derived_indexes")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def _all_entities(self) -> list[_EntityRecord]:
        return [record for _, record in self._entity_rows()]

    def _entity_rows(self) -> list[tuple[str, _EntityRecord]]:
        rows = self.conn.execute(
            """
            SELECT entity_id, payload_nonce, payload_ciphertext
            FROM entities
            ORDER BY entity_id ASC
            """
        ).fetchall()
        return [
            (
                row[0],
                _EntityRecord(
                    name=str(payload.get("name") or ""),
                    entity_type=str(payload.get("type") or ""),
                    description=str(payload.get("description") or ""),
                    chunk_ids=[str(item) for item in payload.get("chunk_ids") or []],
                ),
            )
            for row in rows
            for payload in [self._decrypt_json(row[1], row[2], ENTITY_AAD + row[0].encode("ascii"))]
        ]

    def _all_relationships(self) -> list[_RelationshipRecord]:
        return [record for _, record in self._relationship_rows()]

    def _all_documents(self) -> list[_DocumentRecord]:
        return [record for _, record in self._document_rows()]

    def _relationship_rows(self) -> list[tuple[str, _RelationshipRecord]]:
        rows = self.conn.execute(
            """
            SELECT relationship_id, payload_nonce, payload_ciphertext
            FROM relationships
            ORDER BY relationship_id ASC
            """
        ).fetchall()
        return [
            (
                row[0],
                _RelationshipRecord(
                    source=str(payload.get("source") or ""),
                    relation=str(payload.get("relation") or ""),
                    target=str(payload.get("target") or ""),
                    chunk_ids=[str(item) for item in payload.get("chunk_ids") or []],
                ),
            )
            for row in rows
            for payload in [self._decrypt_json(row[1], row[2], RELATIONSHIP_AAD + row[0].encode("ascii"))]
        ]

    def _document_rows(self) -> list[tuple[str, _DocumentRecord]]:
        rows = self.conn.execute(
            """
            SELECT document_id, payload_nonce, payload_ciphertext
            FROM documents
            ORDER BY document_id ASC
            """
        ).fetchall()
        return [
            (
                row[0],
                _DocumentRecord(
                    source_filename=str(payload.get("source_filename") or ""),
                    title=str(payload.get("title") or ""),
                    summary=str(payload.get("summary") or ""),
                    keywords=_dedupe(payload.get("keywords") or []),
                    chunk_ids=_dedupe(payload.get("chunk_ids") or []),
                    project_id=str(payload.get("project_id") or ""),
                ),
            )
            for row in rows
            for payload in [self._decrypt_json(row[1], row[2], DOCUMENT_AAD + row[0].encode("ascii"))]
        ]

    def _entity_id(self, name: str) -> str:
        return self._digest("entity:" + name)

    def _relationship_id(self, source: str, relation: str, target: str) -> str:
        return self._digest("rel:" + source + "\0" + relation + "\0" + target)

    def _document_id(self, source_filename: str) -> str:
        return self._digest("document:" + source_filename)

    def _index_id(self, name: str) -> str:
        return self._digest("index:" + name)

    def _entity_by_name(self, name: str) -> _EntityRecord | None:
        entity_id = self._entity_id(name)
        row = self.conn.execute(
            """
            SELECT payload_nonce, payload_ciphertext
            FROM entities
            WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        payload = self._decrypt_json(row[0], row[1], ENTITY_AAD + entity_id.encode("ascii"))
        return _EntityRecord(
            name=str(payload.get("name") or ""),
            entity_type=str(payload.get("type") or ""),
            description=str(payload.get("description") or ""),
            chunk_ids=[str(item) for item in payload.get("chunk_ids") or []],
        )

    def _relationship_by_parts(self, source: str, relation: str, target: str) -> _RelationshipRecord | None:
        relationship_id = self._relationship_id(source, relation, target)
        row = self.conn.execute(
            """
            SELECT payload_nonce, payload_ciphertext
            FROM relationships
            WHERE relationship_id = ?
            """,
            (relationship_id,),
        ).fetchone()
        if row is None:
            return None
        payload = self._decrypt_json(row[0], row[1], RELATIONSHIP_AAD + relationship_id.encode("ascii"))
        return _RelationshipRecord(
            source=str(payload.get("source") or ""),
            relation=str(payload.get("relation") or ""),
            target=str(payload.get("target") or ""),
            chunk_ids=[str(item) for item in payload.get("chunk_ids") or []],
        )

    def _entity_chunk_ids(self, name: str) -> list[str]:
        record = self._entity_by_name(name)
        return record.chunk_ids if record else []

    def _relationship_chunk_ids(self, source: str, relation: str, target: str) -> list[str]:
        record = self._relationship_by_parts(source, relation, target)
        return record.chunk_ids if record else []

    def _scope_chunk_ids(
        self,
        project_id: str | None,
        global_only: bool = False,
    ) -> set[str] | None:
        project_id = str(project_id or "").strip()
        if project_id:
            return {
                chunk_id
                for document in self._all_documents()
                if document.project_id == project_id
                for chunk_id in document.chunk_ids
            }
        if not global_only:
            return None
        return {
            chunk_id
            for document in self._all_documents()
            if not document.project_id
            for chunk_id in document.chunk_ids
        }

    def _digest(self, value: str) -> str:
        return hmac.new(self.keys.graph_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _encrypt_json(self, payload: dict, aad: bytes) -> tuple[str, str]:
        nonce = os.urandom(12)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ciphertext = AESGCM(self.keys.graph_key).encrypt(nonce, encoded, aad)
        return _b64(nonce), _b64(ciphertext)

    def _decrypt_json(self, nonce: str, ciphertext: str, aad: bytes) -> dict:
        try:
            payload = AESGCM(self.keys.graph_key).decrypt(_unb64(nonce), _unb64(ciphertext), aad)
        except InvalidTag as exc:
            raise ValueError("secure graph store authentication failed") from exc
        return json.loads(payload.decode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        item = str(value)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
