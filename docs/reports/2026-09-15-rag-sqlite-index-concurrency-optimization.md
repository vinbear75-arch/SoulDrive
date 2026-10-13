# SoulDrive 本地 RAG SQLite 索引并发优化

日期：2026-09-15

## 外部实践对照

- [SQLite WAL](https://www.sqlite.org/wal.html)、[SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与 [SQLite isolation](https://www.sqlite.org/isolation.html)：SQLite 负责文件级事务和隔离，但 `check_same_thread=False` 只放开 Python 线程检查，不会自动把同一连接上的交错游标操作变成安全的应用级临界区。
- [Microsoft GraphRAG default dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 与 [local search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)：来源 chunk、实体/关系、父文档和检索证据都是分层派生链；底层索引若静默少写，后续 local/global 路由只能得到不完整证据。
- [Haystack SentenceWindowRetriever](https://docs.haystack.deepset.ai/docs/sentencewindowretriever)：父子上下文是召回后的受控上下文扩展，不应通过放宽项目范围或整库加载来补偿底层存储不完整。

## 发现的缺口

`SecureVectorStore`、`KeywordIndex` 和 `ParentDocumentStore` 都使用 SQLite，并允许同一连接跨线程使用，但原先没有连接级锁。失败优先在共享 `SecureVectorStore` 连接上用 4 个线程各写 20 个 chunk，复现了 `not an error` / `cannot start a transaction within a transaction`，最终只落盘 20/80 个 chunk。

这不是单纯的“检索参数不够好”：向量、关键词和父文档三层索引可能各自缺行，导致混合检索候选、父上下文和证据引用出现不一致。问题可以在本地存储边界解决，不需要引入 Neo4j、专用向量数据库或新的 Agentic RAG 框架。

## 最小实现

- 三个本地存储均增加连接级 `threading.RLock` 和可重入同步装饰器，保护公开读写操作；父文档的相邻读取可以安全嵌套 `get_parent()`。
- SQLite 连接 timeout 统一设为 30 秒；锁解决同一 Python 连接的线程交错，timeout 为同一数据库的短暂事务竞争提供等待窗口。
- 不改变现有 schema、项目/文档过滤、加密字段、混合检索排序、父子上下文合同或 GraphRAG 路由；索引仍是可重建派生层。
- 增加三条并发回归：向量 4×20 chunk、关键词 4×20 文档、父文档 4×20 parent，分别验证零异常和完整写入/读取。

## 验证证据

- RAG 存储并发定向：3 条新增回归通过；向量、关键词、父文档均无 SQLite 异常，期望的 80 条记录全部可见。
- RAG 组合定向：`41 passed`，覆盖安全向量、关键词索引、父子文档、知识库、项目过滤和索引评测。
- `scripts/check.ps1`：后端 `1614 passed, 1 warning`；前端生产构建成功；UI 测试通过；Python `compileall` 和锁定 Cargo 通过。
- 冻结评测复跑结果：memory `7/7` 场景、`8/8` 查询，recall@k `1.0`，stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；时序工作流 `5/5`；研究窗口角色 `6/6`，各项角色/准备度/检索/引用门均为 `1.0`。
- 真实本地 GraphRAG acceptance：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全为 `true`；三条 hybrid/global/local 查询均 `status=pass`、无拒答、`citation_binding=true`。
- 这些结果用于确认链路行为未被并发修复破坏，不把存储完整性直接等同于模型质量提升。

## 边界

- 本轮保证的是同一 Python 存储实例/连接的线程安全；不同进程依赖 SQLite 自身的事务/锁与 timeout，不宣称跨主机分布式 exactly-once。
- 没有因此宣称召回率、回答事实性或图谱抽取质量提升；冻结评测与三条本地 acceptance 只证明当前固定样本和链路合同仍通过。
- SQLite 向量、关键词、父文档、图谱和原始资产仍是多个存储层；跨层断电原子性、真实 PostgreSQL/Redis、真实 Tauri/WPS UI/E2E 和长历史性能仍未验证。
