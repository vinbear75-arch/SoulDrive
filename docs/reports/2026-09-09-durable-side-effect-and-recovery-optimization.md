# SoulDrive 外部副作用预留与任务恢复安全优化

日期：2026-09-09

## 结论

本轮没有引入新的 durable execution 框架，而是修复两个由当前本机代码直接暴露的状态机缺口：

1. 邮件投递预留原来是“先查询、再插入”，并发请求可能同时看到空记录，失败请求以 SQLite 唯一约束异常结束；现在在同一 SQLite 事务内用 `BEGIN IMMEDIATE` 串行化检查和预留，竞争结果稳定为 `reserved` 或 `conflict`。
2. TaskJournal 恢复器原来会在 owner 进程仍存活时，仅因 900 秒没有 checkpoint 更新就接管任务；长同步步骤可能因此被重复执行。现在存活 owner 不再因为陈旧 checkpoint 被回收，只有进程不可见时才进入现有的 `interrupted` 恢复路径。

这两项修复都不把外部邮件发送宣称为 exactly-once：SMTP 已经接受邮件、但进程在保存 `sent` 前崩溃时，结果仍可能未知；本轮保留“外部副作用必须有业务幂等键且不自动盲重试”的边界。

## 外部实践对照

- [Temporal 固定次数重试](https://github.com/temporalio/documentation/blob/main/docs/design-patterns/fixed-count-retries.mdx)明确指出，worker 在外部 API 调用成功、结果尚未记录时崩溃，后续重试可能重复调用，因此应使用由 workflow/activity 标识派生的稳定幂等键。
- [Temporal Activity heartbeat](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/detecting-activity-failures.mdx)把 heartbeat 作为长活动的存活/进度信号，并提醒超时重试前必须考虑外部副作用的幂等性。
- [Restate 安全入口重试](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)把幂等键作为入口重试合并的基础；SoulDrive 只迁移“原子预留 + 稳定键”的不变量，不引入分布式服务。

## 当前缺口与最小改动

### 邮件 delivery reservation

`ProjectCatalog.reserve_email_delivery()`原先先读取 `email_deliveries`，再执行 `INSERT`。两个本机进程/线程可以同时通过读取分支，随后一个写入成功，另一个撞上 `PRIMARY KEY` 或 `UNIQUE` 约束；这不是用户可理解的幂等结果，也会让 HTTP 层暴露 500。

当前改动：

- 用 `BEGIN IMMEDIATE` 将“按 idempotency key 查冲突 → 按 artifact 查既有状态 → 插入/复用/拒绝”放进同一 SQLite 写事务。
- 保留既有语义：已发送的同键请求返回 `sent`；不同键绑定同一草稿返回 `conflict`；不同草稿复用同一键返回 `conflict`；失败之后的新键仍是显式重试边界。
- 没有自动重试 SMTP，也没有把 `sending` 伪装成 `sent`。

### TaskJournal recovery

`recover_orphaned_tasks()`原先以 `updated_at` 的 900 秒阈值辅助 owner 进程判断。对 PPT 渲染、WPS 交互或长模型/文件步骤来说，状态只能在步骤边界写回；存活 worker 可能被误判为孤儿。

当前改动：

- owner 进程仍存活时直接保留当前 lease，不因 checkpoint 陈旧而接管。
- owner 进程不可见时继续走 `interrupt_task()`；该路径已经递增 `lease_epoch`，旧 worker 在新 worker 接管前后都不能提交步骤或最终结果。
- 这是本机单用户边界下的保守策略：活着但真正卡死的进程不会被自动抢占，需用户/运行时显式终止或恢复；不以自动接管换取潜在的重复外部副作用。

## 失败优先与验证

新增回归均先在旧实现上失败，再应用修复：

- `test_email_delivery_reservation_is_safe_for_concurrent_attempts`：旧实现出现只完成一个结果、另一线程进入异常的竞态；修复后并发结果为 `conflict` 与 `reserved`。
- `TaskJournalTests.test_live_worker_is_not_recovered_only_for_a_stale_checkpoint`：旧实现把当前进程 owner 的陈旧任务标为 `interrupted`；修复后保持 `running`。

定向验证：

- `tests/test_task_journal.py tests/test_presentation_service.py tests/test_project_catalog.py tests/test_topic5_drafts.py tests/test_email_connector.py`：`70 passed, 1 warning`。
- 唯一警告仍是环境中的 Starlette/httpx 弃用提示，不是本轮测试失败。

## 未完成边界

- SMTP 服务端的重复投递检测不由本地 SQLite 保证；进程在外部发送与本地落盘之间崩溃时，需要查询提供方或人工确认，不能盲目重发。
- 当前恢复器按 owner 进程存活做单机判断，尚未实现 Temporal 式跨进程 heartbeat、PID 重用识别或分布式协调；这些超出当前 PC 单用户边界，且需要新的故障注入评测。
- 本报告的专项结果不等于真实 SMTP、WPS、Tauri UI 或安装包交付验收。
