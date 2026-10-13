# SoulDrive RAG Miss 分析身份规则一致性优化

日期：2026-09-12

## 本轮结论

主评测 `scripts/eval_rag.py` 已经区分评测语料库 `doc_ids` 与运行时资产 `document_id`，但 `scripts/analyze_rag_eval_misses.py` 仍直接使用旧的文件名模糊匹配。这样同一个结果可能在主评分中是 miss，却在错误分析报表中被统计为 hit，导致后续优化方向失真。

## 外部实践对照

- [Phoenix RAG retrieval evaluation helpers](https://github.com/Arize-ai/phoenix/blob/main/packages/phoenix-client/docs/source/index.md) 将 retrieved document、document metadata 和 document position 作为评测输入，并支持从 span 中提取可追溯的检索文档。
- [Ragas 的实验与错误分析示例](https://github.com/vibrantlabsai/ragas/blob/main/docs/howtos/applications/evaluate-and-improve-rag.md) 保留逐样本结果和 trace 标识，支持从整体指标回到具体失败样本；本轮借鉴“诊断必须复用同一逐样本判定”的原则。
- [GraphRAG provenance 数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将来源链路保留在 Document/TextUnit 之间；本轮因此让 miss 分析复用主评测的来源身份判断，不另造一套字符串规则。

## 最小实现

- `analyze_rag_eval_misses.py` 改用 `eval_rag.evidence_matches_expected_source`，支持语料库标签、显式评测身份字段和严格来源匹配。
- 分析样本新增 `expected_document_ids`、`document_identity_required` 与 `top_document_ids`，便于从 miss 回溯“金标准身份”和“运行时证据身份”。
- `eval_rag.evidence_evaluation_id` 同时检查证据顶层与 `metadata` 中的 `evaluation_doc_id`、`corpus_doc_id`、`gold_document_id`，适配不同结果序列化形态。

## 验证证据

- 评测资产与摄取评测专项组合：`58 passed`。
- 新增 `test_miss_analyzer_uses_the_same_corpus_identity_rule_as_scoring`，确认旧版同名来源在 miss 分析中仍被判定为 miss，而不是被模糊匹配吞掉。
- 仓库级 `scripts/check.ps1`：后端 `1178 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。
- `git diff --check` 无空白错误；工作区仅报告既存的 LF/CRLF 转换提示。

## 明确边界

- 本轮修复的是评测诊断一致性，不是检索质量或答案准确率提升。
- 没有引入 Phoenix/Ragas tracing 依赖，也没有把 runtime `document_id` 改名或强行映射为评测 `SDOC-*`。
- 诊断脚本仍分析已保存的 JSONL 结果；真实线上 trace、可视化仪表盘、人工标注工作流和 UI/E2E 不在本轮覆盖内。
