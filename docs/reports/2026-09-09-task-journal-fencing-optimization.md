# SoulDrive TaskJournal 旧 Worker 防护优化

日期：2026-09-09

## 外部实践对照

- [Restate Architecture](https://docs.restate.dev/references/architecture)把步骤结果先写入 durable journal，再在重试时读取已提交结果，避免已经完成的步骤再次执行。
- [Durable Workflow：Execution Guarantees and Idempotency](https://durable-workflow.com/docs/constraints/execution-guarantees/)明确区分 worker 的 at-least-once 投递与 durable 状态的提交结果，并要求租约过期后重新投递时保护提交边界。
- [Sledge](https://github.com/torkbot/sledge)把 lease recovery 与 application-level fencing 放在同一条可靠执行路径上；其文档特别指出，旧 attempt 即使继续运行，也不能用旧 attempt key 覆盖新 attempt 的状态。

本轮没有引入工作流框架，只把上述可迁移的不变量补到现有本机 SQLite `TaskJournal`。

## 发现的缺口

原有恢复流程可以把失联任务标记为 `interrupted` 并让新 worker 接管，但步骤写入接口没有携带接管代次。于是存在以下竞态：

```text
旧 worker claim(epoch=1) -> 进程暂停
恢复器 interrupt -> 新 worker claim(epoch=2)
旧 worker 醒来 -> complete_step / complete_task
```

如果旧写入被接受，新的任务状态、步骤输出或最终产物可能被旧 worker 覆盖。租约过期本身不能撤销已经运行的 Python 代码，必须在提交点拒绝旧代次。

## 最小实现

- `runtime_tasks` 增加单调递增 `lease_epoch`，每次 `claim_task` 成功接管时递增；
- `run_steps` 保存本次 claim 的 epoch，并把它传给 `begin_step`、`complete_step`、`fail_step` 和 `complete_task`；
- 提交接口在 SQLite `BEGIN IMMEDIATE` 事务中校验当前 epoch，不一致时抛出 `TaskLeaseLostError`；
- 旧 worker 的迟到提交只会被拒绝/放弃，不会改写新 worker 的 checkpoint；
- 仍保持现有单机边界：外部副作用如果已经在 fencing 前发生，仍必须依赖业务幂等键、结果查询或人工确认，不能宣称 exactly-once。

## 验收证据

- 新增 `test_stale_worker_cannot_commit_after_lease_recovery`，先让旧 worker 获得 epoch 1，再恢复并由新 worker 获得 epoch 2，验证旧 worker 的步骤提交和最终提交均被拒绝；
- `tests/test_task_journal.py tests/test_presentation_service.py tests/test_mcp_server_presentations.py`：`30 passed, 1 warning`；
- 仓库级 `scripts/check.ps1`：后端 `1003 passed, 1 warning`；前端生产构建通过、7 组测试共 `43 passed`；Tauri `cargo check --locked` 通过；
- 当前已知 warning 仍是 Starlette/httpx 弃用提示。

## 边界

这项修复证明的是“旧 worker 不能覆盖 durable journal”，不是“旧 worker 的外部副作用从未发生”。真正的邮件、日历、WPS 或其他不可逆写入仍必须由各自的业务幂等键和确认门保护；跨主机部署也不在当前本机 PC 范围内。
