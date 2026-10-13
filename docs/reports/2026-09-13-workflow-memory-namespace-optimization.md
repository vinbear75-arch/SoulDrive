# 工作流绑定记忆命名空间优化

日期：2026-09-13

## 结论

本轮完成一处会直接影响三个研究窗口语义的记忆边界修补：`paper_review`、`experiment_compare` 和 `meeting_actions` 产生的 `workflow_fact` 现在必须绑定到有效的 `workflow_id` 才能进入运行时记忆上下文；论文窗口的事实不会再被实验窗口或会议窗口复用。`user_preference`、`project_fact` 等共享记忆仍保持原有 global/project 作用域。

这是命名空间隔离和可追溯性修补，不是新增自动记忆抽取器，也不代表三个窗口已经在冻结业务集上证明了不同的答案准确率。

## 公开实践对照

- [Letta：attaching and detaching blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/) 把可切换的工作上下文作为显式 memory block 管理；本地采用更小的实现：复用已持久化的会话 `workflow_id`，不引入新的 Agent runtime。
- [Mem0 v2 memory filters](https://docs.mem0.ai/platform/features/v2-memory-filters) 用 entity、agent、app、run 等过滤维度限制记忆召回；本地把业务窗口作为受控过滤维度，且把 SQLite 的范围门放在候选召回之前。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 区分 thread/checkpoint namespace 与长期记忆；本地继续保持会话事实源与长期 `LearningStore` 分离，只把工作流绑定作为长期事实的显式元数据。
- [Inngest idempotency](https://www.inngest.com/docs/guides/handling-idempotency) 与 [DBOS workflow steps](https://docs.dbos.dev/python/tutorials/workflow-tutorial) 强调重放时输入身份和步骤状态必须可判定；本轮沿用项目已有 review/request digest，不把工作流切换设计成可覆盖旧 session 的隐式副作用。

## 本地缺口与失败优先证据

现有工作流目录已经定义三种不同的研究角色，且会话元数据保存 `workflow_id`；但此前 `LearningStore` 的 `workflow_fact` 只有 global/project 作用域，没有绑定具体窗口。这样同一项目内的论文结论、实验比较结论和会议行动项可能通过记忆检索进入错误窗口。

新增回归先按旧接口运行，`LearningStore.list_memories(..., workflow_id=...)` 直接失败，证明旧实现没有工作流过滤入口；随后加入存储层过滤，再验证论文/实验事实的交叉读取为负，匹配窗口读取为正，未指定窗口的运行时上下文也不会注入未绑定的工作流事实。

## 实现边界

1. 反思草稿从源会话继承工作流；请求显式指定的工作流与源会话不一致时返回 `409`，避免客户端把事实写入另一窗口。
2. `workflow_fact` 的候选、活动记录、历史版本、关键词搜索和语义搜索均带 `workflow_id` 判断；匹配的窗口才能读取，未知或未绑定的旧事实在 context-only 读取时 fail-closed。
3. 共享记忆仍可被所有合法窗口读取；管理列表保留查看未绑定/旧事实的能力，便于清理和迁移，不把管理视图误当运行时上下文。
4. Redis Vector Set/RediSearch 派生投影携带工作流标签并在查询后再次过滤；SQLite 仍是权威范围门。RediSearch 查询返回字段计数同步校正为实际字段数，避免真实 Redis 走兼容回退。
5. 记忆胶囊 provenance 增加工作流标识；审计、反思重放摘要和前端草稿指纹也携带有效工作流，保证同一请求在切换窗口后不会错误复用。

## 验收结果

- `tests/test_learning_store.py`：`70 passed`。
- MCP 学习与会话/Redis 组合：`113 passed, 1 warning`；新增工作流绑定、上下文传参和 Redis 投影过滤回归均通过。
- 记忆运行时、工作流绑定和 Redis 向量专项：`5 passed`。
- 完整 `scripts/check.ps1`：后端 `1210 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。
- 警告为环境中的 Starlette/httpx 弃用提示；前端的大 chunk 是既有构建提示，不是本轮失败。

## 当前仍未宣称的内容

- 没有引入自由循环 Agentic RAG，也没有把 workflow namespace 当作答案正确率证明；窗口差异仍需冻结同一业务评测集，分别验证检索候选、证据覆盖、引用和输出契约。
- 存量未绑定 `workflow_fact` 不会自动猜测归属，也没有自动批量迁移；它们可以由管理/doctor 视图发现后人工确认。
- Redis、PostgreSQL 和本地归档仍是派生同步边界；本轮不宣称跨进程 exactly-once、真实桌面 UI/WPS E2E 或安装包验收。

