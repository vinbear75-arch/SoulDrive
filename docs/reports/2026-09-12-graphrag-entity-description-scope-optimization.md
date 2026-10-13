# SoulDrive GraphRAG 实体描述范围安全降级

日期：2026-09-12

## 本轮结论

本轮确认图存储虽然按 chunk 过滤实体，但实体 `description` 仍是一个未绑定来源的聚合字符串。一个实体同时出现在项目 A、B 时，最后写入的描述可能来自 B；项目 A 的 GraphRAG 构建若只保留 A 的 chunk，仍会携带这段无法证明属于 A 的描述。

## 外部实践对照

- [Microsoft GraphRAG 的默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 将实体描述从 TextUnit 子图汇总，并保留实体的 `text_unit_ids`；本轮确认 SoulDrive 当前描述尚未做到同等粒度。
- [Microsoft GraphRAG 的实体增量合并](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/index/update/entities.py) 将实体合并视为可追溯的数据更新，而不是简单以最后一次写入覆盖；本轮先采用本机项目范围内的最小 fail-closed 保护。
- [Graphiti 的 group/episode 节点边界](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py) 和 [无范围关系失效反例](https://github.com/getzep/graphiti/issues/1728) 进一步说明图事实必须带可验证的来源范围；本轮不扩大到新的图数据库依赖。

## 失败优先证据

- 新增 `test_project_scope_drops_entity_description_without_matching_provenance`，先复现项目 A 只保留 `a-1` 时仍得到项目 B 描述的行为。
- 修复后受限项目/全局图索引检测到实体仍有未进入当前范围的 chunk 时，会保留实体和关系结构，但清空无法按 chunk 证明归属的 `description`。

## 最小实现

- `core/graph_rag.py::_scope_graph_snapshot()` 在项目或 global-only 作用域下比较实体原始 chunk 集与本次允许 chunk 集；只要存在未纳入当前范围的证据，就不把未绑定的聚合描述带入该索引。
- 普通无范围图快照不改变；实体名称、关系、chunk 关联和文档证据仍保留。
- 没有引入 Agentic RAG、图数据库或新的存储 schema；未来若评测证明描述召回损失，再设计 `entity-description ↔ text_unit` 的逐条 provenance。

## 验证证据

- GraphRAG 定向回归：`26 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1186 passed, 1 warning`；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。
- 既有 FastAPI/Starlette `httpx` 弃用警告和 Vite 大 chunk 警告保持原状。

## 明确边界

- 这是对未绑定实体属性的安全降级，不是实体解析、实体消歧、社区摘要或 GraphRAG 召回质量提升结论。
- 当前图存储仍使用按名称合并的实体节点；只有在下一轮建立逐 chunk 描述来源并完成同条件评测后，才考虑恢复更丰富的作用域内描述。
- `scripts/check.ps1` 不证明真实 Tauri UI、安装包、跨机器迁移或线上部署行为。
