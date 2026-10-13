# PPT 对象身份证据覆盖率优化

日期：2026-09-15  
范围：PPT 证据指标、同名文档来源归因、前端生成结果提示

## 发现的真实缺口

上一轮已经让 PPT 选择、证据收集和来源指纹支持显式 `source_document_ids`，但 `presentation_evidence_metrics()` 仍按 `source_filename` 集合计算 `selected_source_coverage`。因此两个不同对象如果展示名相同，指标会把它们折叠成一个来源：只要页面出现一次同名文件，就可能错误地显示 100% 来源覆盖。

这不是答案事实正确性，而是产物审计指标失真；如果指标失真，用户会误以为 PPT 已经覆盖每个选中的对象。

## 外部实践对照

- [LlamaIndex CitationQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/citation_query_engine.py) 要求回答引用对应来源，而不是只报告检索到过某类文本。
- [LlamaIndex Node schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 将节点唯一 ID 与可供向量库过滤的 metadata 分开；展示字段不替代对象身份。
- [Microsoft GraphRAG 输入字段](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 保留输入对象的 `id`，文本单元再回指原始文档；来源覆盖应能回到原始对象，而不是只比较显示标题。

本轮只借鉴来源身份与引用可追溯原则，没有引入新的检索框架或指标依赖。

## 失败优先验证

新增同名双对象回归：两个记录都叫 `同名.pdf`，对象 ID 分别为 `object-a`、`object-b`，提纲只出现一次 `同名.pdf`。

- 修复前测试先失败：结果没有 `selected_source_label_count`，旧实现也无法区分两个对象。
- 修复后测试要求显示名覆盖为 `1/1`，对象身份覆盖为 `0/2`，状态为 `ambiguous_display_labels`；因此不会把展示名命中冒充成对象级完整覆盖。

## 实现

- `core/presentation_service.py`
  - 继续保留展示名口径 `selected_source_label_count`、`cited_source_count` 和 `selected_label_coverage`，用于兼容旧产物和解释页面显示。
  - 新增 `selected_identity_count`、`cited_identity_count` 和 `source_identity_coverage_status`。
  - `selected_source_coverage` 改为对象身份覆盖率：有显式 ID 时按对象 ID 计算；同名对象只有展示名引用时，不推断具体对象，状态为 `ambiguous_display_labels`。
  - 若提纲直接引用对象 ID，或展示名只唯一对应一个对象，则可以计入对象覆盖；无 ID 的旧请求明确标记为 `display_name_only`。
- `souldrive-ui/src/App.tsx`
  - PPT 结果提示改为显示“来源对象 `已归因/已选`”；同名展示标签无法精确归因时直接提示用户。
- `souldrive-ui/scripts/document-scope.test.mjs`
  - 增加 UI 静态回归，确保对象覆盖字段和歧义提示不会被删除。

## 验证证据

- PPT Service/MCP 定向：`46 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1645 passed, 1 warning`；前端生产构建通过；UI 脚本合计 `65 passed`；Python compileall 与锁定 Cargo 构建通过。
- Vite 仍有既有单 bundle 体积警告，当前产物约 `587.12 kB`，本轮未借机扩展到代码分包。
- 冻结记忆质量：[memory_quality_after_presentation_identity_metrics_20260915.json](../../eval/results/memory_quality_after_presentation_identity_metrics_20260915.json)：`7/7` 场景、`8/8` 查询、`memory_recall_at_k=1.0`、`stale_or_forbidden_leakage_rate=0.0`。
- 冻结 GraphRAG 路由：[graphrag_route_after_presentation_identity_metrics_20260915.json](../../eval/results/graphrag_route_after_presentation_identity_metrics_20260915.json)：`4/4`，路由、图门和信号合同准确率均为 `1.0`。
- 研究窗口角色：[workflow_roles_after_presentation_identity_metrics_20260915.json](../../eval/results/workflow_roles_after_presentation_identity_metrics_20260915.json)：`6/6`，角色、就绪、检索画像、输出合同、引用命名空间、RAG 门和来源多样性均为 `1.0`。
- episodic workflow routing：`5/5`。

## 边界与下一步

- 这轮证明的是 PPT 来源覆盖指标不会把同名对象静默折叠，不证明 PPT 内容事实准确率、视觉布局质量或 WPS/Tauri 真实 UI/E2E。
- 旧的仅文件名请求仍可运行，但对象级覆盖只能得到 `display_name_only`；如果同名对象无法唯一映射，必须停在歧义状态。
- 当前 PPT 混合选择普通文件与安全对象时，前端为保持标签与 ID 对齐会回退到文件名路径；下一轮继续检查该回退是否应改为显式拒绝或稀疏 ID 合同，避免安全对象身份在混合选择中被悄悄降级。
- 真实 PostgreSQL/Redis、跨主机 exactly-once、模型答案准确率和真实 WPS/Tauri UI/E2E 仍未证明。
