# SoulDrive GraphRAG 文档范围身份与删除保真优化

日期：2026-09-12

## 本轮结论

本轮把 GraphRAG 的文档身份从“来源文件名单独唯一”收紧为“项目范围 + 来源文件名”唯一。这样不同项目可以同时拥有同名文件，图索引不会因后写文档覆盖先写文档而丢失来源范围；加密图存储在裁剪部分 chunk 后也不会把项目文档降级成全局文档。

## 外部实践对照

- [Graphiti 的 edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 将 episode/group 作为事实来源与关系解析的上下文；本轮将同一类范围身份下沉到本地 `graph_documents` 文档键。
- [Graphiti 的作用域失效反例](https://github.com/getzep/graphiti/issues/1728) 说明图事实在失去端点/范围约束时会错误影响无关事实；本轮因此优先修复文档身份和删除路径的范围保真，而没有先扩大多跳或 Agentic RAG 能力。
- [Microsoft GraphRAG 的 Document/TextUnit provenance](https://microsoft.github.io/graphrag/index/default_dataflow/) 要求图节点、文本单元和来源文档之间可回溯；本地仍以 chunk 为最小证据单元，但不再允许同名跨项目文档共享一个持久主键。

## 失败优先证据

- `test_graph_documents_keep_same_source_filename_per_project` 先复现本地图存储写入 `same.pdf`（项目 A、项目 B）后只剩项目 B。
- `test_secure_graph_documents_keep_same_source_filename_per_project` 先复现加密图存储使用仅由文件名派生的 `document_id`，同样覆盖项目 A。
- `test_secure_graph_chunk_pruning_preserves_document_project_scope` 先复现加密存储裁剪一个 chunk 后文档 `project_id` 变成空字符串，随后 `remove_project()` 无法命中该文档。
- `test_legacy_graph_document_schema_migrates_to_project_scoped_identity` 验证旧版 SQLite `source_filename` 主键表能够迁移，并同时保留已有范围和新增同名项目文档。

## 最小实现

- 本地 `graph_documents` 增加稳定 `document_id`；新写入按规范化 `project_id + source_filename` 生成。已有表首次打开时迁移到新表结构，展示用的 `source_filename` 不变。
- `remove_chunk_ids()` 按 `document_id` 更新/删除，避免同名文档被一起修改。
- `SecureGraphStore` 的加密文档 ID 同样绑定项目范围；对旧 ID 采用同来源同项目清理兼容重复，避免升级后残留双份文档。
- 加密文档裁剪时保留 `project_id`，因此部分证据删除不会把项目资产变成全局资产。
- 没有引入 Neo4j、Graphiti、Leiden 新依赖，也没有改变实体/关系抽取和 GraphRAG 路由策略。

## 验证证据

- GraphRAG 图存储专项：`16 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1185 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。
- 既有 FastAPI/Starlette `httpx` 弃用警告和 Vite 大 chunk 警告仍存在；本轮未把它们混入 GraphRAG 质量结论。

## 明确边界

- 本轮解决的是图文档持久身份、项目范围和删除保真，不等于图实体描述已经按项目隔离；实体/关系仍以 chunk provenance 过滤，下一轮继续审计同名实体的属性混写风险。
- 不宣称 GraphRAG 召回率、社区摘要质量、答案准确率或 Agentic RAG 已达标；这些仍需要冻结评测集上的 B0/B1/候选对比。
- 旧数据库迁移只在本地 SQLite schema/加密存储契约内验证，不等于真实用户历史数据备份恢复、安装包升级或跨机器迁移已验证。
