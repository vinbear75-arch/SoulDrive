# Skill durable 运行终态单调性优化

日期：2026-09-10

## 结论

同一聊天 request key 现在会复用同一条 `skill_runs` 记录，但这会暴露一个必须同时处理的恢复边界：旧请求线程可能在新请求已经成功后才提交失败回调。原 `update_skill_run()` 无条件更新状态，迟到的 `failed` 会把 `completed` 覆盖掉。

现在 Skill 运行终态采用单调保护：已完成记录不接受后续 `failed` 或 `planned` 降级；SQL 更新条件也要求目标记录尚未完成，因此不同 `LearningStore` 连接之间的迟到写入同样不会覆盖完成态。失败状态仍可由后续重试升级为完成态，保持本机聊天重试的恢复路径。

## 外部实践对照

- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 说明恢复会重新执行中断所在节点，副作用必须幂等，不能让旧执行重复写入不可逆状态。
- [LangGraph Functional API](https://docs.langchain.com/oss/javascript/langgraph/functional-api) 要求可重执行任务把副作用放入可检查点的任务，并用幂等键或既有结果检查避免重复写入。
- [LangGraph Fault Tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) 将失败、重试和恢复视作运行时状态，而不是简单覆盖式日志写入。SoulDrive 采用与当前 SQLite/加密 payload 结构相容的最小状态门，没有引入新的运行时依赖。

## 失败优先验证

- 新增 `LearningStoreTests.test_completed_skill_run_cannot_be_downgraded_by_late_failure`：先由一个连接写入 `completed`，再由另一个连接提交旧 `failed` 回调。
- 修复前回归得到 `failed != completed`，证明问题不是测试空转。
- 修复后该回归和 Skill 幂等、并发 claim、聊天自动 Skill 重试组合回归均通过；相关组合本轮共 `4 passed, 1 warning`，此前学习/聊天/Skill 组合为 `140 passed, 1 warning`。

## 实现

- `update_skill_run()` 在读取到 `completed` 且请求状态较低时直接返回已完成记录。
- 持久化更新增加 `(? = 'completed' OR status <> 'completed')` 条件，覆盖另一个连接在读取后抢先完成的竞争窗口。
- 发生条件更新未命中时重新读取当前行并返回实际终态，不向调用方伪造旧写入已经成功。

## 边界

- 这是运行记录状态的单调保护，不是模型生成 exactly-once；模型调用或助手消息仍由聊天 request receipt、完整输入摘要和消息 ID 负责。
- `failed -> completed` 仍被允许，因为同一幂等请求的后续重试可能已经成功；本轮没有新增 attempt 表或分布式租约。
- 多进程共享同一工作区时仍需要更强的 worker lease/attempt fencing 才能表达“哪一次尝试拥有最终收尾权”；当前本机单用户 sidecar 先用 SQLite 条件更新覆盖已验证的迟到降级问题。
