# 记忆历史版本重叠区间诊断优化

## 结论

本轮补齐 `LearningStore.memory_health()` 的一个时间一致性检查：同一记忆的两个活动历史版本如果时间区间重叠，健康检查现在报告 `memory_version_overlap`，并只返回版本 ID、记忆 ID 和重叠范围；不会自动选择、合并或删除任何版本。

## 外部实践对照

- [Graphiti MCP Server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 采用双时间模型，事实带 `valid_at`/`invalid_at`，新事实可以使旧事实失效但保留历史；查询也支持按有效时间筛选。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 将可恢复线程状态与跨线程长期 Store 分离，强调持久状态需要能被检查和回溯。SoulDrive 继续使用本地事实表、历史版本和 `as_of` 读取，不引入外部图数据库。

当前代码已经检查非法区间、孤儿版本和范围错配，但缺少“同一 memory 的正常区间彼此不相交”这一 doctor 规则。若不检查，损坏数据可能在同一个 `as_of` 时点同时返回多个历史事实。

## 实施

- 在 `memory_health()` 中收集同范围、同记忆的有效闭区间；非法区间仍单独报告，不与 overlap 噪声混合。
- 按 `valid_from` 排序，只有前一有效区间的 `invalid_at` 严格晚于后一段 `valid_from` 时报告重叠；相邻端点相等视为合法边界。
- 报告 `memory_version_ids`、`memory_ids` 和不含正文的 `overlap_range`；原有项目/全局范围过滤保持不变。

## 失败优先回归与验证

- 新增 `test_memory_health_reports_overlapping_history_versions`，先在旧实现上复现“只有 memory_missing_chunk、没有版本重叠诊断”，修复后通过。
- 学习 Store、记忆 API、memory gate/runtime 组合回归：`132 passed, 1 warning`。
- 全量 `scripts/check.ps1`：后端 `1110 passed, 1 warning`；Python compileall、前端生产构建、前端 7 组测试共 `48 passed`、Tauri `cargo check --locked` 均通过。

## 边界

- 这是只读完整性诊断，不是自动修复器；修复重叠区间需要人工确认事实优先级或重新生成派生历史。
- 检查的是同一 `memory_id` 的时间区间，不做跨记忆实体消歧，也不推断内容真伪。
- `memory_versions` 仍是本地事实历史；Redis/其他派生投影的一致性由既有 outbox、版本单调和后续健康检查边界负责。
