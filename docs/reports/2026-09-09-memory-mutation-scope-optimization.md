# SoulDrive 项目记忆变更入口范围优化

日期：2026-09-09

## 结论

项目记忆的读取、审核创建和审核决策已经按项目隔离后，本轮继续把编辑、停用、删除三个事实变更入口纳入同一条范围约束：项目记忆必须携带所属 `project_id`，且只能由相同项目操作；全局记忆仍可由共享上下文管理。错误范围现在在存储层拒绝，而不是依赖 UI 自觉传参。

本轮没有增加多租户权限系统，也没有把项目记忆复制成新的全局记忆；只补齐本机单用户工作区内已经存在的项目边界。

## 外部实践对照

- [Open WebUI memory router](https://raw.githubusercontent.com/open-webui/open-webui/main/backend/open_webui/routers/memories.py) 的读取、更新和删除都绑定已认证用户；这提供了“事实变更必须带所有者范围”的最小实践基线。
- [Open WebUI memory 文档](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/memory.mdx) 将记忆作为独立管理对象，支持明确的替换和删除，而不是把记忆内容隐式混入普通对话写入。
- [Open WebUI 安全公告 GHSA-hmjq-crxp-7rjw](https://github.com/open-webui/open-webui/security/advisories/GHSA-hmjq-crxp-7rjw) 说明仅凭已知 `memory_id` 保护读取/更新是不够的；SoulDrive 对应采用项目范围校验，并把检查放在存储层的活动记录变更事务内。

## 失败优先复现

1. 在 `project-a` 创建一条项目记忆。
2. 用旧的 `update_memory(memory_id, ...)` 不带项目范围即可修改成功。
3. 用旧的 `/reflection/reviews/pending?project_id=project-a` 读取时，项目 A 和项目 B 的待审核草稿都会被返回。

这证明仅在列表接口过滤、或仅在审核决策时校验，仍不足以保护整个记忆生命周期。

## 当前改动

- `core/learning_store.py`
  - `list_pending_reviews(project_id=...)` 只返回精确项目的待审核草稿；省略项目时只返回全局草稿。
  - 新增统一的项目记忆变更校验。
  - `update_memory`、`archive_memory`、`delete_memory` 在事务内校验项目范围；项目记忆缺少项目 ID 或项目不匹配时抛出冲突，不修改活动记录。
- `core/mcp_server.py`
  - `/reflection/reviews/pending` 支持 `project_id` 查询参数。
  - 记忆编辑、停用、删除接口接收项目范围，并将存储层冲突转换为 `409`。
  - 自动记忆保存响应返回 `project_id`，便于撤销时复用同一范围。
- `souldrive-ui/src/App.tsx`
  - 记忆管理器和自动记忆撤销请求带上记忆自身的项目范围；全局记忆保持共享操作语义。
- 回归覆盖
  - 审核队列的项目 A/B 读取隔离。
  - 项目记忆缺少范围、错误范围、正确范围下的编辑/停用/删除行为。
  - API 越权编辑返回 `409`，正确项目仍可编辑并保持乐观并发检查。

## 验证

- 旧实现的项目待审核回归失败：项目 A 查询返回了 `project-b` 草稿。
- 旧实现的项目记忆变更回归失败：缺少 `project_id` 仍能修改项目记忆。
- 修复后定向检查：待审核/审核 API `3 passed`，记忆存储范围及管理 API 相关组合 `5 passed`，均保留既有 FastAPI/httpx deprecation warning。
- 最新仓库级检查：后端 `1022 passed, 1 warning`；前端生产构建通过，7 组 UI 测试 `44 passed`；Tauri `cargo check --locked` 通过。

## 未完成边界

- 这是本机单用户工作区内的项目范围约束，不等于跨用户认证或跨主机授权。
- 全局记忆允许从项目上下文管理是有意设计；若未来要求全局记忆也必须绑定具体入口，需要单独设计全局管理会话，不能把当前项目 ID 误当成全局所有者。
- 本轮没有把编辑/删除变更接入新的分布式 durable runtime；本地加密事实层和已有 outbox 仍是当前同步/补偿边界，跨数据库崩溃窗口仍需单独做故障注入验证。
