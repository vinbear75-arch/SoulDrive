# SoulDrive GraphRAG 图存储连接并发优化

日期：2026-09-15

## 外部实践对照

- [Microsoft GraphRAG local search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)：图检索是 RAG 的受控召回阶段，图索引和社区报告应作为可重建派生层，不能因为索引写入竞争而静默缺边。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与 [SQLite isolation](https://www.sqlite.org/isolation.html)：SQLite 负责事务和文件级并发，但 Python `sqlite3` 同一连接被多个线程交错使用时，应用仍需串行化连接操作。
- [Microsoft GraphRAG default dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md)：实体、关系、来源 chunk 和报告之间是分层派生链，索引阶段的不完整写入会直接影响后续 local/global 路由的证据覆盖。

## 发现的缺口

`LocalGraphDB` 和 `SecureGraphStore` 为了允许同一连接跨线程使用而设置了 `check_same_thread=False`，但没有连接级锁。失败优先让 6 个线程同时向同一个 `LocalGraphDB` 写入实体，实际出现 `sqlite3.OperationalError: cannot start a transaction within a transaction`，最终只落盘 39 个实体而不是预期的 180 个。

这会把图索引失败伪装成“图谱召回为空”，进而影响 GraphRAG local/global 回答；问题在图存储写入边界，不需要引入 Neo4j 或新的 Agentic RAG 框架。

## 最小实现

- Local/Secure 图存储都增加连接级 `threading.RLock`，用可重入装饰器保护所有公开读写操作；`get_subgraph()`、`search_context()`、`remove_project()` 等内部嵌套调用仍可安全复用同一连接。
- SQLite 连接 timeout 统一为 30 秒；锁只保护 Python 连接的线程交错，不改变图数据 schema、项目范围过滤或证据字段。
- 图索引仍是可重建派生层；本轮不把 SQLite 模拟图数据库扩展为外部图数据库，也不改变现有 hybrid/local/global 路由合同。

## 验证证据

- 失败优先：无锁版本并发写入 6×30 个实体，触发事务嵌套错误并只得到 39 个实体。
- Local/Secure 图存储并发定向：`24 passed`，包含同一连接 4 线程写入 48 个实体且无异常/丢失。
- GraphRAG、Secure GraphRAG、索引范围、安全检索、知识库组合：`147 passed`。
- 真实本地 GraphRAG acceptance 仍通过：hybrid/global/local 三类请求均 `status=pass`、无拒答、`citation_binding=true`；global/local 的 `source_evidence_count=3`、`report_count=4`。
- `scripts/check.ps1`：后端 `1611 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这解决的是同一 Python 图存储连接的线程竞争；不同进程依赖 SQLite 自身的事务/锁与 30 秒等待，不宣称跨主机或分布式 exactly-once。
- 没有证明图实体抽取质量、社区划分质量、GraphRAG 答案事实忠实度或召回率提升；本地 acceptance 是三条 query 的 smoke/链路验收，不是统计学模型评测。
- 图索引与原始文档/向量索引仍是多个存储层，跨层断电原子性、真实 PostgreSQL/Redis、真实 Tauri/WPS UI/E2E 未验证。
