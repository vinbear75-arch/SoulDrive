# 记忆变更审计身份一致性优化

日期：2026-09-15  
范围：记忆更新、撤销、删除接口的本地审计事件

## 外部实践对照与真实缺口

- [Mem0 Update Memory](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/update.mdx) 将稳定 `memory_id`、新内容/metadata 和时间字段作为一次可核验的更新对象。
- [Mem0 Delete Memory](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/delete.mdx) 要求按明确 ID 或明确过滤范围执行删除，不能把删除对象退化成无范围的日志事件。
- [Mem0 How It Works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将 scope/entity/run 等 metadata 作为检索和生命周期边界。

SoulDrive 的事实源和幂等回执已经保存 `scope`、`project_id`、`record_version` 和来源信息，但三条 mutation 审计事件不一致：更新记录部分身份，删除只有范围信息，撤销只有裸 `memory_id`。出现同 ID 重放、项目清理或来源审计时，审计记录无法独立回答“哪一版、哪个项目、来自哪条审核/会话”。

## 失败优先验证

新增 `test_memory_revoke_audit_keeps_scope_version_and_source_identity`，创建带项目范围、来源候选和来源会话的记忆后调用 revoke。旧实现返回成功，但捕获到的审计 payload 只有：

```json
{"memory_id": "..."}
```

因此测试对范围、版本、候选来源和来源会话的断言先于实现真实失败。

## 最小实现

新增 `_memory_audit_identity()`，只抽取非正文身份字段：

```text
memory_id, scope, project_id, record_version,
source_candidate_id, source_session_id, source_workflow_id
```

更新、撤销、删除三条审计路径统一使用该函数。正文、标题、source excerpt 和消息内容不会写入本次审计 payload；缺失来源字段时保持缺失，不猜测来源。

## 验证证据

- 失败优先：旧撤销审计缺少范围/版本/来源；修复后通过。
- 记忆管理/幂等/审计故障定向：`6 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1653 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python `compileall` 与锁定 Cargo 检查通过。Vite 既有单 bundle 约 `587.48 kB` 警告仍在。
- 冻结记忆评测：[memory_quality_after_memory_audit_identity_20260915.json](../../eval/results/memory_quality_after_memory_audit_identity_20260915.json)：`7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`。
- 冻结 GraphRAG 路由：[graphrag_route_after_memory_audit_identity_20260915.json](../../eval/results/graphrag_route_after_memory_audit_identity_20260915.json)：`4/4`。
- 研究窗口角色：[workflow_roles_after_memory_audit_identity_20260915.json](../../eval/results/workflow_roles_after_memory_audit_identity_20260915.json)：`6/6`，所有角色与输出/引用子指标为 `1.0`。
- episodic runtime：`5/5`；baseline `2/5`，未被本轮变化影响。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_memory_audit_identity_20260915.json](../../eval/results/graphrag_acceptance_after_memory_audit_identity_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮提升的是本地审计可追溯性，不是记忆内容正确性、自动矛盾合并、语义召回率或模型答案质量。真实外部审计 sink、Redis/PostgreSQL、跨主机 exactly-once、历史审计日志迁移和真实 Tauri/WPS UI/E2E 仍需单独验收。
