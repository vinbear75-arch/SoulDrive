# 普通聊天同名文档兼容边界优化

日期：2026-09-15  
范围：普通 `/chat` 的 name-only 文档选择与 RAG 入口

## 发现的真实缺口

前一轮已让普通聊天在携带文档 ID 时校验显示标签，但只带 `document_names` 的旧客户端仍绕过了入口验证。项目内存在两个同名文件时，RAG 的名称过滤会同时命中两个对象，无法证明用户选择的是哪一个；旧实现仍返回 200 并调用 RAG。

## 外部实践对照

- [Graphiti 的 group 与 episode 设计](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 把记忆/来源放入显式分区，并让派生事实回指原始 episode；名称不是唯一身份。
- [Letta MemFS 的分层记忆](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将始终可见的核心内容与按需发现的外部内容分开，避免在边界不清时把全部内容注入上下文。
- [Mem0 的检索元数据](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将用户/项目等标识作为可过滤 metadata，而不是只依赖展示文本。

本轮没有废弃旧 name-only 协议，也没有让普通聊天强制所有客户端立刻升级；只在名称无法唯一确定对象时拒绝猜测。

## 失败优先验证

新增 `test_chat_rejects_name_only_duplicate_documents_before_rag`：

1. 构造同一项目内两个 ID 不同、展示名同为 `同名论文.pdf` 且都已索引的对象。
2. 旧实现返回 200，Fake RAG 被调用。
3. 修复后入口返回 409，给出“同名文档不唯一，请使用文档 ID 后重试”，Fake RAG 未被调用。

## 实现

- 普通聊天现在把 `document_name`、`document_names` 和 `document_ids` 统一送入既有 `_validate_workflow_document_inputs()`。
- name-only 仅在本地文档库中唯一、已索引且属于当前项目时继续；同名、缺失、未索引或跨项目对象均在 RAG 前 fail-closed。
- 唯一名称的兼容路径仍保留，但其证据状态仍只能称为展示标签级，不冒充对象级 provenance。

## 验证证据

- 失败优先：旧实现 `200`，修复后 `409`，且 RAG 调用标记保持 `False`。
- 文档身份/工作流定向回归：`35 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1649 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python compileall 与锁定 Cargo 构建通过。
- Vite 既有单 bundle 体积警告仍在，当前产物约 `587.48 kB`。

## 边界

- 这轮证明的是同名选择不会静默扩大 RAG 证据范围，不证明唯一 name-only 路径具有对象级来源身份。
- 仍未证明模型答案准确率、召回率、真实 Tauri/WPS UI/E2E、跨主机 exactly-once、真实 Redis/PostgreSQL 或外部模型服务行为。
