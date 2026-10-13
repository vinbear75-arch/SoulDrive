# 2026-09-14 记忆最终范围 Prompt 边界优化

日期：2026-09-14  
范围：`_learning_context_for_query()` 的范围元数据、`build_learning_context()` 最终注入边界。

## 1. 本轮发现的真实缺口

生产聊天路径已经在 LearningStore 和 `gate_memory_context()` 中校验项目、全局策略、研究窗口和有效时间，但最终 prompt 组装器此前只校验敏感内容与指令型文本。若未来某个调用方直接构造或错误复用 `learning_context`，错项目 memory capsule 仍可能被当作已确认长期记忆拼入模型上下文。

这不是替代读取门，而是最后一个不可信数据进入 prompt 的边界必须再次确认作用域；没有作用域元数据的预渲染 capsule 在显式范围上下文中不能被信任。

## 2. 对照的开源机制

- [LangGraph Stores](https://docs.langchain.com/oss/python/langgraph/stores) 用 namespace/key 组织跨线程长期记忆；作用域应随运行时上下文传递，而不是只靠调用方约定。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 区分 thread-scoped 状态与可恢复的长期存储；本地把 `project_id/workflow_id/include_global_memory` 作为注入上下文的显式元数据。
- [Mem0 metadata filters](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 要求搜索过滤条件参与结果范围；本地在最终 prompt 入口再拒绝与该范围不一致的项目/窗口记忆。

## 3. 最小实现

- `_learning_context_for_query()` 返回 `project_id`、`workflow_id`、`include_global_memory`，并保留已有 `memory_gate` 决策摘要；异常降级也不伪造记忆内容。
- `build_learning_context()` 在显式范围上下文中再次检查 memory 的 `scope/project_id/workflow_id/subtype`；错项目、禁用全局、未绑定/错窗口的 `workflow_fact` 和未绑定 `project_constraint` 不进入 prompt。
- 预渲染 capsule 的任一 item 越过范围门时，整段 capsule 文本失效，不从未经验证的文本字段回退；无显式范围的历史单元测试保持兼容。

## 4. 失败优先与验证

- 新增旁路反例：项目 A、论文窗口的 `learning_context` 携带项目 B 的 capsule 和补充 memory；修复后两者均不出现在最终上下文。
- RAG prompt 定向 `105 passed`；学习存储/MCP/记忆门组合 `149 passed, 1 warning`。
- 这些是 prompt 前的范围防线，不把它们当作身份认证、自动记忆抽取质量或模型答案准确率证明。

本轮代码稳定后的完整工程门：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。该门仍不证明真实 Tauri/WPS UI、打包启动、外部服务或模型答案准确率。
