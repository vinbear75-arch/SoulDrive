# RAG 同名资产来源多样性身份优化

日期：2026-09-12

## 结论

本轮修复了 RAG 上下文门对来源多样性的身份误判。检索层已经使用稳定的文档/资产身份做候选去重，但 `admit_context` 和纠错重试保护曾只按展示用 `source_filename` 计数。两个不同项目、不同文档或不同资产版本都叫 `report.pdf` 时，它们会被当成一个来源，比较/研究窗口可能因此反复重试或拒答。

现在引入本地轻量 `source_diversity_key`：优先使用 `asset_version_id`、`document_id`、`asset_id`，并加上 `project_id` scope；缺少稳定 ID 时才回退到显示来源名。显示给用户的文件名、citation ID 和项目范围没有改变，只修正来源多样性门和重试保留门的内部身份。

## 外部实践对照

- [Microsoft GraphRAG 输入 schema](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 为文档生成稳定的文本哈希 ID；[默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 再把 TextUnit 链回 Document，强调检索片段与源文档之间的稳定 provenance，而不是只依赖展示标题。
- [LlamaIndex citation query engine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/citation_query_engine.py) 将编号 source nodes 作为回答引用输入；[Node schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 区分 node ID 与 source/ref document 关系。
- 本轮只把上述“稳定来源身份与展示标签分离”原则落到现有 SoulDrive gate，不引入新的检索框架。

## 失败优先验证

- 新增 `ContextGateTests.test_context_gate_counts_same_named_project_assets_as_distinct_sources`。
- 修复前：两个项目的 `report.pdf` 虽然有不同 `document_id`，`source_count` 仍为 1，比较门返回 `retry`，测试失败。
- 新增 `RagPromptingTests.test_retry_source_diversity_distinguishes_same_named_assets_in_one_project`。
- 修复前：同一项目下两个不同文档都叫 `report.pdf` 时，纠错重试只保留其中一个仍被误判为保持覆盖；修复后按稳定文档 ID正确返回 `False`。

## 实现与验证

- 在 `core/retrieval.py` 增加 `source_diversity_key`；`core/context_gate.py` 和 `core/rag_engine.py` 统一使用它进行来源计数与重试覆盖判断。
- RAG/GraphRAG/上下文门/检索组合回归：`182 passed`；GraphRAG 观测来源计数的补充修复见 `docs/reports/2026-09-12-graphrag-source-observability-identity-optimization.md`。
- `scripts/check.ps1`：后端 `1148 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。
- 警告为环境中的 Starlette/httpx 弃用提示，不是测试失败。

## 边界

- 这只修正来源身份和来源覆盖门，不证明召回率、引用正确率或答案准确率提升。
- `source_diversity_key` 不改变原始资产、向量索引或用户可见文件名；没有做跨项目合并或自动 dedup。
- 缺少 `asset_version_id`、`document_id` 和 `asset_id` 的历史索引仍按展示来源名回退，属于兼容边界；应在后续索引重建时补齐稳定元数据。
- 真实模型、冻结业务留出集、Redis/向量服务、Tauri/WPS、安装包和跨存储 exactly-once 不由本轮自动检查证明。
