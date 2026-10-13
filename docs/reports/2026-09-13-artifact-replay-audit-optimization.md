# 草稿幂等回放审计副作用优化

日期：2026-09-13

## 发现

邮件草稿和日程草稿已经使用 artifact `idempotency_key` 防止重复创建，但路由只拿到了 artifact ID，不知道这次结果是首次创建还是回放。因此同一幂等键重试时，artifact 不重复，`artifact.*_created` 审计事件却会重复写入，审计流无法区分一次创建和网络重试。

## 公开实践对照

- [TanStack Workflow persistence](https://github.com/TanStack/workflow/blob/main/docs/guide/persistence.md) 把持久化事件日志作为 replay 的事实源，并要求并发写入有明确的 compare-and-swap 边界；本轮把回放状态作为创建结果的一部分返回，不再让路由猜测。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将 producer `dedupeKey` 与 durable event append 绑定；本地只借鉴“同一次业务写入的去重身份要传递到事件层”，没有引入新的队列或工作流运行时。
- [Stripe idempotent requests](https://docs.stripe.com/idempotency) 明确区分首次请求和相同 key 的回放，并要求同 key 不因重试产生额外可观察副作用；本地范围只覆盖草稿 artifact 与本机审计事件。

## 实现

1. `ProjectCatalog.create_artifact(..., return_status=True)` 在首次插入、同键回放和并发唯一键竞争回读时分别返回 `replayed=false/true`，保持旧调用默认返回字符串的兼容性。
2. 邮件草稿与日程草稿路由改为使用该状态；只有 `replayed=false` 才写 `artifact.email_draft_created` 或 `artifact.schedule_draft_created`。
3. 接口响应增加 `replayed` 标志，客户端可以把网络重试和真正创建区分开；同键异输入仍由 catalog 拒绝。

## 失败优先与验收

- 旧实现反例：同一幂等键连续创建同一邮件/日程草稿时，artifact 数量保持 1，但 `*_created` 审计事件出现 2 次。
- 修复后新增邮件、日程各一组审计计数回归，并保留原有同键回放/异输入冲突测试；项目目录、草稿和 API 定向 `66 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1271 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机 SQLite artifact 与本机审计事件之间的回放标识传递，不是跨存储事务；若 artifact 已提交而进程在审计写入前崩溃，恢复审计不会自动补写。
- 未把所有审计事件改成全局 dedupe，也未改变无幂等键调用的行为；真正外部发送仍由确认状态、邮件 delivery reservation 和发送幂等键保护。
- 该证据不证明跨进程 exactly-once、真实 SMTP/日历副作用、Tauri/UI/E2E 或审计日志远程复制一致性。
