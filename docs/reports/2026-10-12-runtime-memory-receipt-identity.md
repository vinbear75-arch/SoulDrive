# 运行时记忆回执的业务身份闭合

## 外部机制与本地缺口

[Temporal 的 Workflow replay 说明](https://docs.temporal.io/workflows)把 Event History 作为 durable source of truth：Activity 已完成的结果在重放时复用，而不是再次执行外部操作；[Temporal 的事件历史说明](https://docs.temporal.io/encyclopedia/event-history)也明确把恢复所需的每一步保存为持久事件。本轮没有引入 Temporal，而是把同一边界落实到 SoulDrive 当前的本机 receipt：通用请求回执的结果 ID 不能脱离本地记忆事实源直接被信任。

当前运行时记忆接口用 `session_id + memory:{request_id}` 做幂等键，receipt 的 `response_message_id` 实际保存的是 `memory_id`。此前完成态回放只返回这个字符串，没有检查它是否仍对应当前 runtime reflection review、当前项目/全局范围和 active memory。

## B0 反例

新增反例先复现旧行为：人为构造一个 completed runtime receipt，使其指向不存在的记忆 ID；旧接口重试返回 `200/saved`，并把该 ID 当作业务结果。失败优先回归为 `1 failed, 150 deselected, 1 warning`。

这不是单纯的字段格式问题：它会让客户端得到一个无法撤销、无法检索、也无法从本地事实源恢复的“记忆已保存”结果，违反 durable replay 应复用原业务结果的边界。

## 最小修复

- 新增 `_runtime_memory_replay_matches_receipt()`：回放时用相同的 runtime review key/digest 找回原 reflection review，要求 review 已完成且至少有 accepted memory candidate；再要求 receipt 的 ID 命中 active memory，并且命中原 candidate 或明确的 deduplicated memory。
- 回放前再次校验 scope：global memory 必须没有 `project_id`，project memory 必须精确属于当前项目；缺失、跨项目、已归档或 review 不一致统一返回 `503`，不重新执行自动记忆写入。
- 修正记忆 canonical 写入：`user_preference` 虽然可以从某个项目窗口产生，但事实层保持 `scope=global, project_id=null`；项目上下文只在 API 响应中保留，避免把展示上下文写回事实和 outbox。
- 新增 canonical scope 单测，并保留旧反思 API 对 `accepted_assets[].project_id` 的项目上下文契约；没有修改冻结评测集、gold、业务种子、训练或调参。

## 验收证据

- 运行时记忆/回执/反思定向回归：`11 passed, 1 warning`。
- LearningStore canonical scope 定向回归：`2 passed`。
- 第一次完整复核曾真实暴露 1 个响应契约失败（后端 `1769 passed, 1 failed`），修复响应层上下文后重新独立执行。
- 第二次独立完整 `scripts/check.ps1`：退出码 `0`；后端 **1770 passed, 1 warning，468.06 秒**；记忆质量 **7/7 场景、8/8 查询、Recall@K=1.0、陈旧/禁止泄漏率=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；episodic runtime **5/5**；前端生产构建成功；UI 脚本 **66 passed**；`compileall` 与锁定 Cargo 检查通过。

## 仍未证明

- 这是本机 SQLite/加密归档内的回放身份门，不是跨进程、跨主机或外部副作用 exactly-once；真实 Redis/PostgreSQL、断电恢复和发布版 UI/E2E 仍未验证。
- 回放依赖 active canonical memory；如果用户在原请求完成后主动撤销记忆，再用旧 request_id 重试会 fail-closed 并返回 503，需要新的 request_id 重新表达保存意图。这是避免伪造 saved 的保守边界，不等于实现了历史响应快照。
- 该修复证明的是 receipt/事实身份和 scope，不证明长期记忆的开放式召回、模型答案事实性、跨会话泛化或 LongMemEval 分数。
