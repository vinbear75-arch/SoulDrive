# 记忆 capsule 的 memory ID 去重优化

日期：2026-09-12

## 结论

本轮修复了记忆读取 gate 的重复注入边界。此前 `gate_memory_context` 会信任上游列表并逐条接受；当当前记忆、历史版本或不同派生投影返回同一个 `memory_id` 时，胶囊可能重复包含同一逻辑记忆，浪费上下文预算，也使审计中的 accepted ID 与实际注入条数不一致。

现在 gate 在敏感内容、相关性和冲突裁决之后增加 ID 级去重：同一 `memory_id` 只保留一条，按项目范围、人工确认、`record_version`、新鲜度、相关性和更新时间选择胜者；被排除项只记录 ID 级 `duplicate_memory_id` 原因，不执行未经评测的语义合并。

## 外部实践对照

- [Mem0 architecture](https://github.com/mem0ai/mem0/blob/main/skills/mem0/references/architecture.md) 将 deduplication、conflict resolution 和 scoped retrieval 作为记忆检索链路的显式阶段，并警告不同写入模式可能造成同一事实重复。
- [Letta archival memory](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py) 要求长期记忆尽量是自包含事实/摘要，并使用结构化检索结果；[Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 也对重复 block label 做去重。
- [Graphiti edge deduplication](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 将重复事实边视为维护阶段的显式对象，而不是让重复结果自然进入上下文。

本轮只迁移“统一注入边界必须去重”的原则，复用 SoulDrive 已有的 `memory_id`、版本和 gate 审计，不引入向量库全局合并或后台 consolidator。

## 失败优先验证

- 新增 `MemoryRuntimeTests.test_gate_memory_context_deduplicates_repeated_memory_id`。
- 修复前：两条相同 `memory_id` 的有效记录均进入 `accepted_memory_ids` 和 capsule，回归失败于收到两个相同 ID。
- 修复后：accepted ID 和 capsule item 均只有一条，并产生 `duplicate_memory_id` 诊断。

## 实现与验证

- `core/memory_runtime.py::gate_memory_context` 在现有冲突裁决之后执行 bounded ID 去重；无 ID 的旧/测试输入保持原行为。
- 记忆运行时、RAG prompt、学习候选验证组合：`96 passed`。
- MCP 学习 API 回归：`71 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1141 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是同 ID 去重，不是相似文本去重；不同 `memory_id` 的重复事实仍由现有 review、duplicate/conflict、时间有效性和 memory doctor 处理。
- 胜者选择只解决同一逻辑 ID 的读取重复，不改变主库、历史版本或 outbox 数据；不会自动删除或合并记忆。
- 本轮尚未在冻结业务留出集上证明答案质量提升；它只减少重复上下文和可审计的注入噪声。
