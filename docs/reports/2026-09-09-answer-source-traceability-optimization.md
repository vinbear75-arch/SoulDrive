# SoulDrive 答案来源可追溯性优化

日期：2026-09-09

## 外部实践对照

- [Ragas 可用指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)将 Context Recall 与 Faithfulness 分开：召回了相关上下文，不等于答案中的陈述已经被正确支撑。
- [Ragas Faithfulness 说明](https://github.com/vibrantlabsai/ragas/blob/main/docs/concepts/metrics/available_metrics/faithfulness.md)把答案拆成可验证陈述，再检查陈述是否能由检索上下文支持；这为 SoulDrive 增加“答案实际引用了哪些期望来源”提供了边界。
- [Microsoft GraphRAG Local Search](https://microsoft.github.io/graphrag/query/local_search/)强调结构化关系与文本上下文共同进入生成上下文。对 SoulDrive 而言，来源绑定必须保留到答案层，不能只在召回层记录。

这些实践没有被直接复制为新依赖；本轮只在现有离线评测器中增加可解释、可复现的来源引用门。

## 发现的缺口

原有 `scripts/eval_rag.py` 只判断期望来源是否出现在 Top-K 证据中。对于跨文档题，以下结果会出现指标错位：

```text
证据：E1 -> alpha.pdf，E2 -> beta.pdf
答案：alpha 与 beta 均有记录。[E1]
```

此时检索来源全命中，但答案没有实际引用 `beta`。如果只看 `source_all_hit`，会把证据覆盖误当成答案可追溯。

## 实现

- 依据答案中的 `[E1]`、`[E2]` 等引用 ID，反查证据来源文件；
- 普通单来源题要求至少一个期望来源被实际引用；
- `requires_multi_doc=true` 要求每个期望来源都被实际引用；
- `answerable_pass` 同时依赖检索来源门和答案来源引用门；
- 汇总记录答案来源全命中率和平均来源引用覆盖率；拒答样本不混入该分母；
- 对比报告打印这两个生成层指标，避免优化只改善召回而答案仍漏引。

## 验收证据

- 修复前新增反例测试失败，报错为缺少 `expected_source_citation_all` 字段；
- 修复后 `tests/test_eval_assets.py tests/test_evaluation.py`：`39 passed`；
- 仓库级 `scripts/check.ps1`：后端 `1002 passed, 1 warning`；前端构建通过，7 组前端测试共 `43 passed`；Tauri `cargo check --locked` 通过；
- 当前唯一已知自动化警告仍为 Starlette/httpx 弃用提示。

## 边界

这轮只验证“引用 ID 是否覆盖期望来源”，不等于验证引用正文已经完整回答问题，也不等于真实模型答案准确率提升。语义支撑仍由现有 citation-support 检查负责，真实模型留出集、真实 Tauri/WPS/UI/E2E 和安装包仍需独立验收。
