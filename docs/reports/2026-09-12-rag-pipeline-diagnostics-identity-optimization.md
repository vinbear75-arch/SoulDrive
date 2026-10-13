# SoulDrive RAG 流水线诊断文档身份一致性优化

日期：2026-09-12

## 本轮结论

`scripts/diagnose_rag_pipeline.py` 会分别统计 dense、keyword、source-title、merge、GraphRAG、rerank、final 及深度扫描阶段。原实现所有阶段都只按文件名模糊匹配，因此即使主评分已经拒绝旧版同名来源，阶段诊断仍可能把它计为召回成功。

## 外部实践对照

- [Phoenix 的检索文档评测入口](https://github.com/Arize-ai/phoenix/blob/main/packages/phoenix-client/docs/source/index.md) 将 document、metadata、position 和 trace 作为检索评测上下文；本轮把阶段快照中的身份字段保留下来，便于定位是哪一级发生了身份漂移。
- [GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 Document、TextUnit 和来源引用连接起来；本轮让每个诊断阶段共享同一来源判定，而不是为 dense/keyword/rerank 分别复制规则。
- [Ragas Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/) 强调按检索上下文逐项评估；本轮只借鉴稳定身份与逐阶段可审计原则，不引入 Ragas 依赖。

## 最小实现

- 从诊断样本的 `doc_ids`/`expected_document_ids` 读取语料库身份，并校验它与 `expected_sources` 数量一致。
- 将身份参数传递到 dense、keyword、source-title、merged、graph-boosted、reranked、final、recall-depth 和 final-K 扫描。
- 阶段快照保留 `document_id`、`evaluation_doc_id`、`corpus_doc_id`、`gold_document_id`；运行时 `document_id` 只做审计展示，不与 `SDOC-*` 直接比较。
- 所有阶段统一调用 `eval_rag.evidence_matches_expected_source`：有显式评测 ID 时直比；只有语料标签时严格匹配规范化来源；无身份旧数据仍走兼容路径。
- `compact_stage_result` 同步保留身份约束、预期身份与顶层文档 ID，使深度扫描报告不丢失语义。

## 失败优先与验证证据

- 失败优先测试先以 `expected_document_ids` 调用阶段评分，旧实现因不接受身份参数而失败，确认测试覆盖了实际缺口。
- `test_stage_identity_evaluation_rejects_legacy_source_and_keeps_runtime_id`：旧版同名来源不再被阶段诊断计为命中，同时保留运行时资产 ID。
- `test_stage_identity_evaluation_accepts_exact_source_with_runtime_id`：合法来源不会因运行时资产 ID 不同而被拒绝。
- 诊断专项：`11 passed`；评测资产、摄取评测、诊断三组组合：`69 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1180 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。

## 明确边界

- 本轮修复阶段诊断的身份一致性，不证明任何召回率、MRR、答案准确率或 GraphRAG 质量提升。
- 没有引入在线 tracing、外部评测服务或新的索引字段；真实工作区诊断仍需在冻结数据和同条件 baseline/candidate 下运行。
- `scripts/check.ps1` 仍不等同于真实 Tauri UI、安装包冷启动、视觉布局和端到端链路证明。
