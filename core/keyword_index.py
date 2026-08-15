import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
KEYWORD_TABLE = "keyword_documents_v2"


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
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
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

    def upsert_document(self, doc_id: str, content: str, metadata: dict[str, Any]):
        cursor = self.conn.cursor()
        search_text = " ".join(lexical_tokens(content))
        cursor.execute(f"DELETE FROM {KEYWORD_TABLE} WHERE doc_id = ?", (doc_id,))
        cursor.execute(f"""
            INSERT INTO {KEYWORD_TABLE} (doc_id, content, search_text, metadata_json)
            VALUES (?, ?, ?, ?)
        """, (doc_id, content, search_text, json.dumps(metadata, ensure_ascii=False)))
        self.conn.commit()

    def delete_by_document_hash(self, document_hash: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            DELETE FROM keyword_documents_v2
            WHERE metadata_json LIKE ?
        """, (f'%"{document_hash}"%',))
        self.conn.commit()

    def delete(self, doc_ids: list[str]):
        if not doc_ids:
            return
        placeholders = ",".join("?" for _ in doc_ids)
        self.conn.execute(
            f"DELETE FROM {KEYWORD_TABLE} WHERE doc_id IN ({placeholders})",
            tuple(doc_ids),
        )
        self.conn.commit()

    def search(self, query: str, limit: int = 12) -> list[KeywordSearchResult]:
        match_query = self._build_match_query(query)
        if not match_query:
            return []

        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT doc_id, content, metadata_json,
                       bm25(keyword_documents_v2, 0.0, 0.0, 1.0, 0.0) AS score
                FROM keyword_documents_v2
                WHERE search_text MATCH ?
                ORDER BY score ASC
                LIMIT ?
            """, (match_query, limit))
        except sqlite3.OperationalError:
            return []

        return [
            KeywordSearchResult(
                doc_id=row[0],
                content=row[1],
                metadata=json.loads(row[2]),
                score=abs(float(row[3])),
            )
            for row in cursor.fetchall()
        ]

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
