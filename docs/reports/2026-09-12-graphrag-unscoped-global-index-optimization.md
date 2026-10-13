# GraphRAG 无项目请求的全局索引隔离优化

日期：2026-09-12

## 优化原则

本轮只处理一个明确的项目边界缺口：未绑定 `project_id` 的会话只能读取显式的全局知识，不能读取由多个项目混合构建的图索引。没有把这项修复扩展为新的 Agent 编排框架或新的图数据库依赖。

## 公开实践对照

- [Microsoft GraphRAG default dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 Document、TextUnit、Entity、Relationship 与来源 provenance 连接起来，图摘要不是脱离来源范围的全局缓存。
- [Microsoft GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/) 明确展示文档、文本单元和实体/关系之间的来源字段；索引消费方需要知道结果来自哪些原始材料。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 通过 `group_id` 把 episode、entity 和关系限定在业务分组内；[Graphiti 的边操作范围问题](https://github.com/getzep/graphiti/issues/1728) 也说明省略范围会使失效/更新操作误触同组外数据。
- [Neo4j GraphRAG issue #588](https://github.com/neo4j/neo4j-graphrag-python/issues/588) 暴露了实体/关系元数据与租户范围没有自动绑定时的隔离风险。这里借鉴的是“范围必须成为数据身份/查询条件”的原则，而不是引入 Neo4j。

## 本地缺口与失败优先证据

原链路是：

`RAGEngine._apply_graph_rag_route(project_id=None)` → `GraphRAGIndex.retrieve(..., project_id=None)` → `GraphRAGIndex._load_for_project(None)` → `default`。

而 `core/indexer.py` 的旧构建路径曾把项目文档和全局文档混合写入 `default`，之后才分别生成 `global_shared` 与项目索引。因此一个没有项目绑定的会话可能从 `default` 看到某个项目的社区报告或来源文件名。

先加入回归 `test_unscoped_graph_retrieval_uses_shared_global_index_not_mixed_default`：测试快照包含项目 A、全局和项目 B 三类文档，并同时提供混合 `default` 与只含全局文档的 `global_shared`。旧实现实际返回 `index_name == "default"`，而测试要求只能返回 `global_shared`，失败与风险路径一致。

## 最小实现

1. `GraphRAGIndex._load_for_project(None)` 现在只加载 `global_shared`。
2. 只有当历史 `default` 载荷明确标记 `global_only=true` 时，才保留兼容性回退；未标记或混合的历史 `default` 不被信任。
3. 如果全局共享索引陈旧、缺失或被新鲜度检查阻断，不再绕过检查回退到 `default`。
4. `core/indexer.py` 的常规构建路径直接以 `global_only=True`、`index_name="global_shared"` 生成全局图索引，不再继续制造混合 `default`。

## 验证结果

- `tests/test_graph_rag.py`：`27 passed`。
- GraphRAG、索引器范围、加密图存储和本地图存储组合：`72 passed, 1 warning`。
- RAG 提示词、检索、上下文门组合：`145 passed`。
- 完整 `scripts/check.ps1` 已通过：后端 `1187 passed, 1 warning`；前端构建通过，7 组前端测试全部通过；Tauri `cargo check --locked` 通过。

## 边界与未完成项

- 这项修复证明的是未绑定项目时的索引选择和范围拒绝，不证明 GraphRAG 社区摘要质量、召回率、答案准确率或 Agentic RAG 价值。
- 历史 `default` 若没有明确 `global_only` 标记会被安全拒绝，即使它实际上可能只含全局数据；这是兼容性成本，用于避免凭文件内容猜测权限范围。
- 实体聚合描述的逐 chunk provenance 仍未实现；本仓库当前采用发现跨范围证据时清空描述的安全降级，详见 `2026-09-12-graphrag-entity-description-scope-optimization.md`。
- Tauri 实际启动、桌面 UI、WPS/屏幕链路和打包安装仍不由 `scripts/check.ps1` 覆盖。
