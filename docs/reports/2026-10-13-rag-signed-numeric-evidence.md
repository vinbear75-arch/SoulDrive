# RAG 数字证据：保留正负号与科学计数法

日期：2026-10-13。范围：既有逐 claim 数字/单位引用检查及受控重试；不训练 Self-RAG，不引入评估服务。

## 外部机制 -> 本地反例

[Self-RAG 固定论文 v1](https://arxiv.org/abs/2310.11511v1)区分按需检索、相关性与生成内容的证据支持性；[Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)要求逐项检查回答主张是否由检索上下文支持。本轮仅借鉴“有引用标记不等于事实被支持”的边界，不复制论文训练方案或声称计算了 Ragas 分数。

旧数字正则只抽取无符号数字，`-5%` 和 `+5%` 退化成同一个数值，指数又会被拆成数字和单位片段。已有引用也会掩盖方向相反的事实；这会直接影响实验增减解释，而不只是日志展示。

## 固定反例与单因素修复

| 对照 | 固定输入/结果 |
| --- | --- |
| B0 数字检查 | 新增两个循环反例，旧正则下 `2 failed, 48 deselected`；包括负证据被正答案引用，以及科学计数法等价值被误拒绝 |
| B0 实际 RAG 流 | 只恢复旧数字正则，固定知识库给 `-5%`，固定 LLM 先给 `+5%`；`1 failed, 127 deselected`，错误正值直接进入输出 |
| B1 / Candidate | 数字 token 保留显式 `+/-/−` 与 `e/E` 指数，Unicode 减号归一化；既有单位/比例转换不改。数字检查文件 `50 passed` |
| Candidate RAG 流 | 首次 `+5%` 不外发；触发既有一次受控重试，第二次 `-5%` 通过。不是新增无限 Agent 循环 |
| 交叉回归 | answer quality、RAG prompting、GraphRAG acceptance、eval assets、窗口角色、记忆质量：`256 passed`，7.31 秒 |

反例还覆盖 `-2.5s` 与 `−2500ms` 等价、`1e-3` 与 `0.001` 等价、Unicode 指数减号、`1.0E+03ms` 与 `1s` 等价；同时保持 `3-5` 范围、`model-8B` 版本标签中的连接号不被误当作负号。代码与测试均 UTF-8。

B0 在脏工作区中仅还原本次数字规则，Candidate 立即恢复；没有改冻结数据、指标、阈值、提示词或模型。B1 已是最小确定性修复，B2 不适用；这些测试不是开放式数据上的质量收益。

## 完整回归

先后完整工程检查后端为 `1778 passed, 1 warning` 与 `1779 passed, 1 warning`，均退出 0；前端构建/66 项脚本、编译检查及现有四个质量门通过。原 transcript 未完整保存原生命令输出；第三次完整归档复跑同样退出 0，后端 `1779 passed, 1 warning`、459.61 秒，前端及所有工程门通过。1086 行最终日志、硬件/环境与文件指纹见 `2026-10-13-artifact-smoke-confirmed-workflow.md`。

代码指纹：`core/answer_quality.py` SHA-256 `b5cbc60d37ae53164c05a124f01e6042625099d1cc10eb09840f2f5296ea7d32`；`tests/test_answer_quality.py` SHA-256 `929f0bc017a9f611753b03c6187f7453e57609d4c426e66ed0f2687346e1e221`；`tests/test_rag_prompting.py` SHA-256 `9c8cb8c4770eb74fc36f259172339228bd9cf44321266963a68d0bb17dc5c066`。

## 仍未证明

不是通用数值蕴含或因果检查：自然语言“提升/下降”、复杂区间、逗号歧义、超高精度浮点数和量纲组合仍有边界。既有 float/12 位格式化未在此轮改为高精度系统。未测开放式答案准确率、Recall/MRR/nDCG、P50/P95、显存收益、真实模型调用、真实 UI 或端到端业务表现。
