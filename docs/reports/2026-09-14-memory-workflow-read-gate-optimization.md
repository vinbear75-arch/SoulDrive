# 2026-09-14 研究窗口记忆读取闸门优化

日期：2026-09-14  
范围：`workflow_fact` 的最终读取边界、三个研究窗口的记忆命名空间。

## 1. 本轮发现的真实缺口

SoulDrive 已在 LearningStore 查询层按 `workflow_id` 隔离 `workflow_fact`，但 `gate_memory_context()` 本身没有接收窗口身份。若未来的检索器、测试替身或旁路调用直接把候选交给 gate，未绑定窗口的事实仍可能进入 prompt；当前 gate 也无法把“指定窗口”写入读取决策摘要。

这会削弱“论文精读、实验对比、会议行动项确实有不同作用”的服务端契约。前端选择窗口不是足够的安全边界。

## 2. 对照的开源机制

- [Mem0 memory filters](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 将 entity/session 等 metadata 作为检索过滤条件；本地对应为显式 `workflow_id` namespace，而不是依赖 query 文字。
- [Letta memory blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/) 把当前任务可见的 memory block 作为可切换上下文；本地采用更小的 fail-closed 读取门，不引入新的 Agent runtime。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 区分 thread/checkpoint 与长期 store；本地继续保持 LearningStore 为事实源，只把工作流身份作为读取时的显式绑定。

这些项目的 namespace 机制不能直接证明 SoulDrive 的业务答案质量；本轮只借鉴“最终可见上下文仍要带 namespace”的边界。

## 3. 最小实现

`gate_memory_context()` 新增可选的 `workflow_id`，并对 `workflow_fact` 执行：

1. 没有请求窗口时拒绝，原因 `workflow_memory_namespace_required`；
2. 记忆缺少绑定或绑定到其他窗口时拒绝，原因 `workflow_memory_namespace_mismatch`；
3. 只有候选绑定值与请求窗口完全相等时才继续做敏感内容、相关性、冲突和预算判断；
4. `workflow_id` 进入 `memory_read_gate_v1` decision digest，切换窗口不会复用同一份无身份读取结论；
5. 聊天运行时和 `/learning/search` 都从服务端请求上下文向 gate 透传窗口身份，UI 不负责安全过滤。

## 4. 失败优先与验证

先加入两个反例：无窗口调用仍接受论文记忆；指定 `meeting_actions` 时旧 gate 不支持参数。旧实现结果为 `2 failed`，确认缺口真实存在。

修复后：

- `tests/test_memory_runtime.py`：`16 passed`；
- LearningStore、MCP 学习 API、memory runtime 组合：`210 passed, 1 warning`；
- warning 仍为 Starlette/httpx 测试客户端弃用提示。

第一次完整门禁还发现一个兼容性回归：新 namespace 检查早于恶意正文检查，既有安全用例的诊断从 `untrusted_instruction` 变成了 namespace 缺失。已将安全/敏感内容检查置于 namespace 检查之前；`tests/test_memory_gate.py tests/test_memory_runtime.py` 当前 `32 passed`。

截至本轮最新完整工程门：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。

## 5. 明确边界

- 这证明的是窗口命名空间和最终读取拒绝，不证明三个窗口的模型回答准确率、检索召回率或自动 workflow memory 抽取质量。
- `user_preference`、`project_fact` 和项目约束继续按既有 global/project 规则共享或隔离；本轮没有把所有记忆都错误地限制为窗口私有。
- 这不是 Letta 的完整 memory block runtime，也不是跨存储 exactly-once；LearningStore、向量投影、会话归档和 GraphRAG 仍有各自的事实/派生职责。
