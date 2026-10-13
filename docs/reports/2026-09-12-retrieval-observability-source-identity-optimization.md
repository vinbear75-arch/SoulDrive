# 检索质量观测来源身份统一优化

日期：2026-09-12

## 结论

本轮修复了检索观测层与功能门的来源身份不一致：`context_gate`、纠错重试和 GraphRAG `source_count` 已按稳定资产/文档身份计数，但 `LocalKnowledgeBase` 的 `quality_signals.evidence_source_count` 与 `stage_counts.final_sources` 仍按展示文件名计数。现在这两处也复用 `source_diversity_key`，优先使用 `asset_version_id`、`document_id`、`asset_id` 并附加项目 scope，缺少稳定 ID 时才回退展示名称。

这只改变内部质量信号和阶段计数，不改变用户可见的 `source_filename`、引用编号、召回排序或项目过滤规则。

## 外部实践对照

- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 Document、TextUnit 与知识条目的 source-reference 链路分开，明确 provenance 要能回到原始文本。
- [Microsoft GraphRAG 输入说明](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 将输入文档的 `id` 作为稳定字段，并在切片时保留可追踪的来源元数据。
- [LlamaIndex BaseNode schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 区分节点唯一 ID、source node 和展示 metadata；[CitationQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/citation_query_engine.py) 将来源节点组织成可引用证据。

本地映射不是引入上述框架，而是沿用“稳定身份用于追踪/计数，展示名称用于人读”的最小原则。

## 失败优先与实现

1. 新增 `tests/test_retrieval.py::RetrievalTests::test_quality_signals_count_same_named_scoped_sources_by_identity`，先用两个项目的同名 `report.pdf` 复现旧结果：`evidence_source_count` 为 `1`，而预期为 `2`。
2. `core/knowledge_base.py::_retrieval_quality_signals()` 改用 `source_diversity_key()`，避免不同 scope 的同名资产被合并。
3. `core/knowledge_base.py::search_with_evidence()` 的 `stage_counts.final_sources` 同样改用稳定身份。
4. 新增 `tests/test_secure_retrieval_flow.py::SecureRetrievalFlowTests::test_retrieval_observability_counts_same_named_scoped_sources_by_identity`，通过真实 `LocalKnowledgeBase.search_with_evidence()` 同时验证 `quality_signals.evidence_source_count` 和 `stage_counts.final_sources` 均为 `2`。

## 验证证据

- 检索、上下文门、提示词、GraphRAG、项目范围链路：`184 passed`。
- `scripts/check.ps1`：后端 `1150 passed, 1 warning`，Python compileall 通过。
- 前端生产构建通过，7 组前端测试共 `48 passed`。
- Tauri `cargo check --locked` 通过。
- `git diff --check` 通过；仅保留已有的 Windows LF/CRLF 转换提示，无 diff 空白错误。

## 边界

本轮证明的是同名来源在检索质量观测中的计数与功能门一致，不证明 Recall、MRR、引用正确率、答案准确率、真实模型链路、跨进程 exactly-once 或真实 Tauri/WPS/UI 交付。缺少稳定资产/文档 ID 的历史索引仍会回退到展示名称，后续如需消除回退必须先完成索引 schema 迁移与冻结评测。
