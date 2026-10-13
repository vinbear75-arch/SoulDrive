# 记忆上下文 provenance 元数据优化

## 结论

本轮补齐了记忆系统最后一段可追溯性缺口：`learning.injected_into_rag` 审计事件虽然已经记录被接纳的 memory ID，但进入模型上下文的常驻记忆胶囊此前只保留标题和正文，模型无法区分记忆的稳定身份、项目范围、有效期和来源消息位置。现在胶囊保留这些非正文元数据，同时明确不注入 `source_excerpt` 原始对话片段。

## 外部实践对照

- [Letta archival memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py)：归档检索结果同时返回唯一 ID、时间戳、内容和 tags；SoulDrive 复用其“正文与身份元数据并存”的边界。
- [Letta archival memory search](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py)：检索接口支持 tags 与时间范围过滤，结果按语义相关性返回带身份信息的记忆。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)：记忆按 group 隔离，并用 episode UUID 追踪事实来源；事实同时保留 valid_at/invalid_at 的时间语义。
- [Mem0 memory operations](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx)：metadata 用于后续过滤和检索，过期时间决定记忆是否进入搜索结果。

本项目不引入外部记忆服务或图数据库，只沿用现有加密 SQLite、项目范围和时态字段，把安全的 provenance 标签加入本地胶囊。

## 实施

- `core/memory_runtime.py`
  - `build_memory_capsule` 在每条记忆后追加 `memory_id`、`scope`/`project_id`、`valid_from`、`invalid_at` 和最多 8 个来源消息索引。
  - 只读取结构化 provenance 字段，不读取或拼接 `source_excerpt`；原始对话不会因为胶囊构建被重新注入。
  - 保持原有优先级、字符预算和最多记忆条数不变；预算不足时沿用已有截断逻辑。

- `tests/test_memory_runtime.py`
  - 新增回归，验证元数据存在、项目范围和有效期不丢失，且 source excerpt 不进入胶囊。

## 验证

- 记忆、聊天注入和 RAG 提示词组合回归：`146 passed, 1 warning`。
- 本轮失败测试先确认旧实现只输出 `标题: 正文`，随后最小实现通过同一回归。
- 项目统一检查已通过：后端 `1068 passed, 1 warning`，Python compileall 通过；前端构建通过，7 组 UI 测试共 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- provenance 标签只供模型理解和审计定位，不是文档证据引用；系统提示仍禁止把长期记忆当作 `[E#]` 文档证据。
- 时间字段当前以本地存储的数值形式进入提示词；历史时点的自然语言解释仍由现有 `memory_time_scope` 提供。
- 这项改动证明了上下文身份可追溯，不等同于记忆事实本身正确；冲突裁决、人工确认、项目隔离和有效期过滤仍是独立门槛。
