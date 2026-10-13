# 记忆胶囊预算下的 provenance 保留优化

日期：2026-09-12

## 结论

本轮修复了记忆胶囊在字符预算不足时丢失来源标识的边界。旧实现先拼接“标题 + 正文 + provenance 尾缀”，超预算后从整行开头截断；当正文较长时，`memory_id`、项目范围和来源消息 ID 会一起被截掉。这样虽然 `items` 仍保留结构化记忆对象，但实际送入 RAG prompt 的文本无法再回指该记忆，削弱了引用审计和人工复核。

现在预算分配优先保留 provenance 尾缀，再压缩正文；在能够保留正文前缀和完整 provenance 的预算范围内，文本长度仍不超过 `max_chars`。预算极端不足时，优先保留 provenance 开头的 `memory_id`，不把被截断的正文误当作完整事实。

## 外部实践对照

- [Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 将 scope、检索分数、阈值和可解释检索结果作为显式字段，而不是只返回一段不可定位的文本。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 保留事实的 `valid_at`/`invalid_at` 双时间语义，并将来源 episode 与检索结果一起暴露，说明时间和来源元数据不能因上下文压缩而静默丢失。
- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 与 [memory architecture](https://github.com/letta-ai/skills/blob/main/letta/letta-api-client/memory-architecture.md) 对核心记忆块设定硬边界，同时把长期归档内容与当前上下文分层；本轮只借鉴“硬预算下仍保留结构化身份”的原则，没有引入新的记忆框架或自动摘要器。

## 失败优先验证

- 新增 `MemoryRuntimeTests.test_build_memory_capsule_preserves_provenance_when_content_is_truncated`。
- 修复前：`max_chars=150` 时，长正文先占满整行，输出不包含 `memory_id=mem-trace-preserve`、`scope=project:project-a` 或 `source_message_ids=msg-source-a`，测试失败。
- 修复后：同一预算仍满足 `len(text) <= 150`，且三个来源字段都保留；正文被压缩而不是来源尾缀被截断。

## 实现与验证

- 修改 `core/memory_runtime.py::build_memory_capsule`：单独计算正文前缀和 provenance 尾缀，把剩余预算优先分配给正文压缩；极小剩余预算使用 provenance 前缀的保守截断。
- 记忆/RAG/学习组合回归：`245 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1144 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。
- 警告为环境中的 Starlette/httpx 弃用提示，不是测试失败。

## 边界

- 这是 prompt 文本的来源可追溯性修补，不证明检索命中率、引用正确率或答案准确率提升。
- `items` 仍是结构化事实源的引用；本轮没有把原始 `source_excerpt` 放回上下文，也没有扩大项目范围。
- 如果调用方给出的 `max_chars` 小于必要 provenance 本身的长度，系统只能保留其前缀，无法保证所有来源字段完整；生产默认预算仍为 900，冻结业务评测应单独覆盖更紧预算。
- 真实模型、Redis、Tauri/WPS、安装包和跨存储 exactly-once 不由本轮自动检查证明。
