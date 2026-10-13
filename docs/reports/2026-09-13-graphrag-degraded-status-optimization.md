# GraphRAG 部分失败状态可见性优化

日期：2026-09-13

## 对照实践

- [Microsoft GraphRAG Indexing](https://microsoft.github.io/graphrag/index/overview/) 把实体/关系抽取、社区检测、社区报告和向量写入作为可观察的索引工作流，而不是一个不可拆分的“完成/失败”黑盒。
- [Microsoft GraphRAG API overview](https://microsoft.github.io/graphrag/examples_notebooks/api_overview/) 展示按 workflow 读取结果和错误；本轮把这一思想压缩成适合单机项目的全局/项目构建状态。
- [Graphiti silent background failure issue](https://github.com/getzep/graphiti/issues/1707) 说明异步图构建返回成功但后台失败会造成静默数据损失；本轮优先补“可查询、可定位”的状态，不引入后台队列。

## 失败优先发现

- SoulDrive 同一轮索引会先生成 `global_shared`，再按项目生成 GraphRAG 社区索引。
- 某个项目构建异常时，原实现只在 `project_indexes[]` 留下 `failed`，顶层 GraphRAG observation 仍为 `completed`；诊断接口选择全局 observation 时无法表达“部分完成”。
- UI 只有“构建中/失败/未构建/可用”几种显示，无法区分全局可用但项目图谱不完整。

## 本轮修改

- `_build_graphrag_index()` 统计失败项目；全局成功但任一项目失败时返回 `status=degraded` 和 `failed_project_ids`，全局本身失败也不会伪装成 completed。
- `/diagnostics/graphrag` 传递 `graphrag_index_build_degraded` 与失败项目 ID；单项目查询仍优先显示该项目自己的失败状态。
- UI 增加“部分完成（有项目图谱失败）”提示；普通向量/关键词索引的顶层 `indexing.status=completed` 保持原语义，GraphRAG 派生状态单独可见。

## 验收证据

- GraphRAG/索引/项目诊断定向 `96 passed, 1 warning`，覆盖全局成功+项目失败和诊断 API/UI 状态契约。
- 最新 `scripts/check.ps1` 已通过后端 `1335 passed, 1 warning`、前端构建及 8 组测试 `60 passed` 和 Tauri `cargo check --locked`；前端 `GraphRagDiagnostics` 类型已同步声明 `degraded` 状态。

## 边界

- `degraded` 只是状态诚实化，不是 GraphRAG 质量提升、增量图谱原子更新或自动补偿。
- 仍没有把全局图谱、项目图谱、实体关系和社区报告做成跨存储事务；失败项目需要后续重建。
- 这不等于 Agentic RAG，也不改变查询路由、项目范围、证据门和引用校验。
