# GraphRAG 项目范围新鲜度优化记录

日期：2026-09-10

## 结论

本轮修复了一个由代码路径直接证明的范围一致性缺口：社区索引构建时会读取 `LocalKnowledgeBase.metadata_for_chunk_ids()` 得到切片所属项目，但此前 `GraphRAGIndex` 只重新核验图数据库快照。若切片项目归属发生变化而图快照未变化，旧的项目社区索引可能继续返回，导致项目范围与派生索引脱节。

本轮没有引入 Neo4j、后台监听器或新的数据库。实现只增加确定性 `source_scope_digest`、可选的当前切片范围元数据提供器和可诊断的新鲜度状态；范围元数据不可读取或指纹不一致时，项目 GraphRAG 不再继续使用旧索引。

## 外部实践对照

- Microsoft GraphRAG 将输入文档的稳定标识、增量输入和派生输出分层管理；这支持本项目把“图快照”和“切片项目归属”视作两个必须共同绑定的输入，而不是只看社区输出文件是否存在。[GraphRAG 输入](https://microsoft.github.io/graphrag//index/inputs/)；[GraphRAG 输出](https://microsoft.github.io/graphrag//index/outputs/)
- Dagster 的 asset checks 将数据质量/新鲜度作为下游执行的阻断条件，并把检查结果纳入可观察状态；本项目采用同样的轻量思想，只在读取 GraphRAG 派生层时做同步检查，不建设调度系统。[Dagster asset checks](https://docs.dagster.io/concepts/assets/asset-checks)
- Graphiti 和 Qdrant 的现有对照已用于上一轮原始图快照指纹；本轮把同一“派生索引必须能判断是否过期”的边界扩展到外部范围元数据，不把缓存命中误认为内容仍然有效。

## 实现

- `GraphRAGBuilder` 继续保存原始 `source_snapshot_digest`，同时保存由图快照、规范化 `chunk_project_ids`、`project_id` 和 `global_only` 组成的 `source_scope_digest`。
- 新索引标记 `scope_metadata_required` 与 `global_only`，避免把没有外部范围输入的旧/默认索引误判为必须回查外部元数据。
- `GraphRAGIndex` 接受可选 `scope_metadata_provider`。真实 `RAGEngine` 将其绑定到 `LocalKnowledgeBase.metadata_for_chunk_ids()`；测试或只读诊断调用若未提供该回调，仍可读取索引，但诊断明确标为 `scope_freshness=unverified`。
- 当前范围元数据不可读取时返回 `graphrag_scope_metadata_unavailable`；当前范围指纹不一致时返回 `graphrag_index_stale`。项目索引的 global/shared 回退仍需通过自己的新鲜度检查，不会绕过该门。
- GraphRAG 返回结果和 RAG observability 现在同时暴露 `source_freshness` 与 `scope_freshness`，可以区分图源过期、项目范围过期和未验证兼容路径。

## 失败优先回归

新增 `test_graph_index_refuses_stale_project_scope_after_chunk_metadata_changes`：先构建 `project-a` 的社区索引，再只把一个切片的当前归属改成 `project-b`，保持图数据库快照字节不变；读取 `project-a` 时必须返回 `graphrag_index_stale`、空社区结果和 `scope_freshness=stale`。

当前验证：

- `tests/test_graph_rag.py`：`24 passed`；
- GraphRAG/图存储/加密图存储/项目诊断/RAG 提示词组合：`138 passed, 1 warning`；
- 代码改动后的仓库全量 `scripts/check.ps1` 已完成：后端 `1120 passed, 1 warning`；前端构建成功、7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 该门只验证索引构建所依赖的切片项目元数据，不等于已经验证所有业务答案正确性；检索指标和范围新鲜度也不等于答案准确率。
- 未引入自动后台重建。发现 stale 后由现有索引流程重建，期间系统拒绝使用陈旧社区摘要并保留原文混合检索的既有边界。
- 未把没有 `scope_metadata_provider` 的旧调用者强制改成失败；这保持兼容，但明确暴露 `unverified`，真实聊天路径已经绑定提供器。
