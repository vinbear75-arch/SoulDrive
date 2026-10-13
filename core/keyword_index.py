import json
import os
import re
import sqlite3
from dataclasses import dataclass
from functools import wraps
import threading
from typing import Any


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
KEYWORD_TABLE = "keyword_documents_v2"


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class KeywordSearchResult:
    doc_id: str
    content: str
    metadata: dict[str, Any]
    score: float


class KeywordIndex:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {KEYWORD_TABLE}
            USING fts5(
                doc_id UNINDEXED,
                content UNINDEXED,
                search_text,
                metadata_json UNINDEXED
            )
        """)
        self.conn.commit()

    @_synchronized
    def upsert_document(self, doc_id: str, content: str, metadata: dict[str, Any]):
        cursor = self.conn.cursor()
        search_text = " ".join(lexical_tokens(content))
        cursor.execute(f"DELETE FROM {KEYWORD_TABLE} WHERE doc_id = ?", (doc_id,))
        cursor.execute(f"""
            INSERT INTO {KEYWORD_TABLE} (doc_id, content, search_text, metadata_json)
            VALUES (?, ?, ?, ?)
        """, (doc_id, content, search_text, json.dumps(metadata, ensure_ascii=False)))
        self.conn.commit()

    @_synchronized
    def delete_by_document_hash(self, document_hash: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            DELETE FROM keyword_documents_v2
            WHERE metadata_json LIKE ?
        """, (f'%"{document_hash}"%',))
        self.conn.commit()

    @_synchronized
    def delete(self, doc_ids: list[str]):
        if not doc_ids:
            return
        placeholders = ",".join("?" for _ in doc_ids)
        self.conn.execute(
            f"DELETE FROM {KEYWORD_TABLE} WHERE doc_id IN ({placeholders})",
            tuple(doc_ids),
        )
        self.conn.commit()

    @_synchronized
    def contains_ids(self, doc_ids: list[str]) -> bool:
        """Return whether every requested child has a keyword projection."""
        expected = {str(doc_id) for doc_id in doc_ids if str(doc_id)}
        if not expected:
            return True
        placeholders = ",".join("?" for _ in expected)
        rows = self.conn.execute(
            f"SELECT doc_id FROM {KEYWORD_TABLE} WHERE doc_id IN ({placeholders})",
            tuple(sorted(expected)),
        ).fetchall()
        return {str(row[0]) for row in rows} == expected

    @_synchronized
    def search(
        self,
        query: str,
        limit: int = 12,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[KeywordSearchResult]:
        match_query = self._build_match_query(query)
        if not match_query:
            return []

        cursor = self.conn.cursor()
        where_clauses = ["search_text MATCH ?"]
        parameters: list[Any] = [match_query]
        scoped = project_id is not None or global_only or bool(document_names) or bool(document_ids)
        try:
            query_sql = f"""
                SELECT doc_id, content, metadata_json,
                       bm25(keyword_documents_v2, 0.0, 0.0, 1.0, 0.0) AS score
                FROM keyword_documents_v2
                WHERE {' AND '.join(where_clauses)}
                ORDER BY score ASC, doc_id ASC
            """
            if not scoped:
                query_sql += " LIMIT ?"
                parameters.append(limit)
            cursor.execute(query_sql, parameters)
        except sqlite3.OperationalError:
            return []

        results = [
            KeywordSearchResult(
                doc_id=row[0],
                content=row[1],
                metadata=json.loads(row[2]),
                score=abs(float(row[3])),
            )
            for row in cursor.fetchall()
        ]
        if project_id is not None:
            results = [
                result
                for result in results
                if str(result.metadata.get("project_id") or "") == str(project_id)
            ]
        elif global_only:
            results = [
                result
                for result in results
                if not str(result.metadata.get("project_id") or "").strip()
            ]
        if document_names:
            results = [
                result
                for result in results
                if _document_name_matches(result.metadata, document_names)
            ]
        if document_ids:
            results = [
                result
                for result in results
                if _document_id_matches(result.metadata, document_ids)
            ]
        return results[:max(0, int(limit))]

    @_synchronized
    def close(self):
        self.conn.close()

    def _build_match_query(self, query: str):
        tokens = list(dict.fromkeys(lexical_tokens(query)))
        if not tokens:
            return ""
        return " OR ".join(f'"{token}"' for token in tokens[:48])


def lexical_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer((text or "").lower()):
        value = match.group(0)
        if not value:
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]+", value):
            tokens.append(value)
            continue

        if len(value) == 1:
            tokens.append(value)
            continue
        if len(value) <= 8:
            tokens.append(value)
        for size in (3, 2):
            if len(value) < size:
                continue
            tokens.extend(value[index : index + size] for index in range(len(value) - size + 1))
    return tokens


def _document_name_matches(metadata: dict[str, Any], document_names: list[str]) -> bool:
    if not document_names:
        return True
    expected = {
        " ".join(str(item or "").strip().split()).casefold()
        for item in document_names
        if " ".join(str(item or "").strip().split())
    }
    identities = set()
    for key in ("source_filename", "document_name", "catalog_file_ref"):
        value = " ".join(str((metadata or {}).get(key) or "").strip().split())
        if value:
            identities.add(value.casefold())
            identities.add(value.replace("\\", "/").rsplit("/", 1)[-1].casefold())
    return bool(identities & expected)


def _document_id_candidates(document_id: str) -> set[str]:
    normalized = str(document_id or "").strip()
    if not normalized:
        return set()
    if normalized.startswith("secure_"):
        return {normalized, normalized.removeprefix("secure_")}
    return {normalized, f"secure_{normalized}"}


def _document_id_matches(metadata: dict[str, Any], document_ids: list[str]) -> bool:
    if not document_ids:
        return True
    metadata_id = str((metadata or {}).get("document_id") or "").strip()
    if not metadata_id:
        return False
    expected = set()
    for document_id in document_ids:
        expected.update(_document_id_candidates(document_id))
    return bool(_document_id_candidates(metadata_id) & expected)
