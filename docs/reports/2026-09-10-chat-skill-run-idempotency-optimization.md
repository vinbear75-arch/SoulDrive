# 聊天自动 Skill 运行记录幂等优化

日期：2026-09-10

## 结论

本轮修复了聊天重试与自动 Skill 运行记录之间的幂等缺口。聊天接口原本已经用完整输入摘要和 request receipt 保护用户消息/助手回答，但 `_learning_context_for_query()` 在每次进入 Skill 匹配时都无幂等键地创建 `skill_runs` 记录。第一次模型生成失败、receipt 被释放后，客户端使用同一 `request_id` 重试时，会再次追加一条计划记录。

现在聊天请求为自动生成的 Skill 计划传入 `chat-skill:{chat_request_id}`，复用 `LearningStore.record_skill_run()` 已有的唯一幂等键、输入摘要冲突检测和 replay 语义。同一请求重试会返回原 `run_id`，不会创建第二条记录；不同请求仍然可以产生不同运行记录。

## 外部实践对照

- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 明确要求中断前的副作用必须幂等，因为恢复会从节点开头重新执行；不能在恢复路径无条件创建新记录。
- [LangGraph Functional API](https://docs.langchain.com/oss/javascript/langgraph/functional-api) 将重执行、checkpoint 和幂等键放在同一恢复语义下，建议用 idempotency key 或先检查既有结果避免重复副作用。
- [LangGraph Thinking in LangGraph](https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph) 将长流程拆成可恢复步骤，并把失败、重试和状态持久化作为流程的一部分。SoulDrive 当前只补齐聊天→Skill 计划这一条本机 durable 边界，没有引入新的 Agent Runtime。

## 失败优先验证

- 新增 `McpServerLearningTests.test_chat_skill_context_reuses_run_for_stable_request_key`：两次使用同一稳定 request key 构建相同聊天 Skill 上下文，断言返回相同 `run_id` 且持久化表只有一条运行记录。
- 修复前测试在导入函数调用阶段失败：`_learning_context_for_query()` 不接受稳定运行幂等键；这固定了缺口，而不是只验证新代码的成功路径。
- 修复后定向 Skill/聊天回归：`3 passed, 1 warning`。
- 记忆、学习、聊天和 Skill 组合回归：`140 passed, 1 warning`，耗时 `157.56s`。

## 实现

- `chat_stream()` 沿用已有 `request_id`；无 request_id 时使用本次 trace id，因此普通新请求不会意外复用历史运行记录。
- `_learning_context_for_query()` 增加可选 `skill_run_idempotency_key`，在显式选择和自动匹配两条 Skill 计划路径都传给 `record_skill_run()`。
- 没有改变 Skill 执行权限：当前仍只生成 `plan_only` 短契约，原始脚本不会因重试而执行。
- 没有改变显式 `/learning/skills/{skill_id}/runs` API 的既有幂等键和冲突语义。

## 边界

- 这是单个本地工作区内的应用层幂等；它不是跨多个工作区、多个 sidecar 或分布式 worker 的全局 exactly-once 保证。
- 如果同一 request key 的 Skill、查询或计划内容发生变化，`record_skill_run()` 会按已有摘要校验拒绝冲突，而不是静默复用错误计划。
- 运行记录创建成功但进程在回答持久化前崩溃时，恢复会复用同一计划记录；最终状态仍由聊天成功/失败收尾逻辑更新，模型生成本身不声称 exactly-once。
- 本轮没有把对话历史改造成独立 episodic memory，也没有新增 LangGraph/Temporal 依赖；后续只有在现有耐久边界出现可复现缺口时才继续扩展。
