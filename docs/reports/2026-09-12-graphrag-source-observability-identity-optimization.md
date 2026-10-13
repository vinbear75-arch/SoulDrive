# GraphRAG 来源计数与稳定身份观测优化

日期：2026-09-12

## 结论

本轮继续收敛上一轮的来源身份修复，补上 GraphRAG 观测层的遗漏。来源多样性门和纠错重试已经按稳定资产身份计数，但 `observability.graph_rag.source_count` 仍只按 `source_filename` 聚合；同名跨项目资产会导致日志显示 1 个来源，和实际证据门的 2 个来源不一致。

现在 GraphRAG 的来源计数复用 `source_diversity_key`，让功能门、纠错保护和观测数据使用同一身份定义。该改动只修正诊断一致性，不放宽证据或项目范围校验。

## 外部实践对照

- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 TextUnit 回链到 Document 作为 provenance，并把来源引用作为应用层可追溯数据。
- [Microsoft GraphRAG 输入 schema](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 为输入文档保存稳定 ID，而不是用展示标题承担唯一身份。
- [LlamaIndex Node schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 将 node ID 与 source node/reference 关系分开。本轮只把稳定来源身份复用到 GraphRAG 观测，不引入新的图数据库或追踪框架。

## 失败优先验证

- 新增 `GraphRagTests.test_graph_observability_counts_same_named_scoped_sources_by_identity`。
- 修复前：GraphRAG 返回两个不同 `document_id`、不同 `project_id` 但同名 `report.pdf`，`source_count` 为 1，测试失败。
- 修复后：观测 `source_count` 为 2，并与上下文门的来源身份定义一致。

## 实现与验证

- 修改 `core/rag_engine.py::_apply_graph_rag_route`，GraphRAG source count 改用 `source_diversity_key`。
- GraphRAG 专项回归：`25 passed`；RAG/GraphRAG/上下文门/检索组合：`182 passed`。
- `scripts/check.ps1`：后端 `1148 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。
- 警告为环境中的 Starlette/httpx 弃用提示，不是测试失败。

## 边界

- 这是观测计数一致性修补，不证明 GraphRAG 召回、图路径贡献、引用正确率或答案准确率提升。
- 观测 source count 仍只描述已回读的原始证据，不把社区摘要或图关系数量冒充文档来源数。
- 缺少稳定资产/文档 ID 的历史索引仍按来源名回退；真实模型、冻结留出集、Tauri/WPS、安装包和跨存储 exactly-once 不由本轮自动检查证明。
