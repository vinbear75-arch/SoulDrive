# GraphRAG 诊断范围复核与 fail-closed 优化

## 结论

本轮修复了 GraphRAG 运行时与诊断接口之间的语义分歧：运行时 `RAGEngine` 已使用 chunk 项目元数据复核派生图索引，但独立诊断接口此前只创建 `GraphRAGIndex(graph_store)`，对带范围指纹的索引没有提供元数据校验器。结果是索引文件虽然可读，`scope_freshness` 已是 `unverified`，诊断仍可能返回 `ready=true`。

现在，带 `scope_metadata_required` 的索引在缺少范围元数据提供器时只能返回 `status=unverified`、`ready=false` 和结构化原因；同一工作区的知识库已经加载时，诊断会复用其 chunk 元数据提供器完成复核，未加载时不会为了诊断主动加载 embedding、聊天模型或知识库。

## 外部实践与本地映射

- [Graphiti 的搜索过滤实现](https://github.com/getzep/graphiti/blob/main/graphiti_core/search/search_utils.py)在节点、边、全文、向量和 BFS 搜索中显式携带 `group_ids`；范围不是搜索后的展示字段，而是查询契约的一部分。
- [Graphiti MCP server 的 group_id 处理](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)在未显式传入范围时使用配置的有效 group，而不是把未验证的混合结果宣称为已隔离结果。
- [Microsoft GraphRAG 的默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md)将 Document/TextUnit provenance 贯穿派生索引；本地对应地把 chunk 项目归属指纹视为索引可用性的一部分，而不是仅用于调试展示。

## 失败优先与修复

- 新增回归先构建一个带 `source_scope_digest` 和 `scope_metadata_required=true` 的全局图索引，再用没有 `scope_metadata_provider` 的 `GraphRAGIndex` 诊断；旧实现会返回 `ready=true`，但 `scope_freshness=unverified`。
- `core/graph_rag.py::GraphRAGIndex.diagnostics()` 现在把范围未验证显式映射为 `status=unverified`、`ready=false` 和 `graphrag_scope_metadata_unverified`。
- `core/mcp_server.py::diagnostics_graphrag()` 仅在全局知识库已属于当前工作区时复用 `metadata_for_chunk_ids`；没有提供器时保持未验证，不调用 `ensure_knowledge_base_loaded()`，避免诊断引入模型/索引副作用。
- API 诊断状态分支保留 `unverified`，不会在索引观测为 completed 时把它覆盖成 `not_built`，也不会把范围未复核结果冒充 `ready`。

## 已验证

- GraphRAG 与项目诊断定向回归：`88 passed, 1 warning`（包含 GraphRAG 32 项和项目/MCP 诊断 21 项）。
- `python -m compileall -q core tests scripts` 通过；`git diff --check` 通过。
- 全量 `scripts/check.ps1` 通过：后端 `1204 passed, 1 warning`；前端生产构建通过，7 组测试共 `49 passed`；Tauri `cargo check --locked` 通过。前端仍只有既有的单个 JavaScript chunk 体积提示。

## 边界

- 该改动证明的是“诊断不会把未复核的范围索引标为可用”，不证明 GraphRAG 社区摘要质量、召回率、答案准确率或延迟收益。
- 知识库尚未加载时，诊断会诚实返回 `unverified`，不会自动加载模型来改变结果；要得到 `ready`，必须由同一工作区的已加载知识库提供当前 chunk 范围元数据。
- 该项目仍是本机单用户模式；没有引入 Graphiti、Microsoft GraphRAG 或新的运行时依赖。
