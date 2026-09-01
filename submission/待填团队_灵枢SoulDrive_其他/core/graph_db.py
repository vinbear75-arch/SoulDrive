import json
import os
import sqlite3
import time
from core.logging_config import get_logger

logger = get_logger(__name__)

class LocalGraphDB:
    """
    轻量级本地图数据库管理器
    利用关系型数据库（SQLite）的表结构来模拟图数据库（如 Neo4j）的“节点(Entity)”与“边(Relationship)”。
    适用于本地轻量级 RAG（检索增强生成）系统，避免引入庞大的外部图数据库组件。
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            raise ValueError("db_path is required for LocalGraphDB")
        self.db_path = db_path
        logger.info("[GraphDB] 正在挂载本地轻量级图数据库: %s", os.path.basename(self.db_path))

        # 确保数据库存放的父级目录存在，如果不存在则自动创建 (exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # 连接 SQLite 数据库
        # check_same_thread=False: 允许在不同线程中共享此数据库连接。
        # 在大模型/Web应用中，常有异步并发请求，此参数可防止多线程环境下的 SQLite 报错
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)

        # 建立连接后，立即初始化表结构（如果表已存在则不会重复创建）
        self._init_schema()

    def _init_schema(self):
        """
        核心方法：工业级图谱建表
        将图数据抽象为两张核心表：Entities（节点表）和 Relationships（边/关系表）
        """
        cursor = self.conn.cursor()

        # ==========================================
        # 表1: 实体表 (Entities) -> 图谱中的“节点(Nodes)”
        # ==========================================
        # name: 实体名称（主键，确保全局唯一，例如 "Transformer"）
        # type:
        # description: 实体的详细属性/描述（可用于后续大模型的上下文补充）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                name TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                description TEXT
            )
        """)

        # ==========================================
        # 表2: 关系表 (Relationships) -> 图谱中的“边(Edges)”
        # ==========================================
        # id: 关系的唯一标识（自增主键）
        # source: 起始节点
        # target: 目标节点
        # relation: 关系的具体含义（例如 "核心组件"、"属于"）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                FOREIGN KEY (source) REFERENCES entities(name), -- 外键约束：起点必须是已存在的实体
                FOREIGN KEY (target) REFERENCES entities(name), -- 外键约束：终点必须是已存在的实体
                UNIQUE(source, target, relation)                -- 联合唯一索引：防止同一对节点之间重复插入完全相同的关系
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_chunks (
                entity_name TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                PRIMARY KEY (entity_name, chunk_id),
                FOREIGN KEY (entity_name) REFERENCES entities(name)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relationship_chunks (
                relationship_id INTEGER NOT NULL,
                chunk_id TEXT NOT NULL,
                PRIMARY KEY (relationship_id, chunk_id),
                FOREIGN KEY (relationship_id) REFERENCES relationships(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graph_documents (
                source_filename TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                chunk_ids_json TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT ''
            )
        """)
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(graph_documents)").fetchall()}
        if "project_id" not in columns:
            cursor.execute("ALTER TABLE graph_documents ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graphrag_indexes (
                index_name TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # ==========================================
        # 性能优化：建立索引 (Indexes)
        # ==========================================
        # 图数据库的核心操作是“图遍历”（例如：找出所有由 A 出发的关系，或所有指向 B 的关系）。
        # 如果不建立索引，每次查询都会全表扫描 (O(N))；建立索引后查询时间降至 O(log N)，极大提升多跳检索的速度。
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_entity_chunks_chunk ON entity_chunks(chunk_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_chunks_chunk ON relationship_chunks(chunk_id)")

        # 提交上述建表和索引操作到数据库
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
        self.conn.execute(
            """
            INSERT INTO graph_documents
                (source_filename, title, summary, keywords_json, chunk_ids_json, project_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_filename) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                keywords_json = excluded.keywords_json,
                chunk_ids_json = excluded.chunk_ids_json,
                project_id = excluded.project_id
            """,
            (
                source,
                str(title or source)[:240],
                str(summary or "")[:4000],
                json.dumps(_dedupe(keywords or []), ensure_ascii=False),
                json.dumps(_dedupe(chunk_ids or []), ensure_ascii=False),
                str(project_id or "").strip(),
            ),
        )
        self.conn.commit()

    def add_entity(self, name: str, entity_type: str, description: str, chunk_ids: list[str] | None = None):
        """
        数据写入层：插入或更新实体节点
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO entities (name, type, description)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type = excluded.type,
                description = excluded.description
        """, (name, entity_type, description))
        cursor.executemany(
            """
            INSERT OR IGNORE INTO entity_chunks (entity_name, chunk_id)
            VALUES (?, ?)
            """,
            [(name, chunk_id) for chunk_id in _dedupe(chunk_ids or [])],
        )
        self.conn.commit()

    def add_relationship(self, source: str, target: str, relation: str, evidence_chunk_ids: list[str] | None = None):
        """
        数据写入层：插入实体间的连接关系
        """
        cursor = self.conn.cursor()
        # INSERT OR IGNORE
        # 逻辑：尝试插入新的关系。因为我们在建表时设置了 UNIQUE(source, target, relation)，
        # 如果大模型重复抽取出相同的关系，数据库会自动忽略该次插入，避免产生重复的“边”。
        cursor.execute("""
            INSERT OR IGNORE INTO relationships (source, target, relation)
            VALUES (?, ?, ?)
        """, (source, target, relation))
        cursor.execute(
            """
            SELECT id
            FROM relationships
            WHERE source = ? AND target = ? AND relation = ?
            """,
            (source, target, relation),
        )
        relationship_id = cursor.fetchone()[0]
        cursor.executemany(
            """
            INSERT OR IGNORE INTO relationship_chunks (relationship_id, chunk_id)
            VALUES (?, ?)
            """,
            [(relationship_id, chunk_id) for chunk_id in _dedupe(evidence_chunk_ids or [])],
        )
        self.conn.commit()

    def get_all_entities(
        self,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        """
        检索层：返回图谱中所有的实体名。

        【核心技巧】按名称长度倒序排列 (LENGTH(name) DESC)。
        这在 NLP 和文本匹配中非常关键（称为“最大向前匹配”或“贪婪匹配”）。
        例如，如果同时存在实体 "苹果" 和 "苹果公司"，按长度倒序可以让系统优先匹配到更具体的 "苹果公司"，
        从而避免短词过早抢占检索上下文，导致实体截断错误。
        """
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT name
            FROM entities
            -- 优先按长度降序排列，如果长度相同则按字母顺序升序排列保证结果稳定
            ORDER BY LENGTH(name) DESC, name ASC
        """)
        # 提取每一行的第一列（实体名），生成一个平铺的列表并返回
        return [
            row[0]
            for row in cursor.fetchall()
            if allowed_chunk_ids is None
            or set(self._entity_chunk_ids(row[0])) & allowed_chunk_ids
        ]

    def get_related_entities(
        self,
        entity_name: str,
        limit: int = 20,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        """
        检索层：返回与指定实体直接相连的一跳关系（即相邻边）。

        返回格式固定为标准的知识图谱三元组 (source, relation, target)，
        供上层系统（如 LocalKnowledgeBase）直接拼装成 LLM 容易理解的自然语言上下文。
        """
        cursor = self.conn.cursor()
        query = """
            SELECT source, relation, target
            FROM relationships
            -- 关键点：图谱关系是有向的，但在检索时通常需要“双向查询”。
            -- 无论该实体是作为主语 (source) 还是宾语 (target)，只要参与了关系就提取出来。
            WHERE source = ? OR target = ?
            -- 排序保证每次查询结果的一致性
            ORDER BY source ASC, relation ASC, target ASC
        """
        parameters = [entity_name, entity_name]
        if project_id is None and not global_only:
            query += " LIMIT ?"
            parameters.append(limit)
        cursor.execute(query, parameters)
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)
        return [
            item
            for item in cursor.fetchall()
            if allowed_chunk_ids is None
            or set(self._relationship_chunk_ids(*item)) & allowed_chunk_ids
        ][:limit]

    def get_subgraph(
        self,
        entity_name: str,
        depth: int = 1,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        """
        检索层：图谱核心算法 —— 多跳检索 (Multi-hop Retrieval)

        概念解析：
        - 1跳 (depth=1)：直接与中心实体相连的节点（朋友）。
        - N跳 (depth=N)：通过中间节点间接连接的实体（朋友的朋友）。
        用途：在 RAG 场景中，当用户提问时，抓取实体周围的知识网络作为上下文，
              赋予大模型进行“逻辑链条推理”和“关联发散思维”的能力。
        """
        # 防御性编程：深度小于 1 没有意义，直接返回空列表
        if depth < 1:
            return []

        # 【全局截断保护】整个子图最多只提取 50 条边，严格控制 LLM 的 Prompt 长度
        max_edges = 50

        # 记录已访问过的实体，防止在环状图中陷入死循环（比如 A->B, B->A）
        visited_entities = {entity_name}
        # 记录已提取过的边，用于最终结果的去重
        visited_edges = set()
        # 存放最终返回的三元组列表
        results = []

        # 广度优先搜索 (BFS) 的队列：存储待处理的节点及其所处的“跳数”层级
        # 初始状态：将中心实体放入队列，当前深度为 0
        frontier = [(entity_name, 0)]

        # BFS 主循环：只要队列不为空，且提取的边还没达到 50 条上限，就继续探索
        while frontier and len(results) < max_edges:
            # 弹出队列的第一个元素（先进先出）
            current_entity, current_depth = frontier.pop(0)

            # 如果当前节点已经达到了用户要求的最大深度，则不再继续向下探索它的邻居
            if current_depth >= depth:
                continue

            # 调用刚才定义的单跳查询方法，获取当前实体的所有相连关系
            for source, relation, target in self.get_related_entities(
                current_entity,
                project_id=project_id,
                global_only=global_only,
            ):
                # 将三元组打包作为唯一标识
                edge_key = (source, relation, target)

                # 如果这条边没有被处理过，就加入到结果集中
                if edge_key not in visited_edges:
                    visited_edges.add(edge_key)
                    results.append(edge_key)

                # 将这条边的两端节点（source 和 target）都进行检查
                for next_entity in (source, target):
                    # 如果发现了一个从未见过的全新实体
                    if next_entity not in visited_entities:
                        # 标记为已访问
                        visited_entities.add(next_entity)
                        # 将这个新实体推入队列，深度加 1，留作下一轮循环探索
                        frontier.append((next_entity, current_depth + 1))

                # 每提取一条边后，立刻检查是否达到了全局上限，防止多探索
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
        """
        面向 RAG 的图谱上下文接口：从自然语言查询中匹配实体，再返回可直接注入 Prompt 的关系链。
        该方法是后续替换 Kuzu/Neo4j 时最小稳定接口。
        """
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
                graph_context.append(f"已知逻辑关系: [{source}] --({relation})--> [{target}]")
                if len(graph_context) >= max_edges:
                    return graph_context

        return graph_context

    def recall_chunk_ids(
        self,
        query: str,
        limit: int = 40,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        if limit <= 0:
            return []

        match_space = (query or "").lower()
        matched_entities = [
            entity
            for entity in self.get_all_entities(
                project_id=project_id,
                global_only=global_only,
            )
            if entity and entity.lower() in match_space
        ]

        chunk_ids = []
        seen = set()
        allowed_chunk_ids = self._scope_chunk_ids(project_id, global_only)

        def add_chunk_id(chunk_id):
            if allowed_chunk_ids is not None and chunk_id not in allowed_chunk_ids:
                return False
            if chunk_id in seen:
                return False
            seen.add(chunk_id)
            chunk_ids.append(chunk_id)
            return True

        for entity_name in matched_entities:
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
        entities = []
        for name, entity_type, description in self.conn.execute(
            "SELECT name, type, description FROM entities ORDER BY name ASC"
        ).fetchall():
            entities.append({
                "name": name,
                "type": entity_type,
                "description": description or "",
                "chunk_ids": self._entity_chunk_ids(name),
            })

        relationships = []
        for source, relation, target in self.conn.execute(
            "SELECT source, relation, target FROM relationships ORDER BY source, relation, target"
        ).fetchall():
            relationships.append({
                "source": source,
                "relation": relation,
                "target": target,
                "chunk_ids": self._relationship_chunk_ids(source, relation, target),
            })

        documents = []
        for source, title, summary, keywords_json, chunk_ids_json, project_id in self.conn.execute(
            """
            SELECT source_filename, title, summary, keywords_json, chunk_ids_json, project_id
            FROM graph_documents
            ORDER BY source_filename ASC
            """
        ).fetchall():
            documents.append({
                "source_filename": source,
                "title": title,
                "summary": summary,
                "keywords": _json_list(keywords_json),
                "chunk_ids": _json_list(chunk_ids_json),
                "project_id": project_id or "",
            })
        return {
            "entities": entities,
            "relationships": relationships,
            "documents": documents,
        }

    def save_graphrag_index(self, payload: dict, index_name: str = "default") -> None:
        index_name = str(index_name or "default").strip() or "default"
        self.conn.execute(
            """
            INSERT INTO graphrag_indexes (index_name, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(index_name) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (index_name, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def load_graphrag_index(self, index_name: str = "default") -> dict | None:
        index_name = str(index_name or "default").strip() or "default"
        row = self.conn.execute(
            "SELECT payload_json FROM graphrag_indexes WHERE index_name = ?",
            (index_name,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def remove_chunk_ids(self, chunk_ids: list[str]) -> None:
        removed = _dedupe(chunk_ids)
        if not removed:
            return

        affected_entities = set()
        affected_relationships = set()
        cursor = self.conn.cursor()
        for batch in _batches(removed, 500):
            placeholders = ",".join("?" for _ in batch)
            cursor.execute(
                f"SELECT DISTINCT entity_name FROM entity_chunks WHERE chunk_id IN ({placeholders})",
                batch,
            )
            affected_entities.update(row[0] for row in cursor.fetchall())
            cursor.execute(
                f"SELECT DISTINCT relationship_id FROM relationship_chunks WHERE chunk_id IN ({placeholders})",
                batch,
            )
            affected_relationships.update(row[0] for row in cursor.fetchall())
            cursor.execute(
                f"DELETE FROM entity_chunks WHERE chunk_id IN ({placeholders})",
                batch,
            )
            cursor.execute(
                f"DELETE FROM relationship_chunks WHERE chunk_id IN ({placeholders})",
                batch,
            )

        for source, chunk_ids_json in cursor.execute(
            "SELECT source_filename, chunk_ids_json FROM graph_documents"
        ).fetchall():
            kept = [item for item in _json_list(chunk_ids_json) if item not in removed]
            if kept:
                cursor.execute(
                    "UPDATE graph_documents SET chunk_ids_json = ? WHERE source_filename = ?",
                    (json.dumps(kept, ensure_ascii=False), source),
                )
            else:
                cursor.execute(
                    "DELETE FROM graph_documents WHERE source_filename = ?",
                    (source,),
                )

        cursor.executemany(
            """
            DELETE FROM relationships
            WHERE id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM relationship_chunks
                  WHERE relationship_id = relationships.id
              )
            """,
            [(relationship_id,) for relationship_id in affected_relationships],
        )
        cursor.executemany(
            """
            DELETE FROM entities
            WHERE name = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM entity_chunks
                  WHERE entity_name = entities.name
              )
            """,
            [(entity_name,) for entity_name in affected_entities],
        )
        # 删除原始切片后，社区报告和 chunk 指针都可能过期；下次索引会重建派生层。
        cursor.execute("DELETE FROM graphrag_indexes")
        self.conn.commit()

    def remove_project(self, project_id: str, chunk_ids: list[str] | None = None) -> None:
        project_id = str(project_id or "").strip()
        if not project_id:
            return
        self.remove_chunk_ids(chunk_ids or [])
        self.conn.execute(
            "DELETE FROM graph_documents WHERE project_id = ?",
            (project_id,),
        )
        # Global and project GraphRAG reports are derived from the same graph;
        # deleting a project invalidates every report until the next rebuild.
        self.conn.execute("DELETE FROM graphrag_indexes")
        self.conn.commit()

    def _entity_chunk_ids(self, name: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT chunk_id
            FROM entity_chunks
            WHERE entity_name = ?
            ORDER BY chunk_id ASC
            """,
            (name,),
        ).fetchall()
        return [row[0] for row in rows]

    def _scope_chunk_ids(
        self,
        project_id: str | None,
        global_only: bool = False,
    ) -> set[str] | None:
        project_id = str(project_id or "").strip()
        if project_id:
            query = "SELECT chunk_ids_json FROM graph_documents WHERE project_id = ?"
            parameters = (project_id,)
        elif global_only:
            query = "SELECT chunk_ids_json FROM graph_documents WHERE project_id = ''"
            parameters = ()
        else:
            return None
        rows = self.conn.execute(query, parameters).fetchall()
        return {
            chunk_id
            for row in rows
            for chunk_id in _json_list(row[0])
        }

    def _relationship_chunk_ids(self, source: str, relation: str, target: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT relationship_chunks.chunk_id
            FROM relationship_chunks
            JOIN relationships
              ON relationships.id = relationship_chunks.relationship_id
            WHERE relationships.source = ?
              AND relationships.relation = ?
              AND relationships.target = ?
            ORDER BY relationship_chunks.chunk_id ASC
            """,
            (source, relation, target),
        ).fetchall()
        return [row[0] for row in rows]

    def close(self):
        """释放资源，安全关闭数据库连接，防止连接池泄漏"""
        self.conn.close()


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


def _batches(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _json_list(value: str | None) -> list[str]:
    try:
        payload = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return _dedupe(payload if isinstance(payload, list) else [])
# ---------------------------------------------------------
# 简易测试代码 (仅在直接运行此脚本时执行)
# ---------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m core.graph_db <workspace-graph-sqlite-path>")

    # 1. 实例化图数据库对象（自动完成目录创建与建表）
    db = LocalGraphDB(db_path=sys.argv[1])

    # 2. 模拟 LLM (大模型) 解析文本后提取出的知识节点
    db.add_entity("Transformer", "Architecture", "基于自注意力机制的模型")
    db.add_entity("Self-Attention", "Mechanism", "允许模型关注序列不同部分")

    # 3. 建立节点之间的语义关系（Transformer 包含 Self-Attention）
    db.add_relationship("Transformer", "Self-Attention", "has_component")

    logger.info("[GraphDB] 图谱引擎挂载并测试成功，实体与关系已就绪！")
