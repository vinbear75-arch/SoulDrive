# RAG 跨项目同名来源身份优化

日期：2026-09-14

## 结论

本轮修复了跨项目检索中的来源身份缺口：来源标题召回和可选的文档级 semantic profile 召回原先都可能只按文件名/标题合并记录。显式全库或跨项目比较遇到两个项目都存在 `report.pdf` 时，一侧候选会在 hybrid ranking 之前被覆盖，导致来源覆盖和比较证据不完整。

现在两条召回路径统一使用带范围的稳定身份：优先使用 `asset_version_id`、`document_id` 或 `asset_id`，并附加 `project_id`；没有稳定 ID 时，仍保留文件名作为同一范围内的兼容回退。全局文档使用独立的 `global` 命名空间。展示标签仍可相同，但不会再把不同项目的逻辑来源压成一个候选。

## 外部实践对齐

- [Microsoft GraphRAG BYOG](https://github.com/microsoft/graphrag/blob/main/docs/index/byog.md) 将外部已有文档/实体身份作为图索引接入边界；本轮沿用项目已有的资产/文档身份，不把展示文件名当作跨项目主键。
- [LlamaIndex schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 和 [vector store metadata 工具](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/vector_stores/utils.py) 都保留 source/document metadata 供检索和追踪；本轮只借鉴身份与 metadata 分层，没有引入新的向量或图框架。

## 失败优先与实现

1. 新增来源标题回归：两个项目均有 `report.pdf` 时，旧实现返回 1 个候选；修复前失败，修复后保留两个项目。
2. 新增 source profile 回归：两个项目均有 `report.pdf` 时，旧实现生成 1 个 profile；修复前失败，修复后分别生成项目 A/B 两个 profile。
3. `LocalKnowledgeBase._source_title_candidates()` 改用 `source_diversity_key()` 作为 profile key。
4. `_source_profile_records()` 和 `_annotate_source_profile_scores()` 使用同一带项目范围的身份，避免召回与评分投影使用不同 key。

## 回归证据

- RAG/知识库/安全检索定向：`95 passed`。
- 候选身份 fencing 后的 RAG/知识库/安全检索定向：`96 passed`；父文档范围校验加入后 GraphRAG/知识库/安全检索组合：`138 passed`。
- 候选身份 fencing 后完整工程门禁：后端 `1524 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。
- 前端仍报告既有单 chunk 大小警告；本轮没有借此扩大前端构建策略。

父文档范围校验随后以失败优先用例验证：跨项目父文档不会再进入 child context；该改动的完整工程门禁为后端 `1525 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。

来源多样性身份统一后，RAG/GraphRAG/上下文门/知识库定向为 `262 passed`；最新完整工程门禁为后端 `1526 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。

父文档访问键随后按 `parent_id + project_id + document identity` fencing；相同父 ID 的跨项目候选不再被访问去重静默吞掉，范围不匹配时仍保留 child-only。新增父访问键回归后，RAG/GraphRAG/上下文门/知识库定向为 `263 passed`；最新完整工程门禁为后端 `1527 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。

本轮又修正验收脚本自身的来源计数：`scripts/eval_graphrag_acceptance.py` 原先仅按 `source_filename` 去重，两个项目同名 `report.pdf` 会在验收 JSON 中被错误记为一个来源；现在按 `project_id + asset_version_id/document_id/asset_id` 计数，并保留可读的范围标签，同时继续识别 GraphRAG 派生报告不是原始证据。失败优先测试先复现 `source_count=1`，修复后 `tests/test_graphrag_acceptance.py` 为 `4 passed`。

随后对既有冷构建工作区重新校验：旧派生 `global_shared` 索引被当前来源快照门正确判为 `stale`，没有被静默使用；仅重建该派生索引后，6 个文档、193 个实体、128 条关系、4 个社区、结构化报告降级均完成。真实 Ollama 验收三条查询最终通过：`index_ready=true`、`scope_freshness=verified`、普通问题保持 `hybrid`、全局问题启用 `global` GraphRAG、关系问题启用 `local` GraphRAG，三条回答均完成、带引用且未拒答。该验收结果写入临时 JSON，未覆盖历史验收证据。

验收脚本修复后的第一次完整工程门禁为后端 `1529 passed, 1 warning`，前端生产构建成功、8 组 UI 脚本合计 `62 passed`，`compileall` 与锁定 Cargo 通过。加入最终证据 ID 审计后再次完整门禁为后端 `1531 passed, 1 warning`，前端生产构建成功、UI `62 passed`，`compileall` 与锁定 Cargo 仍通过。警告仍是既有 Starlette/httpx 弃用提示和 Vite 单 chunk 体积提示。

随后继续收紧引用验收：验收脚本原先只判断回答中是否出现 `[E#]`，现在 `rag.retrieved` 和采用的 `rag.retrieval_retry` 审计事件携带最终证据 ID，验收要求回答中的 `[E/V/A/M]#` 全部属于该次最终证据集合；没有证据 ID 或引用越界即失败。失败优先覆盖未知编号与“采用重试后切换证据”两条回归，RAG/验收/提示词/安全检索定向为 `134 passed`。固定冷工作区真实 Ollama 验收中，三条回答的 `citation_binding` 均为 `true`，引用分别为 `E1,E4,E5`、`E1`、`E1,E2`，均属于该次最终证据 ID 集合。

## 边界

- 这证明同名跨项目来源不会在候选合并阶段丢失，不等于检索召回率或答案事实准确率提升；仍需在冻结评测集上重新测量。
- source profile recall 仍是显式可选能力，不因本轮修复而默认开启。
- 本轮未引入 Agentic RAG、多跳规划、Neo4j、Redis 或外部评测服务，也不证明真实 Redis/PostgreSQL、打包 Tauri/WPS UI/E2E 或跨存储 exactly-once。
- 父文档身份校验只在本地父文档/安全父文档读取入口生效；真实多进程索引并发和外部存储联调仍未完成。
- 真实 GraphRAG 验收使用固定 6 文档冷工作区和本机 `qwen3.5:9b`，只能证明本轮固定小语料的路由/引用闭环；不能外推到更大语料、答案正确率或自动社区摘要质量。
- 引用绑定门只证明编号与最终证据 ID 对齐，不等于证明引用内容支持每个事实；答案忠实度仍需冻结评测集和逐事实人工/模型评测。

## 后续跨项目 GraphRAG 范围修复

静态审计发现一个比来源去重更深的范围不一致：明确跨项目查询的向量检索会放宽到全项目语料，但 GraphRAG 原先仍只读取 `global_shared`（共享资料索引），因此关系型跨项目问题会漏掉项目资料对应的图社区。另一个边界是共享索引构建时，部分 chunk scope manifest 中缺失的 chunk 曾被当作全局资料纳入。

本轮按 GraphRAG 的 global/local 分层与 LlamaIndex 节点 metadata 过滤契约做了最小收敛：

1. 任何 `explicit_global_scope`（包括仍被路由为 `local` 的“跨项目影响链”问题）都让图检索与向量检索使用同一全项目边界；
2. 索引器在 scope manifest 完整时构建独立 `all_projects` 派生图索引，GraphRAG 通过 `retrieve_all_projects()` 显式读取；没有该索引时返回 `cross_project_graph_index_not_built`，不静默退回 `global_shared`；
3. 部分 manifest 中缺失的 chunk 不再默认视为全局；全项目索引缺少 scope metadata provider 时 fail-closed；
4. 普通项目内/未绑定共享资料查询的 `project:*` 与 `global_shared` 路径保持不变。

失败优先覆盖了跨项目全语料图索引、local-route 跨项目范围、部分 manifest、缺失 provider 和索引未构建五条边界；GraphRAG + indexer scope 定向回归为 `68 passed`。本段尚未声称真实工作区已由最新索引器重新构建 `all_projects`，完整工程门禁与冷工作区复验需在本轮代码稳定后重跑。

外部参考：[Microsoft GraphRAG BYOG](https://github.com/microsoft/graphrag/blob/main/docs/index/byog.md)、[Microsoft GraphRAG local search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)、[LlamaIndex node metadata](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 与 [LlamaIndex metadata filters](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/vector_stores/types.py)。

## 冷工作区实证（2026-09-14）

上述“待重跑”状态已由本轮实测更新。固定冷工作区 `eval/runtime/rag_cn_cold_candidate_20260913` 由当前索引器重新构建 `all_projects` 派生索引：6 个文档、200 个 chunk、200 条 scope metadata、193 个实体、128 条关系、4 个社区，scope manifest 完整，索引新鲜度为 `verified`。构建过程未覆盖原始资产，也没有把不完整的 scope manifest 当作全局资料。

随后用本机 `qwen3.5:9b` 实跑跨语料查询“跨项目分析 RAG 检索、图谱实体和社区之间的影响链”：路由为 `local`（查询意图仍带 `explicit_global_scope=true`），GraphRAG 观测明确为 `index_name=all_projects`、`project_id=null`、`global_only=false`、`scope_freshness=verified`、`source_freshness=verified`；返回 4 个社区、3 条原始 source evidence、2 个 source，回答非空且最终证据 ID 为 `E1..E6`，引用绑定闭合。

这次实证确认了“向量全语料边界”和“图全语料边界”已经接通，但冷工作区的 `project_ids` 为空，因此不能把它表述为两个真实项目内容之间的答案准确率证明；仍需在冻结的多项目评测集上测量 Recall/MRR、证据覆盖、串项目率、引用忠实度与延迟。最新 GraphRAG/indexer scope 定向回归为 `153 passed`（包含检索、安全检索、基础契约和诊断组合），全量门禁仍需随后重跑。

完整工程门禁随后通过：后端 `1536 passed, 1 warning`，前端生产构建成功，8 组 UI 脚本合计 `62 passed`，Python `compileall` 与锁定 Cargo 检查通过。警告仍为既有 Starlette/httpx 弃用提示和 Vite 单 chunk 体积提示；该门禁不等价于真实打包 Tauri/WPS UI/E2E、真实 Redis/PostgreSQL 或多项目答案准确率。

## GraphRAG 全语料索引的诊断闭环

再次对照 [Microsoft GraphRAG query overview](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md) 的全局/局部查询边界，以及 [LlamaIndex metadata filters](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/vector_stores/types.py) 对 metadata 过滤可观测性的要求，发现索引器虽然已生成 `all_projects`，诊断接口却只检查 `global_shared`。这会让运维面误报“全语料图索引未构建/不新鲜”，也无法看到 scope manifest 是否完整。

本轮最小修复：

1. `GraphRAGIndex.diagnostics(index_name=...)` 支持显式诊断命名索引；默认项目/共享路径保持兼容；
2. `/diagnostics/graphrag` 在无项目绑定时额外返回 `all_projects` 的 readiness、source/scope freshness、`build_observation` 和 `indexing_status`；构建中、失败、跳过、降级和运行态陈旧均保留明确状态；
3. 索引器的 `all_projects` 观测补充 `scope_manifest_complete`、`scope_metadata_count`、`chunk_count` 与 `project_ids`，用于解释“为什么可用/为什么降级”；
4. 诊断聚合对重复返回对象做隔离，避免旧适配器或测试替身返回同一 dict 时形成自引用 JSON。

失败优先覆盖了显式 `all_projects` 诊断和 HTTP 暴露两条链路；GraphRAG/indexer/MCP 项目与安全组合为 `122 passed`。这只证明状态面与范围验证面闭合，不证明真实多项目答案质量、真实 Redis/PostgreSQL、打包 Tauri/WPS UI/E2E 或跨存储 exactly-once。

后续记忆 projection replay 范围修复后的完整工程门禁仍保持通过：后端 `1545 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。该数字是全仓当前快照，不将记忆回归结果误记为 GraphRAG 答案质量证据。
