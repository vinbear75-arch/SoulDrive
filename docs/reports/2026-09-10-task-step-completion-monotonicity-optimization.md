# TaskJournal 步骤完成态单调性优化记录

日期：2026-09-10  
范围：`TaskJournal.complete_step` / `fail_step` 的迟到回调与 durable checkpoint

## 外部实现对照

- [Restate](https://github.com/restatedev/restate)把“失败后恢复并避免重新执行已完成步骤”作为 durable execution 的核心语义。
- [Inngest SDK specification](https://github.com/inngest/inngest/blob/main/docs/SDK_SPEC.md)要求按稳定 Step ID 记忆成功结果或最终错误；已有 memoized step 不应再次作为新步骤上报。
- [Hatchet durable tasks](https://github.com/hatchet-dev/hatchet/blob/main/frontend/docs/pages/v1/durable-tasks.mdx)要求从 checkpoint 重建状态并跳过已完成操作，副作用不能因为恢复路径再次执行。

这些实现共同要求步骤状态至少满足“完成后不回退”的单调性。SoulDrive 原实现虽然有步骤状态、输出和租约 epoch，但 `complete_step` 与 `fail_step` 都没有先检查当前步骤状态；同一租约内的迟到 worker 回调仍可能覆盖已完成 checkpoint。

## 失败回归与根因

新增 `TaskJournalTests.test_completed_step_is_monotonic_against_late_complete_or_failure`：先成功保存 `{"value": "first"}`，再模拟迟到失败和迟到成功提交。旧实现最后留下 `{"value": "late"}`，证明步骤结果不是单调 checkpoint。

## 最小修复

- `complete_step` 查询当前步骤状态：已完成时直接保持原 checkpoint；未知步骤报错；非 `running` 步骤拒绝完成。
- `fail_step` 查询当前步骤状态：已完成时直接返回，不把成功步骤改成失败；未知步骤报错。
- 保留原有 lease epoch fencing；本轮不把本地 SQLite 状态扩大解释为外部系统 exactly-once。

## 验证

- 修复前新增回归失败：迟到 `complete_step` 覆盖了首个成功输出。
- 修复后 `tests/test_task_journal.py`：`13 passed`。
- PPT 定向回归与 TaskJournal 合计 `34 passed`；本轮仓库全量 `scripts/check.ps1`：后端 `1076 passed, 1 warning`，前端生产构建与 7 组 UI 测试 `48 passed`，Tauri `cargo check --locked` 通过，退出码为 0。

## 边界

这只保证本地 durable journal 的完成状态不会被迟到回调回滚或覆盖；如果外部 WPS、SMTP、日历或其他服务已经在 checkpoint 之前完成副作用，仍须依赖业务幂等键、结果查询、人工确认和现有 at-most-once 边界。
