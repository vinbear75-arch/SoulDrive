# 索引启动请求幂等与持久化回执优化

日期：2026-09-13

## 发现

`POST /index/run` 以前只用当前 sidecar 进程内的 `indexer_process` 指针防止重复启动。网络请求在 worker 已启动后超时，用户再次点击或客户端重试时，接口没有持久化请求身份，也没有办法区分“同一次索引请求”与“新的重建请求”。worker 指针在 sidecar 重启后还会丢失。

## 公开实践对照

- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http) 将 `Idempotency-Key` 绑定到持久化 invocation，并在重复请求时返回同一结果；[Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 还强调 journal、重放和执行 epoch 的区别。
- [Prefect caching](https://docs.prefect.io/v3/concepts/caching) 将可复用结果绑定到持久化 cache key，并区分输入、代码和隔离级别；本地只采用“输入摘要 + 持久结果”的小范围做法，不引入 Prefect 服务。

## 实现

1. 新增 `core/index_run_journal.py`，在 `runtime/index_run_journal.sqlite` 保存不含正文/密钥的请求回执：`idempotency_key`、索引输入摘要、稳定 `run_id`、状态、错误和时间。
2. `/index/run` 接受可选 `Idempotency-Key`：
   - 同 key、同工作区、同授权输入重试时复用原 `run_id`，活动运行返回 `replayed=true`，终态返回同一终态；
   - 同 key 但工作区/授权输入摘要不同返回 `409`；
   - 不提供 key 的旧客户端仍走原接口形态，并返回本次 `run_id`。
3. runtime state 的 `indexing.idempotency_key` 与 receipt 做投影；runtime/status 读取到 `completed/failed` 时同步终态回执，确保 sidecar 重启后仍能回放已知终态。
4. 运行状态额外持久化 `worker_pid` 与 `worker_started_at`。若 sidecar 内存中的进程指针丢失，启动/备份门会通过本机进程的 PID、启动时间、命令行和工作区参数重新核验；无法读取进程身份时按仍占用处理，避免重复启动。
5. 索引状态更新增加 `expected_run_id` 执行代际栅栏；旧 worker 的迟到进度或终态事件不能覆盖新 run，也不能在工作区锁定后重新打开索引状态。详见 `docs/reports/2026-09-13-index-run-epoch-fencing-optimization.md`。
6. 备份预检把 `runtime/index_run_journal.sqlite` 作为权威 durable receipt 范围，避免只备份资料和索引而丢失请求身份。
7. 前端“重建索引”入口现在为一次逻辑 run 生成稳定的 `Idempotency-Key`，网络异常重试复用同一 key；观察到该 run 进入 `completed/failed/blocked/idle` 终态后才清理，下一次新建索引再生成新 key。UI 不增加额外配置，诊断仍只展示状态。

## 失败优先与验收

- 旧行为反例先证明同 key 会再次进入启动路径，且输入上下文变化不会冲突。
- 修复后定向覆盖：同键单次启动、同键重放、同键异输入 `409`、回执库关闭重开、runtime completed 终态投影、sidecar 指针丢失后的持久化 worker 身份复核和旧 run 迟到写入栅栏；索引/备份/API/运行时组合 `60 passed, 1 warning`。前端静态检查确认重建按钮携带输入绑定 key。
- 完整 `scripts/check.ps1` 通过：后端 `1267 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单 sidecar 的持久化请求去重，不是跨机器分布式 exactly-once；索引器内部仍需依赖现有项目/来源幂等清理逻辑。
- 当前 worker 身份是本机 fail-closed 的存活复核，`expected_run_id` 是本机状态写入栅栏，不是独立 owner/lease fencing；它能覆盖状态文件仍在且 PID/启动时间可验证的孤儿 worker，并拒绝已知旧 run 的迟到事件，但不能对跨机器执行、PID 复用窗口或无法读取的外部 supervisor 提供 exactly-once 证明。
- 失败/中断后的新重建仍应使用新的 key；同 key 只表示同一请求重试，不表示用户主动要求重新生成。
- 该结果不证明索引解析、embedding、GraphRAG 质量、真实 Tauri/UI/E2E 或发布包行为。
