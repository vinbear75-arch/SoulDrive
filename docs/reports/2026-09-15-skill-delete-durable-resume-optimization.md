# 2026-09-15 Skill 删除 durable resume 优化

## 结论

本轮补齐了项目 Skill 删除的阶段收据、幂等键和可恢复边界。此前删除流程是“先删本地 Skill 目录，再删 LearningStore 记录”两个有副作用的步骤；如果目录删除成功、数据库删除失败，重试会因为目录已经不存在而返回 `409 source_cleanup`，数据库记录无法再被清理。

现在 `DELETE /learning/skills/{skill_id}` 支持 `Idempotency-Key`：

- `in_progress`：源目录删除仍可能未知，活动租约阻止其他请求重复执行；
- `source_removed`：源目录已确认删除，重试可以立即接管剩余的数据库删除阶段，不等待旧 worker 租约；
- `completed`：回放加密保存的最终响应，不重复文件或数据库副作用；
- `failed`：源目录阶段失败时保留记录，后续同键可重新 claim。

无幂等键的旧客户端路径保持原有响应语义；Tauri Skill API 代理和前端删除按钮已透传并保留同一键，用户在失败后重新点击时不会悄悄生成另一条删除请求。

## 外部实践对照

- [DBOS Architecture](https://docs.dbos.dev/architecture) 将 workflow 输入和 step 输出持久化，并从最后一个已完成 step 恢复；本轮只借鉴“阶段收据 + 恢复点”，没有引入 DBOS runtime。
- [DBOS Transactional Outbox](https://docs.dbos.dev/python/examples/outbox) 明确区分数据库事务与外部副作用，并要求 at-least-once 的外部 step 具备幂等性；本轮把本地目录清理和 LearningStore 删除拆成可观察阶段，避免把两步伪装成原子事务。
- [DBOS Workflow Recovery](https://docs.dbos.dev/production/workflow-recovery) 的恢复边界要求能从已完成工作继续；本轮用 `source_removed` 允许立即恢复数据库阶段。
- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http) 以 `Idempotency-Key` 绑定一次调用并回放结果；本轮为 Skill 删除保存输入摘要和加密响应，换输入复用旧键会 `409`。

## 失败优先证据

新增并先在旧实现上复现：

1. 源目录删除成功；
2. `LearningStore.delete_skill()` 抛出数据库不可用异常；
3. 首次请求返回 `500 stage=record`，目录已不存在、数据库记录仍存在；
4. 使用相同幂等键重试时，旧实现再次删除目录并返回 `409 stage=source_cleanup`。

另一个反例模拟“数据库记录已经删除，但完成收据写入失败”：首次请求返回收据阶段错误，重试必须从 `source_removed` 收据恢复，而不是把不存在的 Skill 当作普通 `404`。

## 最小实现

- `core/learning_store.py` 新增 `skill_deletion_receipts`，保存请求标识、Skill 身份、项目范围、源摘要、阶段、租约、错误观察和加密响应。
- claim 使用输入摘要绑定幂等键；`in_progress` 仍有并发租约，`source_removed` 允许安全接管剩余数据库阶段。
- `core/mcp_server.py` 在源目录删除后写入 `source_removed`，数据库失败不覆盖该状态；重试跳过已完成的目录阶段。
- 完成响应加密保存，后续同键回放时标记 `idempotent_replay=true`。
- `souldrive-ui/src-tauri/src/lib.rs` 透传 `Idempotency-Key`；`souldrive-ui/src/App.tsx` 在 Skill 删除失败后保留同一键，成功后清理该键。

## 验证证据

- Skill 删除定向：`7 passed, 1 warning`，包含源阶段失败、数据库阶段失败恢复、完成收据写入失败恢复、完成态回放、旧标题/路径兼容和审计失败边界。
- LearningStore / Skill API / 安装器 / Skill E2E 组合：`251 passed, 1 warning in 330.92s`。
- 前端生产构建成功；既有 Vite 单 chunk 体积警告仍存在（约 `587.64 kB`）。
- Tauri `cargo check --locked` 通过。
- 完整 `scripts/check.ps1`：后端 `1662 passed, 1 warning in 503.55s`；前端生产构建成功，8 组 UI 脚本合计 `66 passed`；`compileall` 与锁定 Cargo 检查通过。
- 冻结记忆质量：[memory_quality_after_skill_delete_durable_resume_20260915.json](../../eval/results/memory_quality_after_skill_delete_durable_resume_20260915.json)：`7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`。
- Learning loop：runtime `4/4`，静态 baseline `0/4`；本轮没有把删除可靠性误报成记忆质量提升。

## 边界

本轮证明的是本机 SQLite/LearningStore 与 Skill API 在阶段失败、重试和同键回放下的可恢复行为，不证明跨主机 exactly-once、真实 Redis/PostgreSQL、文件系统与数据库的原子事务、真实 Tauri UI/E2E 或外部 Skill 源的安全性。`source_removed` 之后的数据库删除仍是 at-least-once；`delete_skill` 必须保持幂等，不能据此宣称跨系统 exactly-once。
