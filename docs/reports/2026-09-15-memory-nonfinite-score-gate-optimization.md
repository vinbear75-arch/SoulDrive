# 记忆非有限检索分数 fail-open 修复

日期：2026-09-15  
范围：记忆读取门、Redis 语义投影、学习上下文

## 外部实践与真实缺口

对照 [Mem0 how it works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 关于“事实源权威、向量存储为派生层”的边界，以及 [Redis Vector Set filtered search](https://redis.io/docs/latest/develop/data-types/vector-sets/filtered-search/) 对无效/缺失属性不应进入有效匹配的约束，本地检索分数也不能绕过最终读取门。

审计发现两条分数归一化路径都没有显式拒绝 IEEE 非有限值。`memory_runtime` 会把 `NaN/Infinity` 当作有效上下文相关度参与 `query_irrelevant` 判断；语义投影合并则可能把它们经过 `min/max` 归一化为 `1.0`。异常投影因此可能把无关记忆提升到上下文中，形成 fail-open。

## 失败优先与最小实现

先加入两个反例：

1. 无关记忆分别带 `math.inf` 和 `math.nan`，读取门必须拒绝并报告 `query_irrelevant`。
2. Redis 语义投影同时返回 `Infinity`、`NaN` 和正常分数，只有正常分数允许进入语义分数表。

实现保持最小范围：

- `core.memory_runtime._safe_float()` 只接受有限浮点值，非有限值按 `0.0` 处理，避免绕过相关性、排序和摘要计算。
- `core.mcp_server._semantic_memory_scores()` 在距离转换后先执行 `math.isfinite()`，再做 `[0, 1]` 截断；异常语义投影被跳过，不影响 SQLite/关键词回退路径。
- 不把语义投影变成候选事实源，当前有效 LearningStore 记录、项目/工作流/时间范围和既有 memory gate 仍是最终边界。

## 回归证据

- `tests/test_mcp_server_learning.py -k "semantic_memory_scores"`：`2 passed, 121 deselected, 1 warning`。
- `tests/test_mcp_server_learning.py tests/test_memory_runtime.py`：`144 passed, 1 warning`。
- 非有限值反例覆盖读取门和语义投影两个层级；后续全项目门禁与冻结评测结果以本轮收尾记录为准。

本轮收尾复核：完整工程门禁后端 `1550 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 检查通过；冻结记忆质量评测 `7/7` 场景、`8/8` 查询通过，`memory_recall_at_k=1.0`、陈旧/禁止泄漏率 `0.0`；GraphRAG 路由评测 `4/4` 通过。研究窗口情景评测保持 baseline `2/5`、runtime `5/5`。

## 边界

本轮证明异常数值不会通过这两条本地记忆链路进入上下文，不证明真实 Redis/RediSearch 投影质量、召回率、p95、模型答案准确率或真实 Tauri/UI/E2E。真实存储仍需使用集成环境和冻结数据集单独验证。
