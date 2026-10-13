# SoulDrive Skill 计划幂等性优化记录

日期：2026-09-09

## 本轮结论

已将 `POST /learning/skills/{skill_id}/runs` 的 `plan_only` 记录入口补为可重试的本地幂等操作。调用方提供 `Idempotency-Key` 后，同一工作区内相同 Skill、查询、计划、匹配分数、输入和状态只产生一条 `skill_runs` 记录；重复请求返回原 `run_id`，输入不一致返回 409，不会静默创建第二条计划历史。

这解决的是“计划记录写入”这一小型本地副作用，不等同于通用脚本执行 exactly-once：当前接口仍明确停用通用 Skill 脚本执行，且没有把外部系统副作用纳入本轮范围。

## 外部实践依据

- Restate 的 HTTP invocation 文档说明，`Idempotency-Key` 用于重试去重；完成后保存响应，使用同一键重试时返回相同结果且不再次执行：[Restate HTTP invocations](https://docs.restate.dev/services/invocation/http)。
- LangGraph 的持久化文档将跨请求状态与命名空间持久化作为独立能力，提醒本地工作流状态不能只依赖内存：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。本轮只借鉴“持久记录+明确键”的原则，没有引入 LangGraph。

## 实现范围

1. `learning.sqlite` 的 `skill_runs` 增加 `idempotency_key` 和 HMAC 绑定摘要；旧库启动时自动补列，并建立允许 NULL 的唯一索引。
2. `LearningStore.record_skill_run` 使用 `BEGIN IMMEDIATE` 串行化同键声明，首次写入、同输入重放、不同输入冲突分别返回明确结果。
3. Skill API 读取 `Idempotency-Key` 请求头；重放不重复写入计划审计事件，并在响应中给出 `replayed`。
4. 补充存储层、并发、HTTP 入口回归测试。

## 验收证据

- 先添加失败测试，旧实现确实因 `record_skill_run` 不接受幂等键而失败。
- `tests/test_learning_store.py`：47 passed。
- `tests/test_mcp_server_learning.py`：54 passed，1 个既有 Starlette/httpx 弃用警告。
- `git diff --check`：未发现空白错误；仅报告既有工作区文件的 CRLF 提示。

## 边界与后续

- 幂等键只在当前工作区的本地学习数据库内生效；不跨机器、不跨工作区，也没有自动过期策略。计划历史保留周期仍由 Skill 删除/清理策略决定。
- 该键保护的是“计划记录”写入，不代表 RAG 回答正确率提升，也不证明真实 Tauri UI 或打包环境的网络重试行为已完成验证。
- 下一轮继续审计尚未统一的项目参数入口，优先检查多模态推理/工作流路由在传入未知 `project_id` 时是否会产生误导性结果。
