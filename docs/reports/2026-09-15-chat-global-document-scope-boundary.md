# 普通聊天全局文档与项目文档范围闸门

日期：2026-09-15  
范围：无项目会话中的文档对象选择、全局 RAG 边界和同名对象兼容

## 发现的真实缺口

普通聊天已有文档身份校验，但此前只有在请求带 `project_id` 时才执行。无项目会话仍可携带项目文档 ID；普通查询通常会被 global-only 检索过滤，显式全局查询却可能把“项目对象选择”带入没有项目边界的请求，错误只能在检索阶段表现为无证据或范围不清。

本轮全量回归还暴露了一个兼容性事实：交互上下文会将重复展示名去重，但保留多个对象 ID。因此“两个同名对象 ID + 一个展示标签”是合法的普通聊天输入，不能沿用研究窗口的严格一对一标签协议。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 将 episode 按 `group_id` 分区，并让事实回指原始 episode；来源分区先于检索。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将核心记忆和按需发现内容分层，强调明确边界后再注入上下文。
- [Mem0 memory flow](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将用户/项目等标识作为可过滤 metadata，而不是依赖展示文本推断范围。

本轮没有引入新的图数据库、Agentic RAG 框架或远程服务；只把已有文档目录的 scope、index 状态和对象 ID 校验前移到普通聊天入口。

## 失败优先验证

新增 `test_chat_rejects_project_document_selection_without_project_scope_before_rag`：

1. 构造无 `project_id` 的普通聊天，请求选择一个属于项目 `p1` 的文档对象。
2. 旧实现返回 200，Fake RAG 被调用。
3. 修复后入口返回 409，提示该文档不属于当前项目，Fake RAG 未被调用。

全量回归另发现并固定 `test_chat_passes_request_scoped_interaction_context_to_rag`：两个同名全局对象可以共享一个去重后的展示标签，只要每个 ID 都能在全局文档目录中被验证。

## 实现

- `_validate_workflow_document_inputs()` 增加全局范围校验选项；项目范围为空时只允许 `scope=global` 的已索引对象通过，项目对象必须绑定显式项目。
- 普通聊天同时校验 `document_name`、`document_names`、`document_ids`，在进入 RAG 前拒绝缺失、未索引、跨项目和未知对象。
- 研究窗口仍使用严格的名称/ID 数量及逐项匹配；普通聊天允许一个展示标签广播到多个同名对象 ID，但每个 ID 仍需单独存在、已索引且范围合法。

## 验证证据

- 失败优先：旧实现 `200`，修复后 `409`，且 RAG 未调用。
- 文档身份与学习/聊天交叉定向：`166 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1650 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python compileall 与锁定 Cargo 构建通过。
- 当前冻结评测：记忆 `7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口角色 `6/6`；episodic runtime `5/5`。
- Vite 既有单 bundle 体积警告仍在，当前产物约 `587.48 kB`。

## 边界

- 这轮证明的是文档对象范围错误在 RAG 前被拒绝，不证明模型答案准确率、文档召回率或唯一 name-only 路径的对象级 provenance。
- 全局对象仍只能属于全局索引；若用户要读取项目对象，必须先选择/创建对应项目会话。
- 仍未证明真实 Tauri/WPS UI/E2E、跨主机 exactly-once、真实 Redis/PostgreSQL 或外部模型服务行为。
