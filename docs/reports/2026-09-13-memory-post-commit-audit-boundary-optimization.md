# 记忆提交后审计故障边界优化

日期：2026-09-13

## 对照实践

- [LangGraph checkpoints](https://langchain-ai.github.io/langgraph/reference/checkpoints/) 将 durable checkpoint 与运行状态恢复分开；[memory 管理说明](https://langchain-ai.github.io/langgraph/how-tos/memory/manage-conversation-history/) 区分 thread 级短期状态与跨 thread 的长期 store。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 保留事实的时间边界和历史，而不是把当前检索投影当作唯一事实源。
- [Graphiti scope/invalidation issue](https://github.com/getzep/graphiti/issues/1728) 提醒：事实失效必须绑定正确的范围，否则一个上下文的更新可能错误影响另一个上下文。

## 失败优先发现

记忆主事实已经写入本地 LearningStore、审核结果已经落库、运行时记忆已经完成 receipt，或者记忆已经完成编辑/撤销/删除时，如果 `current_audit_logger().append_event` 抛错，旧接口会返回 500。运行时记忆尤其危险：receipt 未闭合会让客户端误以为保存失败并重复提交。

## 本轮修改

- 反思候选草稿回放/创建、人工审核、运行时自动记忆、记忆编辑、撤销和删除统一采用 `_append_audit_best_effort`。
- 审计失败返回 bounded `warning`，不改变已提交的记忆状态、版本、时间有效性或 storage sync 结果。
- 运行时记忆先完成主事实写入，再记录审计告警，最后闭合 receipt；同一 `session_id + request_id` 重试仍回放同一 `memory_id`。
- UI 在候选生成、审核完成、自动记录、编辑、停用和删除提示中显示 warning；审计失败不会静默吞掉，也不会覆盖业务成功。

## 验收证据

- 新增 4 个失败优先回归：反思草稿、反思审核、运行时记忆 receipt、记忆编辑/撤销/删除。
- 记忆专项回归：`4 passed, 1 warning`；原记忆模块回归保持通过。
- 前端完整 UI 测试：`60 passed`；TypeScript/Vite production build 通过。
- 随后完整 `scripts/check.ps1`：后端 `1307 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是记忆审计 outbox，也不证明审计事件自动补偿、跨进程 exactly-once 或跨存储原子提交。
- 记忆向量、关键词和图谱仍是可重建派生索引；本轮没有把它们提升为事实源，也没有扩大记忆自动写入范围。
- 本轮只处理审计 sink 故障；模型误判、语义重复、跨项目/工作流隔离和时间有效性仍由既有 LearningStore、memory gate、GraphRAG 范围契约和评测门负责。
- 完整工程门不等于真实 Tauri/UI 点击、真实模型调用或长期用户记忆准确率。
