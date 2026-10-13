# 普通聊天文档身份闸门优化

日期：2026-09-15  
范围：普通 `/chat` 请求进入 RAG 前的显式文档选择校验

## 发现的真实缺口

研究窗口已经拒绝“文档 ID 与显示标签不一致”的请求，但普通 `/chat` 仍可携带 `active_context.document_ids` 和错误的 `document_names` 直接进入检索。旧路径可能返回 200，并把名称与 ID 的空交集留给 RAG 处理，用户只能看到“没有证据”而不是明确的选择错误。

## 外部实践对照

- [Microsoft GraphRAG 输入对象 schema](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 将稳定对象 ID、标题和文本作为不同字段，派生的 TextUnit 通过 ID 回指来源。
- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 先建立文档与派生单元的关联，再进入检索和生成；来源身份不应在生成阶段靠标题猜测。
- [Haystack 元数据过滤](https://docs.haystack.deepset.ai/docs/metadata-filtering) 要求过滤条件落在结构化 metadata 上。本地对应为：普通聊天若已经提供显式文档 ID，先复用工作流身份验证，再允许请求进入 RAG。

本轮没有改变普通聊天的 name-only 兼容协议，也没有引入重量级 Agentic RAG；只把已有对象身份规则前移到普通聊天入口。

## 失败优先验证

新增 `test_chat_rejects_explicit_document_id_label_mismatch_before_rag`：

1. 构造同一项目内 `document_id=object-a`、显示标签为另一文档的普通聊天请求。
2. 修复前请求返回 200，RAG 仍被调用，错误在检索后才暴露。
3. 修复后请求在入口返回 409，响应包含 `document_context.ready=false`、具体 mismatch 问题和下一步动作；RAG 不再被调用。

## 实现

- `core/mcp_server.py` 在普通聊天的工作流计划检查之后，针对带有 `active_context.document_ids` 或 `active_context.document_names` 的非工作流请求复用 `_validate_workflow_document_inputs()`。
- ID 与名称数量不一致、逐项 basename 不一致或对象不存在时，统一 fail-closed；返回结构化 `document_context`，让 UI 能要求用户刷新选择。
- 旧 name-only 普通聊天仍保留兼容协议，但现在也会验证名称是否唯一、已完成索引且属于当前项目；唯一名称路径仍不宣称对象级 provenance。

## 验证证据

- 普通聊天/工作流定向回归：`34 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1648 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python compileall 与锁定 Cargo 构建通过。
- Vite 仍有既有单 bundle 体积警告，当前产物约 `587.48 kB`。

## 边界

- 本轮证明的是请求身份错误会在 RAG 前被拒绝，不证明模型答案准确率、召回率或跨模态引用质量。
- 唯一名称的普通聊天仍无法提供对象级 provenance；同名 name-only 选择现在会 fail-closed 并要求使用 ID。是否让所有旧客户端都升级到强制 ID，仍是后续兼容性决策。
- 仍未证明真实 Tauri/WPS UI/E2E、跨主机 exactly-once、真实 Redis/PostgreSQL 或外部模型服务行为。
