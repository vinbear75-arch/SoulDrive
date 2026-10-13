# GraphRAG 全局综合来源覆盖门优化

## 结论

本轮修复了一个真实的答案质量断点：全局 GraphRAG 已召回多份原始资料时，模型仍可能只引用 `[E1]`，而旧验收只检查“存在合法引用”，会把单一来源的伪综合误判为通过。

现在全局 GraphRAG 在最终上下文至少包含两份逻辑原始文档时，生成校验要求至少引用两份不同来源。生成重试仍不能满足时，系统不把单一来源包装成共同结论，而是按不同来源列出可定位证据，并明确这是受控回退。

## 外部实践对照

- [Microsoft GraphRAG query overview](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md) 将 global search 定义为面向全数据集的综合查询，并通过 community reports 做 map-reduce；local search 则面向实体和关系。
- [Microsoft GraphRAG global search](https://github.com/microsoft/graphrag/blob/main/docs/query/global_search.md) 明确 global query 会在多个 community reports 上进行 map/reduce/aggregate，而不是把单个社区报告当作全局结论。
- [Microsoft GraphRAG default dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 保留社区报告到原始文本的 provenance；SoulDrive 继续只允许原始 `[E#]` 证据支撑事实回答。

本轮只借鉴“多来源综合必须可观察、可回指”的质量边界，没有引入 Microsoft GraphRAG 运行时或新的重量级依赖。

## 失败优先证据

固定冷工作区 `eval/runtime/rag_cn_cold_candidate_20260913` 的全局查询此前实际召回 4 个原始来源，但回答只引用 `E1`。旧验收仍会把 `candidate_all_cited=true`，因此这个缺口不是假设问题。

新增回归先要求 `validate_answer_citations(..., required_source_count=2)` 拒绝单来源答案；同一文档的两个 citation ID 也只计为一个逻辑来源。回退路径回归要求保留多个来源引用，且不得声称它们已经形成共同结论。

## 实现

- `core/answer_quality.py`
  - 增加逻辑来源计数：优先使用 `source_document_ids`，普通证据使用带项目范围的 `source_diversity_key()`。
  - `validate_answer_citations()` 支持 `required_source_count`，对缺少来源覆盖的答案返回稳定的 retry 原因。
- `core/rag_engine.py`
  - 仅在 `GraphRoute(mode="global")` 且最终上下文至少有两份来源时启用来源门，普通 hybrid/local 和单来源 global 查询不受影响。
  - 将要求放入首个 system prompt，并在一次回答重试中重复约束；额外生成按逻辑文档去重的来源覆盖清单，帮助模型按 map/reduce 顺序逐份核对。
  - 生成无法通过时按不同原始来源输出可定位摘录，并标明“不把它们强行归纳为共同结论”；回执记录回退实际覆盖的逻辑来源数。
- `scripts/eval_graphrag_acceptance.py`
  - 验收新增 `global_source_diversity_satisfied` 和 `global_source_gate`，区分模型生成通过与受控来源门回退，不再只检查是否出现 `[E#]`。

## 验证结果

- 失败优先后变更定向回归：`41 passed`（来源校验、跨模态命名空间、GraphRAG、提示词、验收契约）。
- 完整工程门禁：后端 `1625 passed, 1 warning`；前端生产构建通过；8 组 UI 脚本合计 `62 passed`；Python `compileall` 与锁定 Cargo 通过。
- 冻结评测：记忆 `7/7` 场景、`8/8` 查询，召回 `1.0`，过期/越权泄漏 `0`；GraphRAG 路由 `4/4`；episodic 路由 `5/5`；工作流角色 `6/6`。
- 真实本地 GraphRAG 验收：`eval/results/graphrag_acceptance_final_source_plan_20260915.json`。
  - `index_ready=true`、`community_target_ok=true`。
  - 普通查询保持 `hybrid` 且不启用图；共同总结启用 `global`；关系问题启用 `local`。
  - `global_source_diversity_satisfied=true`；全局查询要求 `2` 个逻辑来源，受控回退实际覆盖 `4` 个来源，引用均属于最终证据集合。

## 边界

- 本轮证明的是来源覆盖门、来源清单和回退诚实性，不证明模型已经能稳定生成高质量的跨资料共同结论；固定冷工作区本次全局查询仍走了受控回退，说明提示约束尚未替代专门的跨来源综合评测。
- 来源数不是答案正确率；没有将 `Recall@K`、引用绑定或来源数量宣称为事实准确率。
- 仍未证明真实 Tauri/WPS UI/E2E、Redis/PostgreSQL/SMTP 外部服务、跨主机 exactly-once、断电恢复，或大规模语料下的 GraphRAG 社区摘要质量。
