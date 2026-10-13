# SoulDrive GraphRAG 项目删除的派生 provenance 清理

日期：2026-09-14

## 本轮结论

修复了图存储 `remove_project(project_id, chunk_ids=None)` 的接口漏洞：调用方省略 chunk 列表时，LocalGraphDB 和 SecureGraphStore 现在会先从该项目的图文档记录推导 chunk，再清理实体、关系和实体属性来源。共享实体只保留其他范围的 chunk，不再因仅删除 `graph_documents` 而留下项目孤儿事实。

## 外部开源实践对照

- [Graphiti group deletion](https://github.com/getzep/graphiti/blob/main/server/graph_service/zep_graphiti.py) 按 group 收集并删除边、节点和 episode，而不是只删除一个可见目录记录。
- [Graphiti group-scoped clear](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/graph_data_operations.py) 将作用域作为派生图数据清理条件。
- [Microsoft GraphRAG provenance dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将文档与 text unit/图元素关联，删除或增量更新时需要沿来源关系处理派生数据。

SoulDrive 当前不引入图数据库；本轮把同一原则落到已有 `graph_documents`、chunk provenance 和本地图存储上。

## 失败优先证据

修复前，两个新用例均失败：

- LocalGraphDB 删除项目但不传 `chunk_ids` 后，项目 chunk 仍出现在实体和关系 provenance 中。
- SecureGraphStore 同样只删除了项目文档，未清理加密实体/关系的项目 chunk。

## 最小实现

- `remove_project()` 收到空列表时查询项目范围图文档的 `chunk_ids_json`/加密文档 payload。
- 先复用既有 `remove_chunk_ids()`，再删除项目图文档和失效 GraphRAG 派生索引。
- 有其他范围 provenance 的实体/关系继续保留；没有剩余 provenance 的实体/关系按既有逻辑删除。
- 空项目、重复删除和显式传入 chunk 列表的既有契约保持不变。

## 验证证据

- 失败优先修复后：LocalGraphDB 和 SecureGraphStore 删除用例 `2 passed`。
- 图存储、加密图存储、GraphRAG、图抽取器：`62 passed`。
- 项目/索引删除相关定向回归：`57 passed, 1 warning`。
- `git diff --check`：通过；LF/CRLF 提示为工作区既有换行提示。
- 本轮代码变更后的仓库级 `scripts/check.ps1` 已通过：后端 `1422 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `61 passed`；Python `compileall` 与锁定 Cargo 通过。既有 Vite bundle 大小提示与 Starlette/httpx 弃用警告仍存在。

## 边界

- 这是本机 SQLite/加密图存储的按项目派生清理，不是跨存储原子删除或外部 exactly-once。
- 若进程在多个存储之间中断，已有项目删除 receipt/重试路径仍是恢复边界；本轮没有扩大到分布式事务。
- 本轮不证明 GraphRAG 召回率、答案准确率、实体消歧、社区摘要或 Agentic RAG 质量。
