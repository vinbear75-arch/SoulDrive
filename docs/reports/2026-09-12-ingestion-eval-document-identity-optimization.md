# SoulDrive 摄取评测文档身份语义优化

日期：2026-09-12

## 本轮结论

本轮审计发现，`eval/ingestion_quality_v0.1.jsonl` 的每条记录都有 `SDOC-*` 语料库标签，但 `scripts/eval_ingestion.py` 原先完全忽略 `doc_id`，并通过双向文件名模糊包含匹配定位分块。这样会把 `安全治理标准-旧版.pdf` 当成 `安全治理标准.pdf` 的摄取结果。

同时，运行时索引元数据中的 `document_id` 是本地路径或资产稳定 ID，不是 `SDOC-*` 语料库标签。两者不能直接比较，否则合法的当前来源也会被误判为缺失。

## 外部实践对照

- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将 Document 与 TextUnit 建立来源链路，TextUnit 作为知识项回溯原始文本的 provenance；本轮借鉴“来源链路独立于展示文本”的原则。
- [Ragas Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/) 提供基于 retrieved/reference context ID 的评测思路；SoulDrive 的 `SDOC-*` 属于评测语料库命名空间，未与运行时资产 ID 强行合并，也没有为此引入外部依赖。
- GraphRAG 的数据流还明确区分 Document、TextUnit 与后续派生图数据；本轮因此保留运行时资产身份供审计展示，同时让评测集来源标签负责金标准匹配。

## 失败优先证据

- `test_ingestion_eval_rejects_legacy_source_for_corpus_document_id`：复现旧版同名文件被双向模糊匹配误纳入的路径，修复后不再命中。
- `test_ingestion_eval_does_not_compare_corpus_id_to_runtime_asset_id`：确认合法来源携带不同运行时 `document_id` 时仍可通过，避免跨命名空间误判。
- 原有无 `doc_id` 的旧索引回归仍通过：没有评测身份时继续允许 `document_title` 的兼容匹配。

## 最小实现

- `scripts/eval_rag.py` 增加严格规范化来源匹配入口 `source_matches_exact`，保留原有模糊/别名匹配供旧数据使用。
- `scripts/eval_ingestion.py` 增加 `chunk_matches_record_document`：
  - 分块显式提供 `evaluation_doc_id`、`corpus_doc_id` 或 `gold_document_id` 时，与评测记录的身份直接比较；
  - 记录只有 `doc_id` 时，按规范化后的 `source_filename` 精确匹配，不把运行时 `document_id` 当作语料库 ID；
  - 记录没有评测身份时，保留原有文件名/别名及旧索引标题兼容路径。
- 逐样本结果新增 `expected_document_id` 与 `document_identity_required`，使评测报告能区分身份约束是否生效。

## 验证证据

- 摄取评测专项：`13 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1176 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。
- 前端仍有既有的 Vite 大 chunk 警告；本轮没有把它误报为摄取质量结论。
- `git diff --check` 无空白错误；仅有工作区既存的 LF/CRLF 转换提示。

## 明确边界

- 本轮修复的是摄取评测的身份完整性，不证明真实索引的召回率、切片质量或答案准确率提升。
- 目前未把 `SDOC-*` 写入生产索引元数据；如果以后需要跨评测集自动回放，应新增明确的评测映射字段，而不是复用 `document_id`。
- 没有引入 Ragas、GraphRAG 或在线 tracing 依赖，也没有声称实现 exactly-once 摄取。
- 真实工作区、打包启动、UI/E2E 和多用户权限仍不在本轮自动回归覆盖内。
