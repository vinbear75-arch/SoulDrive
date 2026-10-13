# 记忆版本冲突与派生投影一致性优化

日期：2026-09-14

## 发现

记忆记录使用 `memory_id + record_version` 作为重放边界，但本地 archive 与 PostgreSQL 投影原先只阻止旧版本回写；同一版本、同一生命周期状态下的不同正文或元数据仍可能覆盖已有记忆。这样会让错误的旧事件看起来像一次成功重放。

## 公开实践对照

- [Mem0 memory operations](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx)：记忆写入与检索要保留稳定身份和可追踪的更新语义，不能把检索结果本身当作事实源。
- [Graphiti temporal edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py)：时间有效性与失效状态是显式字段，重放需要区分版本/有效期，而不是静默覆盖。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：稳定请求身份遇到不同输入应暴露冲突，安全重试只接受同一输入。

本轮沿用 SoulDrive 已有的 `record_version` 和生命周期状态，不引入新的记忆数据库。

## 实现

- 增加记忆语义身份摘要，覆盖版本、范围、项目/会话、标题、正文、来源、标签、workflow 和有效期等字段；排除归档/运行时时间戳及向量缓存字段。
- 本地加密记忆 archive：同版本同生命周期的不同语义返回 `memory version conflict`；版本升级正常写入，`active → archived/revoked/deleted` 生命周期迁移正常写入。
- `ProductionSessionStore` 的 memory outbox stale gate 在派生事件重放前执行同一冲突判断，避免旧 outbox 绕过事实源规则。
- PostgreSQL memory projection 在版本相同时校验正文 hash、项目/会话身份和已存在的元数据；同版本不同输入 fail-closed，旧版本仍被忽略。
- 这条边界与此前 memory scope/workflow、时间有效性及最终 prompt scope gate 叠加，记忆系统仍以本地事实源为准。

## 验证

- 失败优先反例：旧实现允许同一版本不同正文覆盖；修复后拒绝并保留原始内容。
- PostgreSQL 同版本不同 payload：拒绝；旧版本：不触发 upsert。
- memory、会话、学习和运行时相关回归：`179 passed`。
- 离线 memory quality evaluator：`7/7 scenarios`、`8/8 queries`、`memory_recall@k=1.0`、`stale_or_forbidden_leakage_rate=0.0`。
- 完整工程门禁：后端 `1507 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。真实 Redis/PostgreSQL 实例联调仍未完成。

## 边界

这保证的是本地事实源和控制面投影不会静默接受同版本冲突输入，不等价于记忆内容事实正确、模型回答正确或跨存储 exactly-once。
