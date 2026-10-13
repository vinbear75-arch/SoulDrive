# TaskJournal durable step 尝试历史优化

日期：2026-09-13

## 发现

SoulDrive 的 `TaskJournal` 已经能保存任务输入、步骤输出、租约 epoch、稳定 step 幂等键，并从最后完成步骤恢复；但 `runtime_task_steps` 只有当前 `attempt_count` 和当前 `error`。重试进入 `begin_step()` 时会清空当前错误，导致上一次失败原因无法在恢复后查询，也无法形成可审计的执行经历。

## 公开实践对照

- [DBOS Workflow Tutorial](https://docs.dbos.dev/python/tutorials/workflow-tutorial) 将每个 durable step 的检查点作为恢复边界，工作流中断后从最后完成步骤继续。
- [DBOS Workflow Recovery](https://docs.dbos.dev/production/workflow-recovery) 强调恢复需要识别中断、避免重复恢复，并按执行者边界接管任务。
- [DBOS Concurrent Executions](https://docs.dbos.dev/explanations/concurrent-executions) 将 step 边界的并发冲突和已持久化结果视为正确性边界；本地继续使用已有 lease epoch 和单机 SQLite，不引入分布式控制面。

## 实现

1. 在现有任务库增加 `runtime_task_step_attempts`，以 `(task_id, step_name, attempt)` 唯一标识一次尝试，保存稳定 step 幂等键、lease epoch、开始/结束时间、终态和错误摘要。
2. `begin_step()` 为每次实际执行创建 `running` 记录；`complete_step()`、`fail_step()` 分别将当前尝试落为 `completed`/`failed`。
3. 孤儿恢复或中断会将正在运行的尝试落为 `interrupted`；取消导致的边界终止记录为 `cancelled`，不把“未执行”伪装成成功。
4. `get_task()` 和既有 `public_task()` 投影 `attempt_history`，因此任务/PPT API 可以查询恢复轨迹；PPT 面板增加可展开的只读“执行轨迹”，展示步骤状态、尝试次数和错误摘要；步骤最终输出仍只保留在原有加密 checkpoint 中。
5. 这是对现有本地持久化的增量表创建，未引入 Temporal/DBOS/Redis，也未把失败文本自动写入长期记忆；失败经历留在对应任务的 durable execution 记录中，需要人工确认后才可能成为学习资产。

## 失败优先与验收

- 新回归先于实现运行时真实失败：首轮重试完成后读取 `attempt_history` 报 `KeyError`，证明旧实现确实丢失尝试轨迹。
- 实现后 TaskJournal 专项 `17 passed`；任务/PPT/会话/sidecar 组合 `89 passed`。
- 回归覆盖：失败后重试并重开数据库仍保留 `failed → completed` 两次尝试；两次尝试复用同一个 step 幂等键；孤儿 worker 恢复记录 `interrupted` 及原因；已有完成检查点仍不会被迟到失败覆盖。
- 完整 `scripts/check.ps1` 通过：后端 `1229 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。
- 前端在加入执行轨迹后重新完成生产构建和 7 组脚本测试，仍为 `49 passed`；完整工程门随后再次通过。

## 边界与下一步

- 尝试历史是执行审计和恢复证据，不是外部系统 exactly-once；外部邮件、日历、WPS 或文件副作用仍必须依靠业务幂等键、提供方查询或人工确认。
- 旧任务在新增表之前的历史不会凭空补齐；升级后新执行会开始记录，旧步骤仍可按原有 checkpoint 恢复。
- 当前只保存错误摘要和状态，不保存每次尝试的完整输出副本，避免重复保存敏感/大型产物；若未来需要失败诊断，应先确认保留期限、脱敏和用户可见范围。
- 下一步优先评估把 `attempt_history` 接入任务诊断/项目摘要的最小只读视图，而不是把全部执行错误自动注入 RAG 或长期记忆。
