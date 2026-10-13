# GraphRAG 索引范围元数据先行优化

## 结论

索引器原先先写一次未绑定 `chunk_project_ids` 的 `global_shared`，再读取 chunk 元数据并重写全局/项目索引。这既重复生成社区摘要，也让第一次派生索引写入绕过了当前 chunk 的权威项目归属。

本轮改为：先读取单一图快照和 chunk 项目元数据，再用同一快照构建 `global_shared` 与项目索引。派生索引不再在范围元数据准备前写入；如果全局构建失败，由外层 durable 索引流程报告失败，不保留一个看似成功但范围不完整的首写结果。

## 外部实践与本地映射

- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 先建立 Document→TextUnit provenance，再从 TextUnit 进行实体、关系和社区处理；本地把 chunk 项目元数据视为同一范围链上的权威输入。
- [Microsoft GraphRAG 输入模型](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 为文档提供稳定 ID，并将元数据带入切片；本地保留现有项目/资产元数据，不引入新的外部 ID 服务。
- [Graphiti 的 group_id 过滤](https://github.com/getzep/graphiti/blob/main/graphiti_core/search/search_utils.py) 要求图查询阶段带上范围过滤；本轮将同一原则前移到派生图索引首次写入前。

## 失败优先与修复

- 失败回归用图文档 `project_id` 为空、但 chunk 元数据明确属于 `project-a` 的快照，记录 GraphRAG builder 的调用顺序；旧实现的第一次 `global_shared` 构建没有 `chunk_project_ids`。
- `core/graph_rag.py::GraphRAGBuilder` 支持调用方传入权威 `source_snapshot`，保证范围元数据与派生索引使用同一快照。
- `core/indexer.py::_build_graphrag_index()` 先读取快照与 chunk 项目元数据，再首次构建全局索引；项目索引复用同一快照。
- 新增索引器调用顺序回归和 GraphRAG 单快照回归，避免只测最终 payload 而漏掉第一次写入窗口。

## 已验证

- GraphRAG、图存储、索引器范围组合：`59 passed`。
- 编译与 `git diff --check` 通过。
- 全量 `scripts/check.ps1` 已执行：后端 `1202 passed, 1 warning`；前端生产构建通过，7 组测试共 `49 passed`；Tauri `cargo check --locked` 通过。构建仅保留既有的单个 JavaScript chunk 体积提示。

## 边界

- 该改动只解决本机 GraphRAG 派生索引的范围元数据时序和快照一致性，不宣称社区摘要质量或召回率提升。
- 该项目仍是本机单用户模式；不引入分布式图构建、跨主机锁或外部 GraphRAG 运行时。
