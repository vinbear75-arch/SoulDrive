# SoulDrive 检索反馈跨实例幂等优化

日期：2026-09-15

## 外部实践对照

- [LangMem semantic memory concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)：记忆系统应区分结构化语义事实、经历和程序规则，并让更新/合并/删除成为显式存储操作，而不是每次都追加一条不可解释记录。
- [LangMem semantic memory guide](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)：已有记忆被更新时，系统应能返回删除/替换旧记录的结果；当前项目的检索反馈对应“同一 query 的已确认改写”更新语义。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)：唯一约束和事务边界必须覆盖“查找既有记录—决定更新/插入—发布新载荷”的完整过程；`NULL` 参与复合 UNIQUE 时不能代替显式事务串行化。

## 失败优先发现

`retrieval_feedback` 表已有 `UNIQUE(scope, project_id, query_digest)`，但 global 反馈的 `project_id` 是 `NULL`。SQLite 对复合唯一约束中的多个 `NULL` 不视为相等；两个独立 `LearningStore` 实例同时保存同一 query 时，实际产生了两个不同 `feedback_id`，而不是回放同一条反馈。

这会让用户重复确认同一个检索纠正，后续检索拿到多个候选改写，破坏“反馈更新替换旧变体”的既有语义。

## 最小实现

- `save_retrieval_feedback()` 将“查询既有反馈、生成加密载荷、更新或插入”放入显式 `BEGIN IMMEDIATE` 事务。
- 第二个实例等待第一个事务提交后重新查询，发现既有 `feedback_id` 并更新/回放同一条记录；同一进程仍由 LearningStore 的连接级 `RLock` 保护。
- 选择既有记录时使用 `updated_at DESC, feedback_id ASC` 稳定顺序，不改动已有表结构，也不静默删除可能由旧版本产生的历史重复记录。

## 验证证据

- 失败优先：跨实例相同 global query 回归曾得到两个不同 `feedback_id`。
- 修复后 LearningStore 全套：`92 passed`；同一 query 的两个并发结果共享一个 `feedback_id`，查询只返回一条活动反馈；既有项目/global 更新、撤销和加密存储回归保持通过。
- `scripts/check.ps1`：后端 `1619 passed, 1 warning`；前端生产构建成功、UI 脚本测试通过；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机 SQLite 多实例的幂等与更新串行化，不是跨主机分布式 exactly-once，也不证明外部检索服务或模型调用幂等。
- 不自动合并/删除历史重复反馈；若旧版本已留下重复记录，当前实现选择稳定的最新记录进行后续更新，清理仍需独立的审计/人工确认流程。
- 本轮不引入 LangMem/LangGraph 依赖；现有项目继续使用加密 SQLite 事实源、可选语义投影和显式项目/workflow 范围。
