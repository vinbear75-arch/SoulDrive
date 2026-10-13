# Skill run 完成态 fencing 优化

日期：2026-09-12

## 结论

本轮修复了 Skill durable run 的终态覆盖问题。此前 `update_skill_run` 已阻止 `completed → failed`，但允许迟到的另一个 `completed` 回调覆盖已经持久化的 outcome 和 failure reason；同一 `run_id` 的旧执行因此可能改写当前已确认结果。

现在 `completed` 是不可变终态：任何后续状态或结果回写都只返回当前已持久化记录；`planned/failed → completed` 的正常重试恢复仍可发生。没有新增执行框架、attempt 表或依赖。

## 外部实践对照

- [Temporal activity state](https://github.com/temporalio/temporal/blob/main/chasm/lib/activity/proto/v1/activity_state.proto) 为每次 activity attempt 保存 `stamp` 与 `started_stamp`，用于丢弃旧 attempt 的任务；其状态机将 completed 作为明确终态。
- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 说明 task token 对一次 Activity execution 唯一，重试后旧 token 可能失效；本地实现不能让同一 run 的迟到回调覆盖新终态。
- [Temporal service API](https://github.com/temporalio/api/blob/main/temporal/api/workflowservice/v1/service.proto) 将完成回执绑定到 task token 或 Activity ID，而不是只按业务结果字符串接受任意回写。

本轮只迁移“终态不可被旧回调改写”的原则，保留 SoulDrive 现有本地 `run_id`、幂等键和 `LearningStore` 结构。

## 失败优先验证

- 新增 `LearningStoreTests.test_completed_skill_run_cannot_be_overwritten_by_late_completion`。
- 修复前：第一次完成写入“当前执行的正式结果”，另一个连接以 `completed` 回写“旧执行迟到的错误结果”，读取到的 outcome 被覆盖；测试失败于 outcome 不一致。
- 修复后：迟到回写返回原 completed 记录，outcome 与 failure reason 保持第一次确认的内容。
- 既有 `completed → failed` late callback 回归仍通过，证明本轮没有把已失败运行错误地锁死为不可恢复。

## 实现与验证

- `core/learning_store.py::update_skill_run` 将已完成记录统一视为不可变终态；数据库层原有条件更新继续保护“读取后、提交前被其他连接完成”的竞态。
- 定向终态回归：`2 passed`。
- LearningStore + TaskJournal + MCP 学习组合：`147 passed, 1 warning`。
- 仓库统一检查：后端 `1140 passed, 1 warning`；前端生产构建与 7 组 UI 静态测试合计 `48 passed`；Tauri `cargo check --locked` 通过。真实 Tauri/WPS UI、安装包启动和跨进程 E2E 仍不由该检查证明。

## 边界

- 本地 Skill run 仍是单机声明式执行记录，不提供跨主机 worker、心跳或 exactly-once 外部脚本副作用。
- `failed → completed` 仍被允许用于显式重试恢复；若产品未来引入并行 attempt，需要把 attempt/lease token 作为 API 参数继续收紧，而不是仅依赖 `run_id`。
- 本轮没有改变脚本执行目录、超时或权限策略。
