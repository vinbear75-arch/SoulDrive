# GraphRAG 旧项目索引范围元数据 fail-closed 优化

## 结论

本轮修复了一个比“诊断是否有 provider”更隐蔽的兼容缺口：旧的项目级 GraphRAG payload 可能带有 `project_id` 和源快照指纹，却没有 `source_scope_digest` 或 `scope_metadata_required`。旧实现会因为 payload 可读而继续检索，无法证明其中的 chunk 仍属于当前项目。

现在项目级索引必须同时具备可重验证的范围元数据，并由当前工作区提供 chunk 项目归属；缺少任一条件时，检索和诊断均返回 `graphrag_scope_metadata_unverified`，不会静默退回 `global_shared`。明确的全局兼容索引仍保留既有兼容路径。

## 外部实践与本地映射

- [Graphiti 的搜索过滤实现](https://github.com/getzep/graphiti/blob/main/graphiti_core/search/search_utils.py)把 `group_ids` 作为节点、边、全文和向量检索的显式范围输入；范围不是结果生成后的标签。
- [Graphiti 的 group/episode 隔离入口](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)在未明确传入范围时使用配置得到的有效范围，说明默认范围也必须是可解释的契约。
- [Microsoft GraphRAG 的默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md)以 Document/TextUnit provenance 支撑派生索引；本地对应地把项目级 chunk 归属指纹视为索引可用性的必要条件。

## 失败优先与修复

- 新增回归先构建项目 A 的带范围指纹索引，再移除 `source_scope_digest` 与 `scope_metadata_required`，旧实现会错误返回 `ready=true` 并继续生成检索结果。
- `GraphRAGIndex._load_checked()` 现在对缺少源摘要或范围摘要的项目 payload fail-closed；即使 payload 有社区内容，也不再当作已验证的项目结果使用。
- 项目索引范围未验证时，`_load_for_project()` 不再用 `global_shared` 掩盖错误；`retrieve_project_and_global()` 也不把项目失败改写成看似成功的组合结果。
- 全局-only 索引的历史兼容行为没有扩大：只有明确的全局语义才保留兼容读取，未验证状态仍通过诊断字段暴露。

## 已验证

- GraphRAG 专项：`33 passed`。
- GraphRAG、项目诊断、MCP 安全/索引与流水线诊断组合：`89 passed, 1 warning`。
- `python -m compileall -q core scripts tests` 与 `git diff --check` 通过。
- 完整 `scripts/check.ps1` 通过：后端 `1205 passed, 1 warning`；前端生产构建通过，7 组脚本测试共 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 该改动证明旧项目索引不会在缺少范围证明时继续回答，不证明 GraphRAG 社区摘要质量、召回率、答案准确率或延迟收益。
- 该改动不引入 Graphiti、Microsoft GraphRAG 或新的运行时依赖，也不把全局-only 兼容索引误判成项目索引。
- 真实 Tauri/UI/E2E、打包启动、第三方图后端内部过滤诚实性仍需独立验证。
