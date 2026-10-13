# 2026-09-14 会话绑定记忆写入优化

日期：2026-09-14  
范围：`/reflection/draft`、`/learning/memories/runtime` 的持久会话与项目/工作流范围绑定。

## 1. 真实缺口

`/chat` 已经把 `session_id` 的项目与 workflow 角色作为权威来源，但记忆写入入口没有完全沿用同一契约：

- 反思草稿可以从一个 `workflow_id = null` 的通用会话提交 `workflow_id = paper_review`，候选随后会被写入论文窗口命名空间；
- 运行时偏好保存可以把项目 A 会话与项目 B 请求组合，或使用未验证的会话标识作为来源；省略项目时也没有从已绑定会话推导项目。
- `workflow_fact` 候选即使没有 `workflow_id` 也能通过通用候选校验，最终只能在读取门被丢弃，形成不可复用的孤儿记忆。
- 全局来源会话也可以被反思请求临时指定为项目 A，绕过 `/chat` 与运行时记忆已有的全局/项目一致性约束。
- 任意非空的未知 `workflow_id` 可以进入候选/存储层；它不会被合法窗口读取，形成另一类不可复用的孤儿记忆。

这会让“来源会话”“记忆项目”和“记忆窗口”三者不一致。它不是模型能力问题，而是写入前的 durable scope contract 缺失。

## 2. 对照的公开机制

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 将 thread-scoped checkpoint 与可恢复状态绑定；本地将持久会话视为窗口角色的权威边界。
- [LangGraph Stores](https://docs.langchain.com/oss/python/langgraph/stores) 以 namespace 组织长期记忆；本地要求 workflow memory 的 namespace 不能由请求临时覆盖。
- [Mem0 metadata filters](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 将 metadata 过滤作为记忆检索边界；本地把同样的范围原则前移到写入入口。

这些资料只支持机制取舍，不代表 SoulDrive 直接引入外部框架。

## 3. 失败优先与最小实现

先加入五个失败优先反例：

1. 通用源会话 + `workflow_id=paper_review` 的反思请求，修复前返回 200；现在返回 409，要求选择已绑定论文窗口的会话。
2. 项目 A 会话 + `project_id=project-b` 的运行时记忆请求，修复前返回 200；现在返回 409，且不会创建审核记录或归档记忆。
3. 缺少 `workflow_id` 的 `workflow_fact` 候选，修复前可进入写入层；现在在候选校验和 `LearningStore` 防线均被拒绝。
4. 全局源会话 + `project_id=project-a` 的反思请求，修复前返回 200；现在返回 409。
5. `workflow_id=unknown_window` 的候选/遗留记忆，修复前可写入且健康检查不报警；现在写入拒绝，健康检查同时覆盖当前和历史版本。

实现保持局部：

- `/reflection/draft` 拒绝通用源会话临时升级为业务窗口，仍允许已绑定 workflow 的会话省略请求 workflow 并使用持久角色；
- `/learning/memories/runtime` 在幂等键 claim、审核查重、接受和归档之前校验会话存在及项目一致性；省略请求项目时从会话绑定项目推导；
- 运行时入口不再让客户端提供的项目字段参与幂等摘要或持久化范围。
- 反思来源会话要求显式项目与会话项目严格相等；全局会话只能保持全局范围，不能临时升级为项目范围。
- 候选校验与 `LearningStore` 存储层都要求 `workflow_fact` 使用现有支持目录中的 workflow namespace；`memory_health()` 对当前记忆和历史版本都报告未知/缺失 namespace，读取门之外再加写入与诊断不变量。

## 4. 验证

- 反思定向回归：`11 passed, 107 deselected, 1 warning`。
- 运行时记忆回归：`8 passed, 109 deselected, 1 warning`。
- 候选/记忆角色定向：`11 passed`；LearningStore workflow/review 回归：`14 passed, 69 deselected`。
- 学习/会话/记忆组合回归：`252 passed, 1 warning`。
- 既有运行时测试补齐了真实持久会话 manifest，验证新的 durable contract，而不是放宽生产代码接受不存在的来源会话。

本轮完整工程门已通过：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。该门也不等于真实 Tauri/WPS UI、发行包启动、外部服务或模型答案准确率验证。

## 5. 边界

这只保证本地单用户服务内的会话/项目/窗口写入一致性，不提供身份认证、跨主机 exactly-once、自动冲突合并或模型级记忆正确性。没有 `session_id` 的旧兼容调用仍可创建无来源的显式项目记忆；这类调用不能声称具备会话来源追溯。
