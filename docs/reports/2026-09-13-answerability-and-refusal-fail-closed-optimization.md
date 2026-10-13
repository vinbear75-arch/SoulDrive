# 证据可回答性与拒答回退 fail-closed 优化

日期：2026-09-13

## 发现的问题

本轮第一次跑拒答集时只有 `1/4` 正确拒答，`3/4` 被错误放行。失败不是检索完全没有命中，而是两个边界叠加：

1. `evidence_answers_query()` 把“本地、给出、建议、商业、许可证”等泛词重叠当成问题已被回答；天气、商业许可证价格等越界问题因此进入生成阶段。
2. 主 RAG 链已经拒答时，评测辅助回退仍会重新调用裸 `search_with_evidence()` 并直接抽取证据；敏感个人信息拒答因为没有证据块，被这个回退重新打开。

这类问题说明“召回到文档”不能等价于“文档能够回答问题”，而“主链拒答”也必须是终态，不能被后置 fallback 覆盖。

## 外部实践对照

- [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/) 和 [Qdrant text filtering](https://qdrant.tech/documentation/search/text-search/text-filtering/) 将精确 metadata/payload 条件与向量相似度分开；本轮把硬标识符和问题信号当成回答资格门，不把低分相似召回当作答案许可。
- [LlamaIndex RetrieverEvaluator](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/evaluation/retrieval/evaluator.py) 体现了召回评测与回答质量评测分离；本轮同时记录可回答性、拒答漏放和误拒，而不是只看来源是否出现。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 的终态与重放边界对应本地回退语义：已经形成的拒答结果不能被另一个未经过同一门控的路径覆盖。

## 实现

### 1. 回答相关性硬门

`core/answer_quality.py`：

- 从查询中提取英文/数字硬标识符；证据中缺失 `SoulDrive`、`GDP`、年份、模型名等标识符时直接判定为“不回答该问题”。
- 对中文查询过滤常见流程词和泛化词，再计算问题信号覆盖；只有泛词重叠不再足以让低分证据进入生成。
- 对达到拒答分数但不回答问题的证据，优先返回结构化 `evidence does not answer query` 重试门；分数低于拒答阈值时仍保持置信度拒答语义。

### 2. 拒答结果不可被评测回退覆盖

`scripts/eval_rag.py`：生成器返回带拒答标记的文本时，评测辅助路径直接保留该结果，即使没有 evidence block 也不再调用裸抽取回退。模型/运行时异常仍可以进入受控抽取回退，但回退不再绕过已完成的拒答。

## 失败优先测试与结果

- 新增天气越界反例：模型选择建议文档含有“本地/给出/建议”等泛词，仍必须拒答。
- 新增许可证价格反例：证据提到商业模型和 MIT 许可，但缺少 `SoulDrive` 标识符和价格，仍必须拒答。
- 新增评测回退反例：主链已返回私人手机号拒答时，后置回退不得把作者联系方式证据重新抽出。
- 相关单元回归：`97 passed`；章节来源/上下文/检索组合回归：`175 passed`。
- 拒答冻结集 4 条：拒答正确率 `100%`，拒答漏放率 `0%`。
- 可回答开发集 16 条：全命中@6、首条命中@1、MRR、来源覆盖率、上下文精确率、引用合法率、引用支持率、综合可靠率和关键词覆盖率均为 `100%`，误拒率 `0%`。
- 可回答 sealed 集 8 条：上述指标均为 `100%`，误拒率 `0%`。

结果文件：

- `eval/results/rag_cn_raw_v0.1_final_refusal_trace_20260913.jsonl`
- `eval/results/rag_cn_raw_v0.1_final_dev_trace_answers_20260913.jsonl`
- `eval/results/rag_cn_raw_v0.1_final_sealed_trace_answers_20260913.jsonl`
- trace 逐样本缺失数为 `0`；拒答终态、正常完成和异常回退已可区分。详见 `docs/reports/2026-09-13-answer-eval-trace-observability-optimization.md`。

## 边界

- 这是冻结本地语料、当前量化模型和自动规则门的证据；4 条拒答样本不能代表长期越界/时效问题覆盖率。
- 硬标识符门只约束回答资格，不负责判断事实正确、因果关系或数值统计有效性；这些仍由引用支持、实验账本和人工评审负责。
- 评测回退安全性和最小决策 trace 已加固，但生产端真实 UI/Tauri 流式断线恢复、外部工具副作用、跨进程 exactly-once 和 OpenTelemetry/Langfuse 后端接入仍需独立验收。
