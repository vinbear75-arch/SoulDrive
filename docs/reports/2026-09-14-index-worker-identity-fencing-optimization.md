# 索引 worker 身份传播与运行状态 fencing 优化

日期：2026-09-14

## 结论

本轮修复了索引链路中一个会让 durable 回执失真的身份旁路，以及一个启动竞态：

1. `/index/run` 生成的 API `run_id` 过去没有传给 `core.indexer_worker`；worker 内部再次生成 UUID，`expected_run_id` 栅栏会忽略真实 worker 的扫描/完成事件，正常 worker 退出还可能被父进程误判成异常退出。
2. 父进程在 `Popen` 返回后补写 `queued`，可能晚于 worker 已写入 `scanning/indexing/completed`；runtime state 过去没有同一 run 的状态单调保护，迟到的 `queued` 会回退运行状态。

现在启动命令携带 `run_id` 和 `idempotency_key`，worker 原样传入 `DriveIndexer.sync_workspace()`；索引运行状态按同一 run 的 `queued -> scanning -> indexing -> terminal` 单调更新，迟到状态被忽略。不同 run 仍可正常初始化，锁定态也不会被旧 worker 重新打开。

## 外部实践对齐

- [Temporal Workflow ID and Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx) 区分业务级 Workflow ID 与单次执行 Run ID，并保证同一开放业务身份不会产生重复执行；本地索引将 API 生成的 run identity 传递给实际 worker，避免一个请求出现两个执行身份。
- [Temporal Workflow Execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflow-execution.mdx) 将状态转移作为持久执行的一部分；本项目用 SQLite/runtime JSON 的轻量状态栅栏实现同一原则，不引入 Temporal 服务。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md) 强调入口重试必须绑定幂等键，避免连接丢失时重复执行；索引请求的幂等键现在随 worker 一起传播并继续由本地 receipt 记录。

## 实现边界

1. 只绑定本机 API 与本机 sidecar worker 的 `run_id`，不宣称跨机器 worker、跨进程 exactly-once 或任务调度服务。
2. 同一 run 的终态 `completed/failed/blocked/idle` 不再被其他状态覆盖；新 run 必须使用新的 `run_id`，不会被旧 run 的 fencing 拦截。
3. 文件事实源、SQLite receipt 和 runtime state 仍分别承担事实、幂等回执和运行投影职责；本轮不把 runtime JSON 当成索引内容事务。

## 回归证据

- 失败优先：worker CLI 身份传播测试修复前因未知参数失败；状态竞态测试修复前将 `scanning` 回退为 `queued`。
- 运行状态/索引幂等回归：`12 passed, 1 warning`。
- worker 与索引范围回归：`25 passed`；MCP 索引入口回归：`20 passed, 1 warning`。
- 完整工程门禁：后端 `1517 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；`compileall` 与锁定 Cargo 通过。真实打包 sidecar 启动、WPS/Tauri UI 和多进程压力仍不由该门禁覆盖。
