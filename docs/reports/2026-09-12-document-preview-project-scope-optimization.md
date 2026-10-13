# 文档预览项目范围优化

日期：2026-09-12

## 结论

文档预览现在沿用检索和 PPT 证据的项目范围规则：`/documents/preview` 接受可选 `project_id`；无项目上下文遇到项目绑定父块时返回范围冲突，不把项目私有索引降级成无范围预览；项目上下文只读取同项目和全局父块。前端从项目空间打开文档时会自动携带当前项目 ID。

## 外部实践对照

- [Open WebUI file access control](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/access_control/files.py) 以文件 ID 和知识库访问关系先完成读取授权，再进入文件内容操作；不能把文件名当作授权依据。
- [Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py) 先生成用户可访问文件集合，之后才支持 ID、路径和文件名解析，并对重复文件名要求更具体的引用。
- [Qdrant exact payload filtering](https://qdrant.tech/documentation/search/text-search/text-filtering/) 将 ID/范围字段作为精确 keyword 条件，而不是用展示名称模糊推断范围。

本地实现仍保持轻量：没有引入 RBAC 或新存储，只复用已存在的父块 `project_id` 元数据和当前项目状态。

## 失败优先验证

新增 `tests/test_mcp_server_indexing.py::McpServerIndexingTests::test_indexed_document_preview_requires_scope_for_project_bound_same_name`：写入两个项目的同名父块，验证无范围预览返回 `scope_error` 且不返回正文；项目 A 预览只包含项目 A 内容，不包含项目 B 内容。

保留通过的既有回归：全局父块预览按 `parent_index` 顺序返回；加密文档本体存在多个同名对象时仍拒绝猜测并返回 `409`。

## 回归证据

- 文档、项目、PPT API 与预览组合回归：`67 passed, 1 warning`。
- 前端文档范围回归：`16 passed`。
- 最新完整 `scripts/check.ps1`：后端 `1166 passed, 1 warning`；前端生产构建通过，7 组测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明父块预览的项目隔离与调用方范围传递，不证明多用户权限、跨存储事务或真实 UI/E2E。没有项目上下文的项目材料会安全失败；调用方必须在项目空间中打开，或把文档归为全局后重建索引。
