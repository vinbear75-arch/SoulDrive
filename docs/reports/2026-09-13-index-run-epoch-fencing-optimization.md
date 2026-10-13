# 索引执行代际栅栏优化

日期：2026-09-13

## 发现

上一轮已经为 `/index/run` 增加持久化请求回执和 worker 身份复核，但仍有一个更底层的 durable execution 风险：旧 worker 可能在新 run 已经开始后迟到写入状态。原来的 `update_indexing_status()` 只合并字段，不验证写入者属于哪个 run，因此旧 worker 的 `completed`、`failed` 或进度事件可能覆盖新 run 的状态。

## 公开实践对照

- [Restate Architecture](https://docs.restate.dev/references/architecture) 将每次执行尝试附带单调递增 epoch，并拒绝已被替代尝试的迟到事件；这里只借鉴“旧尝试事件必须被栅栏拒绝”的本地原则，不引入 Restate 的分布式日志、Raft 或跨机器 exactly-once。
- [Temporal Workflow ID and Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid) 区分具有业务含义的 Workflow ID 与可变化的 Run ID，并保证同一 Workflow ID 同时只有一个开放执行；SoulDrive 将当前索引 `run_id` 作为本机状态 owner 的执行身份，业务幂等键仍由 `IndexRunJournal` 管理。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 将 checkpoint/store 作为恢复依据，并要求可重执行步骤的副作用具备幂等边界；本轮对应的是“状态写入也必须带执行上下文”，而不是把整个索引器改造成 Agent runtime。

## 实现

1. `runtime_state.update_indexing_status()` 新增可选 `expected_run_id`。当前状态的 `run_id` 与期望值不一致时，忽略这次迟到更新并返回未改变的状态。
2. `DriveIndexer.sync_workspace()` 的扫描、解析、失败、进度和完成状态更新全部携带本次 `run_id` 栅栏；首个带同一 run ID 的初始化更新仍可建立新 run，锁定状态不允许旧 run 重新打开索引。
3. `/index/run` 既有新 run 分配、持久化回执和 PID/启动时间 owner 复核保持不变；本轮只收紧状态事件的写入边界，没有扩大到跨机器协调。

## 失败优先与验收

- 旧实现反例：先写入 `new-run`，再让带 `old-run` 的完成事件写入；旧实现最终会把状态变成 `old-run`。
- 修复后覆盖：旧 run 不能覆盖当前 run；工作区锁定后旧 run 不能重新打开索引；正常索引 scope/worker/runtime/API/备份回归均保持通过。
- 定向组合回归：`60 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1267 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机 runtime state 的 run 身份栅栏，不是跨进程共享日志、租约服务或分布式 epoch 共识。
- `expected_run_id` 只能拒绝带有正确栅栏参数的旧调用；若未来新增索引写入点，必须同时传递当前 run 身份，否则仍需代码审计和测试覆盖。
- 该证据证明状态一致性和回放边界，不证明索引解析、embedding、GraphRAG 召回率、答案准确率、真实 Tauri/UI/E2E 或安装包行为。
