# 回答质量门跨 scope 来源印证优化

日期：2026-09-12

## 结论

本轮修复 `answer_quality.has_corroborating_evidence()` 仍按 `source_filename` 判定独立来源的问题。两个项目或两个资产版本可能都叫 `report.pdf`；旧逻辑会把它们当作一个来源，使低于常规阈值但已有两份独立证据的比较问题继续 `retry`。现在 corroboration 与上下文门、纠错重试、GraphRAG 观测和知识库质量观测统一使用 `source_diversity_key`。

这只改变“证据是否来自至少两个稳定来源”的内部门控，不放宽最低分数、查询相关性、项目范围或引用校验，也不改变用户看到的文件名。

## 外部实践对照

- [Microsoft GraphRAG 输入说明](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 将稳定的文档 `id` 与展示 `title` 分开，并要求切片保留来源字段。
- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 通过 Document→TextUnit 的 source-reference 支持回溯原文 provenance。
- [LlamaIndex BaseNode schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 将 node ID、source node 和 metadata 分开；[CitationQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/citation_query_engine.py) 保留来源节点用于引用。

本地只迁移“稳定身份参与证据判定”的机制，不引入第三方框架或额外运行时依赖。

## 失败优先与实现

1. 新增 `tests/test_answer_quality.py::AnswerQualityTests::test_evidence_gate_accepts_same_named_scoped_corroborating_sources`，先复现两个项目同名 `report.pdf` 的旧结果：两个分数均达到 corroboration 最低分，但证据门仍返回 `retry`。
2. `core/answer_quality.py::has_corroborating_evidence()` 改用 `source_diversity_key()`，优先按 `asset_version_id`/`document_id`/`asset_id` 加项目 scope 区分来源，历史缺少稳定 ID 时才按展示名称回退。
3. 原有“同一来源两个切片不能算 corroboration”回归保持不变，避免把同一文档的重复片段误当成独立印证。

## 验证证据

- 回答质量、检索、上下文门、提示词、GraphRAG 与项目范围组合：`225 passed`。
- `scripts/check.ps1`：后端 `1151 passed, 1 warning`，Python compileall 通过。
- 前端生产构建通过，7 组前端测试共 `48 passed`。
- Tauri `cargo check --locked` 通过。

## 边界

本轮证明的是同名跨 scope 证据不会被 corroboration 门错误合并，不证明证据内容彼此独立、因果关系成立、召回率、引用正确率、答案准确率、真实模型链路或跨存储 exactly-once。稳定 ID 缺失的历史索引仍会回退展示名称，不能把该回退当作完整 provenance。
