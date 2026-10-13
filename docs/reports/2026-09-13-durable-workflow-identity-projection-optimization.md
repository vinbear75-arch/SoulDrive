# Durable workflow 身份投影与时间序列优化

日期：2026-09-13

## 发现

本地加密归档和 Redis 会话元数据已经保存 `workflow_id`，但可选 PostgreSQL 派生层的 `souldrive_sessions` 没有该列，`souldrive_memory_records.metadata_json` 也没有保留工作流身份。恢复或外部诊断因此可能看到会话/记忆，却无法判断它属于论文、实验还是会议窗口。

## 公开实践对照

- [Temporal Workflow ID/Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid) 将稳定的业务 Workflow ID 与可因重试变化的 Run ID 分开；本地把 `workflow_id` 作为会话/记忆业务身份持久化，不把一次请求 trace 或重试运行号写成窗口归属。
- [Graphiti project configuration/group_id](https://github.com/getzep/graphiti/pull/1209) 强调项目配置是 group_id 的权威来源，并修复写入与读取使用不同 group 的问题；本地让归档 manifest 成为 PostgreSQL session projection 的来源，outbox 重放也从同一份本地元数据补齐。
- [Restate workflows](https://docs.restate.dev/tour/workflows) 的 durable workflow 思路要求重放使用持久化状态而不是内存猜测；本地只同步派生元数据，不引入新的分布式 workflow 服务。

## 实现

1. PostgreSQL `souldrive_sessions` 建表加入 `workflow_id`，并用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 兼容已有本地数据库。
2. `upsert_session()`、session-created/session-message outbox 和恢复路径均传播 `workflow_id`；消息重放缺少新 payload 字段时回读本地 manifest，避免旧 outbox 重新覆盖/丢失身份。
3. PostgreSQL memory projection 的 `metadata_json` 保留 `workflow_id`，与本地 LearningStore/Redis 投影保持同一工作流命名空间。
4. `LearningStore.update_memory()` 在连续写入时对活动记忆的 `updated_at` 使用严格递增的下一个可表示浮点值，避免 Windows 时钟分辨率让后更新事实在分数相同的搜索中落后。

## 失败优先与验收

- 新增 PostgreSQL session identity、memory identity 和 ProductionSessionStore 传播回归共 `3 passed`；会话存储专项 `45 passed`。
- 第一次完整检查真实复现已有时间 tie 反例：`test_memory_search_prefers_newer_memory_on_score_tie` 失败；修复严格递增更新时间后，LearningStore `71 passed`。
- 随后完整 `scripts/check.ps1` 通过：后端 `1214 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮验证的是 SQL/schema/参数传播契约和本地恢复逻辑；没有连接真实 PostgreSQL 服务执行迁移，也没有宣称跨服务 exactly-once。前端大 chunk 和 Starlette/httpx 弃用提示仍是既有警告；真实 Tauri/WPS/UI E2E 和安装包仍需单独验收。

