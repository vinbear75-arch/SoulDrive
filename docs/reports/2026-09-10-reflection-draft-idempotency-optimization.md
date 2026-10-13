# SoulDrive 反思草稿生成幂等优化记录

日期：2026-09-10

## 优化目标

只解决一个可复现的本地问题：`/reflection/draft` 在网络重试或用户重复点击时，原流程会再次调用候选生成器，并可能产生多个内容相同的 pending review。这个问题会浪费本地模型调用，也会让人工审核列表出现重复项。

本轮不把“反思”扩展成后台自动 dreaming、自动合并或自由 Agent 循环；长期记忆整合仍保持人工 review 边界。

## 外部实践对照

- [Letta 的反思指导](https://github.com/letta-ai/letta-code/blob/main/src/agent/subagents/builtin/reflection.md)强调反思应引用已有记忆、避免重复，并通过独立维护流程改善记忆质量。
- [Mem0 的 add 记忆操作](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx)把新增、更新、删除和 no-op 作为可审计的操作结果，而不是无条件追加文本。
- [Mem0 并发重复写入案例](https://github.com/mem0ai/mem0/issues/6515)说明只在应用层先查询再插入会有 TOCTOU 窗口，数据库约束仍需要作为并发兜底。

对 SoulDrive 的最小映射是：草稿生成请求用输入摘要绑定幂等键，生成前先回放，落库再由局部唯一索引保护；不直接照搬外部记忆框架。

## 实现内容

1. `reflection_reviews` 增加可空的 `idempotency_key`、`idempotency_digest`，并建立非空键的局部唯一索引。
2. `LearningStore.create_review` 支持同键同输入回放、同键异输入冲突，并在事务中处理并发插入；持久化 candidate 可重新解密和返回。
3. 新增 `replay_reflection_draft`，让 `/reflection/draft` 在候选生成器之前完成廉价回放，成功重试不会再次调用模型。
4. API 接受标准 `Idempotency-Key` header，同时兼容 body `request_id`；两者不一致在会话、模型加载和 RAG 之前返回 `409`。
5. 前端按触发原因、项目和消息内容维持重试键；失败重试复用原键，输入指纹变化或成功后才生成/清理新键。

## 验收证据

- 新增 HTTP 回归验证：首次 keyed 请求创建一个 review；相同 key+相同输入返回同一 review 且 `replayed=true`；同键异输入返回 `409`；body/header 不一致返回 `409`；候选生成器调用次数保持为一次；pending review 只有一条。
- 后端定向：`tests/test_mcp_server_learning.py tests/test_learning_store.py`，`116 passed, 1 warning`。
- 前端 learning 回归：`3 passed`；生产构建通过。
- 全量 `scripts/check.ps1`：后端 `1103 passed, 1 warning`；Python compileall 通过；前端构建及 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机 SQLite 范围内的请求幂等，不代表模型供应商或跨主机服务 exactly-once。
- 未做语义相似自动去重；不同幂等键的相似候选仍需人工审核或后续有独立评测支持时再处理。
- 未新增后台调度器、向量数据库或记忆框架；当前证据证明的是重复草稿控制，不是长期记忆质量或答案准确率提升。
