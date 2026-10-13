# SoulDrive 上下文精确率评测优化

日期：2026-09-10

本轮继续采用“外部实践对照 → 现有评测审计 → 最小确定性改动 → 定向/全量验证”。目标是补齐 RAG 评测中的检索噪声维度，不把一个新指标包装成答案准确率。

## 外部实践对照

- [Ragas 可用指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)将 Context Precision、Context Recall、Faithfulness、Response Relevancy 分开；召回到相关来源不等于前排上下文足够精确，也不等于答案已被事实支撑。
- [Haystack Evaluation](https://docs.haystack.deepset.ai/docs/evaluation)将组件级检索/文档评测与端到端答案评测分开，并区分依赖标签的统计评测与模型评测。对本机小项目而言，先补无需新模型和新依赖的标签型指标更可复现。
- [Microsoft GraphRAG 查询总览](https://microsoft.github.io/graphrag/query/overview/)仍强调不同问题类型要走不同查询路径；因此评测应保留逐样本、路径和证据位置，而不是只给一个总命中率。

## 现有缺口

`scripts/eval_rag.py` 已有来源覆盖率、MRR、引用合法性、claim-to-citation 支撑、拒答和项目越界指标，但此前没有记录“相关证据是否被无关 chunk 挤到后面”。`average_source_coverage` 只能回答期望来源覆盖了多少，无法回答 Top-K 前排是否噪声过多。

## 最小实现

新增 `context_precision_score`：

- 以期望来源作为当前冻结数据上的相关性标签；
- 对每个相关证据在其排名位置计算 precision，再取相关证据位置的平均值，保留排序敏感性；
- `context_precision` 逐样本保存，`average_context_precision` 汇总并进入对比报告；
- 没有期望来源的拒答样本标记为“不适用”，不把无答案题伪装成正向检索样本；
- 不改变线上召回、重排、生成或提示词，也不引入 Ragas、Haystack 等运行依赖。

例：`noise, alpha, beta` 的上下文精确率为 `(1/2 + 2/3) / 2`，而 `alpha, beta, noise` 为 `1.0`；两者来源覆盖率都可为 `1.0`，但排序质量不同。

## 验证证据

- `tests/test_eval_assets.py`：`40 passed`；新增噪声在前、相关证据在前和拒答分母边界回归。
- 评测/schema/诊断组合：`55 passed`。
- `scripts/check.ps1`：后端 `1083 passed, 1 warning`；Python compileall 通过；前端构建通过、7 组前端测试 `48 passed`；Tauri `cargo check --locked` 通过。
- 构建仅保留既有前端 bundle 大于 500 kB 的提示，以及既有 FastAPI/Starlette `httpx` deprecation warning；没有新增失败。

## 边界与下一轮

- 该指标是基于冻结来源标签的检索排序指标，不是 LLM-as-a-judge，也不是事实正确率、答案完整性或用户满意度。
- 仍需在同一冻结数据、索引、模型和延迟预算下比较 B0/B1/候选，并分别查看 `context_precision`、`source_coverage`、引用支撑和拒答正确率；不能只看综合平均值。
- 真实答案相关性、跨窗口角色差异和记忆注入收益仍需逐样本答案生成评测；在证据闭环前不引入自由循环 Agentic RAG。
