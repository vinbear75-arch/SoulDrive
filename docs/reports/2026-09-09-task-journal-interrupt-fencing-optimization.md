# SoulDrive TaskJournal 中断窗口 fencing 优化记录

日期：2026-09-09  
范围：本机 `TaskJournal` 的 worker 恢复、租约 epoch 和步骤/任务提交边界

## 结论

本轮在已有“新 worker 接管后旧 worker 不能提交”的回归之外，又发现并修复了一个更早的竞态窗口：恢复逻辑把运行中的任务改为 `interrupted` 后，到新 worker 重新 claim 之前，旧 worker 的 `lease_epoch` 原来没有变化，仍可能提交步骤输出，甚至尝试提交最终结果。

现在 `interrupt_task` 在运行任务转为 `interrupted` 或取消态时同步递增 `lease_epoch`，旧 worker 的任何带旧 epoch 的步骤完成和任务完成都会被 `TaskLeaseLostError` 拒绝。新 worker 仍可正常重新 claim，并从已完成 checkpoint 继续执行。

## 外部实践对齐

- [Durable Workflow：Execution Guarantees and Idempotency](https://durable-workflow.com/docs/constraints/execution-guarantees/) 将 at-least-once 投递、持久化提交和幂等边界分开，恢复后的旧执行不能覆盖新的持久状态。
- [Sledge](https://github.com/torkbot/sledge) 将 lease recovery 与 application-level fencing 放在同一条恢复路径上；SoulDrive 本轮把可迁移的不变量补到现有单机 SQLite 日志，没有引入重量级分布式运行时。

## 实现与回归

- `core/task_journal.py`
  - `interrupt_task` 在运行任务转入 `interrupted`/取消态时递增 `lease_epoch`。
  - 保留现有任务/步骤输出加密、恢复后跳过已完成步骤和新 worker claim 递增 epoch 的行为。
- `tests/test_task_journal.py`
  - 新增 `test_interrupted_task_fences_old_worker_before_reclaim`。
  - 回归先在旧代码上失败：恢复后旧 epoch 的 `complete_step` 没有抛出 `TaskLeaseLostError`；修复后通过。

## 验证边界

- `tests/test_task_journal.py tests/test_presentation_service.py tests/test_mcp_server_presentations.py`：`32 passed, 1 warning`。
- 这仍是单机 SQLite durable execution；不声称跨主机 exactly-once，也不能撤销旧 worker 已经完成的外部 SMTP/WPS 副作用。不可逆副作用必须继续依赖业务幂等键、确认门和结果查询。
