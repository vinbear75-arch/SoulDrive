# Durable 请求结果在 UI 的可见性优化

日期：2026-09-13

## 发现

公开 durable execution 实践把“稳定请求身份、可回放结果、进行中状态和失败状态”视为同一条客户端契约，而不是只在服务端去重。对照后检查 SoulDrive 的现有接口，发现项目、会话、联系人、邮件/日程草稿和索引启动接口已经返回或能够返回 `replayed/status`，但 UI 多数只读取业务对象；PPT 入口虽然返回 `worker_started`，UI 却固定显示“正在提取”。网络重试或重复点击因此会把回放误报成新建，或把已完成任务误报成运行中。

## 公开实践对照

- [Inngest Durable Endpoints](https://www.inngest.com/docs/learn/durable-endpoints)：同步成功路径和异步重试路径都需要可观察的最终结果；本地只借鉴状态显式化，不引入云端 durable endpoint。
- [Inngest Idempotency](https://www.inngest.com/docs/guides/handling-idempotency)：稳定 key 要绑定输入，重复调用应返回同一业务结果；本轮沿用已有本地 key 和持久化对象，不新增第二套身份。
- [Open WebUI 后台知识处理失败问题](https://github.com/open-webui/open-webui/issues/27987)：后台失败若没有状态、错误或重试入口，客户端会停在不可解释的处理中；本轮把已有 warning/replayed/worker 状态呈现出来。

## 实现

1. `/projects` 创建响应补充布尔 `replayed`，与会话、联系人和草稿接口保持一致；项目、会话、联系人、邮件/日程草稿的现有状态区域显示回放语义。
2. 索引启动条显示“任务已提交”或“已回放已有索引任务”，失败仍显示启动失败；不改变索引任务的持久化回执和 worker 栅栏。
3. PPT `create_plan()` 在已有任务回放时只返回传输层 `replayed=true`，不把该字段写入 TaskJournal；HTTP 返回 `replayed` 与 `worker_started`。UI 对已完成大纲、已有运行任务和等待 worker 分别给出提示。
4. 只补状态消费和契约断言，没有新增轮询、队列或自动重试，也没有把审计 warning 当作业务失败。

## 失败优先与验收

- 项目/API 定向：`26 passed, 1 warning`。
- PPT 服务/API 定向：`31 passed, 1 warning`，覆盖回放元数据不落盘和完成态不启动 worker。
- 前端项目范围静态 UI：`20 passed`；学习状态 UI：`4 passed`；生产构建成功。
- 完整 `scripts/check.ps1`：后端 `1289 passed, 1 warning`；前端构建及 7 组测试通过；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机单 sidecar 的状态契约和静态/构建级 UI 消费，不是真实 Tauri 网络断线、视觉回归或端到端点击证明。
- `replayed` 仍只代表本地稳定身份对应的已有结果；不宣称跨进程/跨机器 exactly-once，也不覆盖外部 WPS、SMTP、日历或文件系统副作用。
- PPT `worker_started=false` 是“本次请求没有启动新 worker”的事实，不能单独证明另一个进程一定健康；实际任务状态仍以持久化 TaskJournal 和后续查询为准。
