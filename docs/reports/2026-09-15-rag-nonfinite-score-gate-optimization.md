# RAG 证据分数非有限值门禁优化

日期：2026-09-15  
范围：答案证据门、上下文门、GraphRAG/混合证据排序与引用修复

## 外部实践与真实缺口

本轮沿用外部 RAG/记忆组件的共同边界：检索分数只是排序信号，事实证据必须经过独立的范围、质量和可引用性门禁。对照 [Mem0 的事实源与派生向量分离](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)、[Redis Vector Set 的过滤检索](https://redis.io/docs/latest/develop/data-types/vector-sets/filtered-search/) 以及项目现有的 [GraphRAG 原文回指](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 约束，异常数值不应被视为有效高分。

代码审计发现，RAG 证据分数、支持度和 GraphRAG 合并排序仍有 `float("nan")`/`±Infinity` 的 fail-open 路径：

- `answer_quality.evaluate_evidence_gate()` 可能把 `NaN` 的最大分数保留下来，跳过低置信度拒答；
- `context_gate.admit_context()` 可能接收 `NaN` 证据，`support_score=NaN` 还会因 Python 的比较语义被归一化为 1.0，绕过相对支持度门；
- `_prioritize_context_evidence()` 的排序可能让 `NaN` 排在正常高分证据之前；引用修复的分数 tie-break 也没有统一有限值处理。

这类异常在正常冻结语料中通常不会出现，因此不能只依赖常规 RAG 样本命中率发现，必须用失败优先边界回归覆盖。

## 失败优先与最小实现

新增反例：

1. 只有 `NaN` 分数的证据必须被答案质量门拒绝，并报告最大分数为 `0.0`；
2. `NaN` 证据必须在上下文门被拒绝；
3. `support_score=NaN` 必须按保守的 0 分处理，不能当作缺失或满分；
4. GraphRAG/普通检索混合排序中，有限的 `0.2` 分证据必须排在 `NaN` 证据之前。

实现只做有限值归一化，不改变既有阈值和检索策略：

- `core/answer_quality.py` 的 `_safe_float()` 对非有限值返回 0；
- `core/context_gate.py` 的证据分数同样 fail-closed；`support_score` 的非有限数值按 0 参与相对支持度门；
- `core/rag_engine.py` 统一使用有限的证据分数进行 GraphRAG/混合排序和引用修复 tie-break。

## 回归与评测证据

- 失败优先：答案质量、上下文门、GraphRAG 非有限值反例均按预期失败。
- 修复后定向 RAG 组合：`132 passed`；上下文门新增回归后 `20 passed`。
- 全量工程门禁：后端 `1557 passed, 1 warning`（既有 Starlette/httpx 弃用警告）；前端生产构建成功，UI 脚本 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。
- 冻结记忆质量评测：`7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`，陈旧/禁止泄漏率 `0.0`。
- GraphRAG 路由评测：`4/4`，路由、图谱门和信号契约均 `1.0`。
- 研究窗口 episodic 评测：runtime `5/5`；baseline `2/5`，用于确认改动仍保留既有窗口隔离收益。
- 已有开发/封存/拒答答案结果复核：开发集 `16` 条、封存集 `8` 条、拒答集 `4` 条，均保持既有 `100%` 综合可靠率；这些是已保存结果复核，不是本轮重新调用模型。

## 边界

本轮证明异常浮点值不会从本地 RAG 的证据门、上下文门、GraphRAG 合并排序或引用修复路径进入有效证据；不等于召回率、答案事实忠实度、真实 Redis/PostgreSQL、跨进程 exactly-once、长时间性能、打包 Tauri/WPS UI/E2E 或真实模型端到端质量已完成验收。
