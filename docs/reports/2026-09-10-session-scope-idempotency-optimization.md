# SoulDrive 会话范围与创建幂等优化记录

日期：2026-09-10

## 本轮结论

会话 ID 现在是不可迁移的持久范围身份：全局会话不能被项目入口复用，项目会话也不能被全局入口复用；同一项目、同一 `session_id`、同一标题和会话角色的重试返回 `replayed=true`，不再把重试伪装成新建并重复写入 `session.created`。

前端项目会话创建也不再每次网络调用都省略 `session_id`：在项目、标题和业务窗口输入不变时沿用同一会话 ID，失败后重试仍指向同一个持久会话；输入变化或重新打开创建窗口时生成新身份。

## 外部实践依据

- LangGraph 持久化把 `thread_id` 作为恢复短期状态的明确线程身份，并将状态按线程组织；SoulDrive 将同样的不可漂移原则应用到全局/项目会话范围：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
- LangGraph 的子图持久化文档区分 per-invocation、per-thread 和 stateless 生命周期；研究窗口需要持久 thread 角色，不能在另一个入口中把同一 ID 改成另一命名空间：[LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)。
- Inngest 将事件 ID 用作重复事件去重身份，并要求业务调用方在重试期间保持同一 ID：[Inngest idempotency](https://www.inngest.com/docs/guides/handling-idempotency)。

本轮没有引入图运行时或分布式协调服务，直接修正本地加密会话 manifest 的项目比较逻辑，并让 UI 使用已有的显式 `session_id` 字段。

## 实现范围

1. `ConversationStore.create_session` 对已有 manifest 要求 `existing.project_id == requested.project_id`；`None` 只匹配全局，不再匹配任意项目。
2. 全局和项目会话创建 API 在显式 ID 重试时返回 `status=replayed`、`replayed=true`，并将审计事件区分为 `session.replayed`。
3. UI 用输入指纹绑定稳定的 `session_id`，网络失败且输入未改变时复用；标题/角色/项目变化时生成新 ID。
4. 保留同一 ID 但标题或 workflow 改变时拒绝，防止通过重试悄悄改变研究窗口职责。

## 验收证据

- 先添加失败回归：旧实现允许全局会话 ID 从项目入口返回 `200`，并对同项目重试仍返回 `status=created`。
- 修复后项目/会话与会话存储专项：`40 passed, 1 warning`。
- UI 静态回归：`15 passed`；覆盖稳定会话 ID、项目历史项目范围和研究窗口切换状态清理。
- 随后的仓库级 `scripts/check.ps1`：后端 `1062 passed, 1 warning`；前端生产构建通过，7 组 UI 测试 `48 passed`；`cargo check --locked` 通过。真实 Tauri 窗口仍未替代自动回归。

## 边界与后续

- 本轮只保证当前本机工作区内的会话命名空间不可迁移；不提供跨主机 session store 的分布式租约。
- `session_id` 相同但标题或 workflow 不同仍返回冲突，这是保护研究窗口职责的预期行为，不是自动合并。
- UI 静态检查不能证明真实浏览器在断网重试时保留状态；若要关闭该门，需要真实 Tauri/浏览器网络故障演示。
