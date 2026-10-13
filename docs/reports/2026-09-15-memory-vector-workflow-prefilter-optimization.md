# 记忆 Vector Set 工作流预过滤优化

日期：2026-09-15  
范围：Redis 8 Vector Set、研究窗口记忆检索、workflow namespace

## 外部实践与真实缺口

对照 Redis 官方 [Vector Set filtered search](https://redis.io/docs/latest/develop/data-types/vector-sets/filtered-search/) 和 [VSIM](https://redis.io/docs/latest/commands/vsim/)，属性过滤应在向量相似度搜索前缩小候选集合，`FILTER-EF 0` 用于稀疏过滤条件下继续寻找足够候选。

SoulDrive 的 RediSearch 分支已经把 `workflow_id` 放入查询过滤器，但 Vector Set 分支此前只按项目、会话、时间、模型和 active 状态预过滤，workflow 仍在 KNN 取回后才由权威门过滤。当其他研究窗口的高相似向量数量超过候选深度时，合法窗口的 `workflow_fact` 可能在进入后置门前就被挤出，导致窗口职责不同但召回不足。

## 最小实现

- Vector Set 新写入 metadata 始终保存 `workflow_id`：绑定窗口写入白名单值，共享记忆写入空值。
- workflow 查询的 `VSIM FILTER` 允许“当前窗口或共享空窗口”，把窗口边界前移到向量检索。
- 已有老 projection 可能没有该字段；过滤结果不足时保留无过滤兼容补读，再执行既有 `_vector_result_matches_scope()` 权威项目、workflow、时间校验与 memory ID 去重。
- 未引入新的向量数据库、GraphRAG 图数据库或 Agent 框架，也没有放宽未知 workflow 的拒绝规则。

## 失败优先与证据

先在现有 workflow Vector Set 回归中断言过滤表达式包含窗口条件；旧实现只产生项目/时间过滤，测试失败。修复后：

- `test_redis_vector_scope_keeps_workflow_facts_in_their_workflow` 与 Vector Set 写入/过滤回归通过。
- 完整 `tests/test_conversation_store.py`：`87 passed`。
- 完整工程门禁：后端 `1546 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。

后续非有限分数修复后的收尾复核：完整工程门禁后端 `1550 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过；冻结记忆质量 `7/7` 场景、`8/8` 查询通过，GraphRAG 路由 `4/4` 通过，研究窗口 runtime `5/5` 通过。

## 边界

旧 projection 的兼容补读依赖候选不足触发，尚未做真实 Redis 8 实例迁移或全量重建；因此不宣称所有存量向量都已完成 metadata 补齐，也不宣称 workflow 召回率、p95 延迟、答案准确率或真实 UI/E2E 已达标。
