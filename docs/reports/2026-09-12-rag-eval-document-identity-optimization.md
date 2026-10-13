# SoulDrive RAG 评测文档身份绑定优化

日期：2026-09-12

## 本轮结论

本轮审计发现，`eval/retrieval_quality_blind_v0.3.jsonl` 等数据已经提供 `doc_ids`，但这些 `SDOC-*` 值是评测语料库标签；运行时引用中的 `document_id` 是本地资产/路径稳定 ID，二者不属于同一命名空间。此前 `EvalRecord` 丢弃了语料库标签，评分一直使用文件名模糊包含匹配，既无法利用评测身份，也会把同名旧版、带额外后缀的错误文档误算为命中。

现在评测会保留语料库标签，并在存在 `doc_ids` 时启用严格的规范化来源名/别名匹配；只有结果显式携带 `evaluation_doc_id`、`corpus_doc_id` 或 `gold_document_id` 时，才与语料库标签直接比对。运行时 `document_id` 继续输出供审计，但不会被错误地拿来和 `SDOC-*` 直接比较。

## 外部实践对照

- [Ragas IDBasedContextPrecision](https://github.com/vibrantlabsai/ragas/blob/main/docs/concepts/metrics/available_metrics/context_precision.md) 提供了基于 retrieved context IDs 与 reference context IDs 的精确检索评测路径，避免只比较展示文本或文件名。
- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 Document、TextUnit 和来源引用关联起来，用稳定来源链路支持 provenance，而不是把最终展示标题当作唯一身份。
- [Phoenix retrieval evaluation](https://github.com/Arize-ai/phoenix/blob/main/packages/phoenix-client/docs/source/index.md) 将检索文档及其 metadata 作为独立评测输入，支持逐文档相关性分析；SoulDrive 本轮仅借鉴身份/metadata 约束，没有引入外部评测依赖。

## 失败优先证据

- 新增 `test_corpus_doc_id_ground_truth_does_not_accept_same_named_wrong_document`：先复现“`安全治理标准-旧版.pdf` 的文件名包含匹配足以命中，但来源不是严格的评测来源”的误通过路径。
- 新增 `test_corpus_doc_id_does_not_compare_runtime_asset_id`：确认合法来源即使携带不同的运行时资产 `document_id` 仍可命中，防止跨命名空间误判。
- 新增 `test_eval_dataset_preserves_document_identity_ids`：确认 JSONL 中的 `doc_ids` 不再在加载阶段丢失。

## 最小实现

- `EvalRecord` 新增可选 `expected_document_ids`，从 `doc_ids`/`expected_document_ids` 读取。
- 当金标准提供 `doc_ids` 时，`source_hit`、首条命中、MRR、Context Precision 和答案引用来源覆盖均按严格规范化来源名/别名匹配；缺少显式评测身份字段时，不会把运行时 `document_id` 当成 `SDOC-*` 直接比较，也不会回退到模糊包含匹配。
- 当结果显式提供 `evaluation_doc_id`、`corpus_doc_id` 或 `gold_document_id` 时，才按评测身份精确匹配；运行时 `document_id` 仅作为逐样本审计输出。
- 当金标准没有文档 ID 时，继续使用已有文件名/别名匹配，兼容旧版评测集，并输出 `document_identity_required` 与 `top_document_ids` 供审计。

## 验证证据

- 评测资产专项：`43 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1174 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试 `49 passed`；Tauri `cargo check --locked` 通过。
- Vite 仍报告既有的大 chunk 警告；本轮未把它误报成评测质量结果。

## 明确边界

- 这次修复提升的是评测身份完整性，不是检索召回率或答案准确率提升证明；运行时资产 ID 与评测语料库 ID 的命名空间差异是本轮明确保留的边界。
- 未给出 `doc_ids` 的旧评测集仍只能按文件名/别名评测；要获得身份级结论，必须先冻结并补齐文档 ID。
- Ragas/Phoenix/GraphRAG 的完整框架、LLM-as-a-judge 或在线 tracing 均未引入；本轮保持本机、无新增依赖和可复现测试边界。
