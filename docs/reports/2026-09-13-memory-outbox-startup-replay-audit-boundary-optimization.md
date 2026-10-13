# 记忆 outbox 启动回放与审计故障边界优化

日期：2026-09-13

## 对照实践

- [Dapr Transactional Outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 将业务事实与待发布事件作为 durable intent 保存，再由可重试消费者处理。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将事件结果、工作租约、重试和去重状态纳入 durable ledger；观测事件不是主工作结果的替代品。
- [Restate durable execution](https://docs.restate.dev/quickstart) 的重试会跳过已经成功的步骤；后续步骤失败不应把已成功的步骤倒写成失败。

## 失败优先发现

启动加载工作区时，SoulDrive 先回放记忆派生索引 outbox，再写 `learning.memory_outbox_startup_replay` 审计事件。旧实现把两步放在同一个 `try` 中：如果回放已完成但审计 sink 抛错，异常处理会返回 `completed=0, failed=1`，同时工作区路径已经加入“一次回放”集合，后续进入点不会再按本轮结果重试。这会把已完成的 durable 投影误报成失败。

## 本轮修改

- 启动回放完成后，审计事件改用 `_append_audit_best_effort`。
- 回放计数只由真实 outbox 投影结果决定；审计故障不会覆盖 `completed/failed`，也不会触发错误的失败回放状态。
- 回放仍保持每工作区一次尝试的既有边界；真正的投影失败仍由 outbox 的 `failed` 结果和后续显式恢复路径表达。

## 验收证据

- 新增失败优先回归：`test_memory_outbox_startup_replay_result_survives_audit_failure`；同时保留“一次工作区加载只回放一次”回归。
- 学习模块全量回归：`93 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1310 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是跨数据库/消息代理的事务性 outbox；当前 outbox 只覆盖本地记忆派生投影。
- 这不是自动补偿证明；投影真实失败仍需要既有 sync replay/health 路径，审计 warning 不会替代失败状态。
- 不宣称跨进程 exactly-once、外部向量库一致性或真实 Tauri 启动 E2E。
