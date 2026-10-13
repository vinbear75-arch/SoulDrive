# SoulDrive RAG 评测契约优化

日期：2026-09-09

## 外部实践对照

- [Ragas 可用指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)把 Context Precision、Context Recall、Faithfulness 和 Response Relevancy 分开，避免只用一次召回命中就代表答案可靠。
- [Microsoft GraphRAG 查询总览](https://microsoft.github.io/graphrag/query/overview/)明确区分 Local Search 与 Global Search：实体局部问题和跨社区/全局主题问题使用不同的证据组织方式。
- [GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)强调结构化图信息与原始文本块共同进入上下文，候选实体、文本单元和社区报告都要经过排名与过滤。

这些项目没有直接授权 SoulDrive 引入新的依赖；它们给出的可迁移原则是：评测契约必须区分单来源与多来源任务，并把“证据覆盖”与“答案是否有引用”分开报告。

## 发现的本地缺口

`scripts/eval_rag.py:score_record` 原先统一使用：

```text
expected_sources 只要任意一个命中 → source_ok
```

这对单文档问题合理，但对 `requires_multi_doc=true` 的对比题过宽。例如期望来源为 `alpha`、`beta`，只返回 `alpha` 且答案只引用 `alpha` 时，旧逻辑仍可能把 `answerable_pass` 记为真。旧报告虽然另有 `multi_doc_all_hit_rate`，但综合可靠率没有使用这个硬条件，容易把检索指标和答案达标混淆。

## 最小修复

现在评测契约按样本类型选择来源门：

- 普通单文档题：`source_requirement=any`，命中任一期望来源即可进入后续答案检查。
- `requires_multi_doc=true`：`source_requirement=all`，所有期望来源都必须在 Top-K 证据中出现；缺任一来源时 `answerable_pass=false`。
- 结果行同时保存 `source_requirement_satisfied`，便于逐样本追查。

这只改变离线评测的判定，不改变线上召回或模型提示，不会凭评测代码制造检索提升。

同一轮还修正了答案评测分母：答案生成模式下，空答案不再从 `answer_sample_count`、可回答题达标率、拒答正确率、综合可靠率和关键词覆盖率中被排除，而是按失败样本计入；纯检索模式仍不虚构答案评测样本。

## 本轮新增：答案必须实际引用必需来源

继续对照 Ragas 的 Context Recall 与 Faithfulness 分离后，发现上一轮仍有一个边界：Top-K 证据中虽然同时召回了 `alpha`、`beta`，答案却可能只写 `[E1]`。这种结果的证据覆盖是完整的，但答案引用覆盖只有一半，不能算跨文档答案达标。

现在 `score_record` 进一步按“引用 ID -> 证据来源文件”建立映射：

- `expected_source_citation_all/any` 表示答案实际引用的证据是否覆盖全部/任一期望来源；
- `expected_source_citation_coverage` 记录答案引用覆盖比例；
- 多文档题的 `answerable_pass` 必须同时满足证据来源全命中和答案引用来源全命中；普通单文档题仍要求至少引用一个期望来源；
- 汇总新增 `answer_expected_source_all_hit_rate` 与 `answer_expected_source_coverage`，且拒答样本不进入答案来源覆盖分母。

因此现在可以明确区分：`multi_doc_all_hit_rate` 是检索层指标，答案来源覆盖率是生成层可追溯指标，二者不能互相替代。

## 验收证据

- 先新增反例测试：只命中 `alpha` 的 `alpha + beta` 对比题必须失败；修复前测试失败，修复后通过。
- `tests/test_eval_assets.py tests/test_evaluation.py`：新增“证据全命中但答案漏引 beta”反例；两项修复后的定向回归为 `39 passed`。
- 既有单文档、负例拒答、项目错误证据、引用语义支持和检索指标测试保持通过。
- 随后执行仓库级 `scripts/check.ps1`：当时后端 `1002 passed, 1 warning`；后续 TaskJournal fencing、聊天历史时点路由、记忆版本历史、工作流文档范围、中断 fencing、邮件预留竞态、长步骤恢复保护、记忆审核并发、自动记忆写入边界、审核草稿项目绑定、自动记忆候选范围、待审核列表范围、项目记忆变更范围和 GraphRAG 评测实际图路修正后的结果为后端 `1023 passed, 1 warning`；之后的记忆管理幂等键回执循环结果为后端 `1026 passed, 1 warning`，本地学习同步 outbox 后为 `1028 passed, 1 warning`，再之后活动记忆接受恢复和 claim/lease 完善后为 `1030 passed, 1 warning`，问答上下文自动重放后为 `1031 passed, 1 warning`，GraphRAG 关系召回/路由观测/纠错回调和 workspace load 扫描后为 `1036 passed, 1 warning`，本轮记忆 outbox 项目范围回放、完成标记故障回归、会话角色契约和幂等派生投影范围回归后最新为 `1043 passed, 1 warning`；前端构建通过、7 组测试 `44 passed`；Tauri `cargo check --locked` 通过。

## 仍然没有宣称的内容

- 该修复只提高答案评测门槛，不证明模型答案准确率已经提升。
- 仍未实现 Ragas 的 LLM-as-a-judge、自动数据生成或 GraphRAG 的社区报告索引；当前项目优先保持本机可复现、无新增重量级依赖。
- 下一轮需要继续在同一冻结样本上验证 local/global/graph/corrective 路径的延迟、证据覆盖和引用正确性；本轮已先补齐路径是否选择、是否执行和图上下文是否非空的观测字段。

## 本轮后续路由观测补丁

对照 Microsoft GraphRAG Local/Global 查询分层和 Haystack 的可观测多路组件后，评测结果新增 `retrieval_observability`。`graph_context_hit_rate` 保留兼容但明确是来源命中率；新字段 `graph_route_selected_rate`、`graph_stage_executed_rate`、`graph_context_nonempty_rate` 仅对带完整观测的样本计分，旧 JSONL 结果不会被假装成有路由证据。

同时，GraphRAG local 打分加入社区关系文本，RAGEngine 的图路由统一基于 contextual retrieval query，检索完成回调在一次有界纠错检索结束后才发送。定向 `137 passed`，最新全量后端 `1043 passed, 1 warning`；这仍不等同于 GraphRAG 答案准确率或 Agentic RAG 质量提升。
