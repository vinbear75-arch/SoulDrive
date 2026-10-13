# GraphRAG 本地一跳邻居扩展优化记录

日期：2026-09-14  
范围：GraphRAG local 路由、跨社区关系、原始证据回读和检索观测。

## 1. 本轮发现的真实缺口

SoulDrive 已有 `global`、`local` 和普通 `hybrid` 路由，也在 GraphRAG 构建阶段保存了 `community_relationships`。但 `local` 查询此前只对社区标题、报告、实体和关系文本做词项排序，没有利用跨社区关系把“命中实体的邻接社区”纳入有限检索结果。结果是关系型问题可能只看到起点社区，漏掉已经在图索引中存在的下游节点。

## 2. 对照的开源机制

- [Microsoft GraphRAG overview](https://github.com/microsoft/graphrag/blob/main/docs/index.md) 将 Local Search 描述为围绕具体实体向邻居和关联概念展开，同时把 Global、Local、DRIFT 和 Basic 作为不同成本/能力路径。
- [GraphRAG query API](https://microsoft-graphrag.mintlify.app/api/query) 明确区分 local search 与 DRIFT search；DRIFT 还包含动态推理和迭代遍历。
- [GraphRAG local search documentation](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 支持以实体、关系和文本单元组合构建局部上下文；本轮只吸收一跳关系扩展，不复制完整 DRIFT 代理循环。

这些资料说明“local 路由应有邻居上下文”，但不证明 SoulDrive 需要引入 Microsoft GraphRAG 服务或通用 Agent 框架。

## 3. 已实现的最小改动

- 在 `GraphRAGIndex._retrieve_payload()` 的 `local` 路径读取已有 `community_relationships`，从直接命中的社区出发只扩展一跳。
- 邻居必须同时满足：关系端点属于已选社区、邻居存在于当前已加载的同一索引、关系/邻居实体与原始查询存在词项交集；不满足时保持原来的局部结果。
- 扩展总数受既有 `limit` 约束，不新增无限循环、不新增模型调用、不改变 global 路由。
- 扩展社区仍只提供 GraphRAG 导航并回读原始 chunk；最终文档证据继续经过既有项目范围、来源和 context gate，社区摘要不能直接成为事实引用。
- 结果和 RAG 观测增加 `graph_expansion.mode=one_hop`、扩展社区 ID 和数量，便于区分“直接命中”与“关系扩展”。

## 4. 失败优先与回归结果

先加入 Alpha → Beta 的跨社区关系反例，旧实现只返回 Alpha，测试按预期失败；修复后 Beta 在 `limit=2` 内进入结果，Gamma 等无关社区没有进入。

- GraphRAG 专项：`37 passed`；
- GraphRAG/RAG 提示/路由组合：`135 passed`；
- 冻结 GraphRAG 路由集：`4/4`，hybrid/local/global 路由和信号契约均通过；
- 关系扩展结果带 `graph_expansion=one_hop`，并在 RAG 观测中保留 `expanded_count`。

收尾完整工程门已通过：后端 `1395 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。该门不能替代真实图谱质量或人工答案评审。

## 5. 明确未宣称的内容

- 这不是完整 DRIFT Search，也不是多跳 Agentic RAG；当前仍是确定性一跳扩展，未引入 LLM 生成子问题、反馈循环或动态停止。
- 这不证明图实体抽取、关系正确性、社区划分或答案事实准确率提升；扩展依赖已有图索引质量。
- 这不改变项目/全局 scope、原始证据回读、引用门和 fail-closed 约束，也不证明跨进程 exactly-once、真实 Tauri/WPS UI/E2E 或外部服务可用性。
