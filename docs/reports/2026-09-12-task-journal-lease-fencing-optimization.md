# TaskJournal lease epoch fencing 优化

日期：2026-09-12

## 结论

本轮收紧了 TaskJournal 的 worker 写入边界。此前 `begin_step`、`complete_step`、`fail_step` 和 `complete_task` 虽然支持 `lease_epoch`，但省略参数时会跳过校验；因此直接调用者可能在任务被恢复、重新 claim 后，仍用陈旧执行上下文写入步骤或任务结果。

现在所有上述状态变更入口都要求调用方携带 claim 返回的 `lease_epoch`。缺失或不匹配都抛出 `TaskLeaseLostError`；生产 `run_steps()` 已经在 claim 后把同一 epoch 贯穿到每个步骤和最终化动作。取消与 orphan recovery 仍是控制面操作，不借用 worker lease。

## 外部实践对照

- [Temporal activity state](https://github.com/temporalio/temporal/blob/main/chasm/lib/activity/proto/v1/activity_state.proto) 将 attempt-scoped stamp 用于丢弃旧 attempt 的 in-flight task，并区分当前活动生命周期 stamp 与当前 worker attempt stamp。
- [Inngest SDK specification](https://github.com/inngest/inngest/blob/main/docs/SDK_SPEC.md) 将 `runId`、`attempt` 和 memoized step result 作为 durable step 恢复的身份基础，完成后的 step 结果不应被重复执行覆盖。
- [Restate service invocation protocol](https://github.com/restatedev/service-protocol/blob/main/service-invocation-protocol.md) 将状态迁移写入 invocation journal，使恢复从已确认的进度继续，而不是信任旧执行者的内存状态。

本地只迁移“每次 worker 写入都必须带当前 claim 的代际标识”这一原则；没有引入 Temporal、Inngest 或 Restate 运行时，也没有宣称对外部副作用提供 exactly-once。

## 失败优先验证

- 新增 `TaskJournalTests.test_worker_without_lease_epoch_cannot_commit_after_lease_recovery`。
- 修复前：旧 worker 在任务恢复并由新 worker claim 后省略 `lease_epoch`，仍可把新 worker 正在执行的步骤写成 `completed`，测试失败于未抛出 `TaskLeaseLostError`。
- 修复后：省略 epoch 的写入在数据库更新前被拒绝，步骤保持 `running`；TaskJournal 全部回归 `14 passed`。

## 实现与验证

- `core/task_journal.py::_assert_lease_epoch` 不再把 `None` 当成“无需校验”，而是将缺失 epoch 视为无效 worker lease。
- 既有单元测试改为显式使用 `claim_task()` 返回的 epoch，避免测试继续掩盖不安全调用。
- TaskJournal、PresentationService、PPT MCP 接口组合回归：`38 passed, 1 warning`。
- 本轮 `scripts/check.ps1`：后端 `1142 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。该结果只代表本机 Python/前端/Cargo 自动门，不代表真实 Tauri/WPS 或安装包交付。

## 边界

- 这是 SQLite 工作区内的 worker 状态 fencing，不是跨主机租约服务、网络分区下的共识或外部提供方 exactly-once。
- worker 在写入前已经成功调用外部系统、随后丢 lease 的重复副作用，仍需外部幂等键、结果查询或人工核验；TaskJournal 只能防止陈旧结果继续污染本地 journal。
- `request_cancel` 和 `recover_orphaned_tasks` 属于用户/恢复控制面，不要求 worker epoch；它们通过状态迁移和 epoch 递增使旧 worker 后续写入失效。
