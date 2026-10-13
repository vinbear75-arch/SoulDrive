# SoulDrive 检索反馈工作流命名空间优化

日期：2026-09-15

## 结论

本轮修复了检索诊断反馈的作用域缺口：此前反馈只区分 `global/project`，同一项目中“论文研究窗口”确认的查询改写可能被“实验对比窗口”复用。现在反馈额外绑定白名单 `workflow_id`，查询时只读取当前窗口的专属反馈和同项目/全局的通用反馈；未绑定窗口的查询只读取通用反馈。

这使“选择不同会话窗口确实产生不同作用”落到检索反馈这一条实际链路，而不是只停留在提示词或 UI 标签层。窗口角色仍由既有 `paper_review`、`experiment_compare`、`meeting_actions` 契约约束，未引入任意自由字符串。

## 外部实践对照

- [Mem0 How It Works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将用户、代理、运行和元数据范围作为记忆检索边界；本轮将同样的范围身份思想用于检索反馈，并保留 global/project 两级兼容。
- [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 在写入和查询时保留 scope/entity 约束，并通过确定性标识避免重复；本轮把窗口身份纳入反馈摘要键，继续使用 SQLite 权威记录和既有幂等回放。
- [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) 将长期记忆区分为按命名空间组织的事实集合；本轮采用更窄的业务命名空间，只隔离检索改写，不扩大到新的记忆后端或重量级框架。

## 失败优先验证

新增 `LearningStoreTests.test_retrieval_feedback_isolated_by_workflow_namespace`：在同一项目写入论文窗口、实验窗口和通用三条反馈，修复前接口不接受 `workflow_id`；实现后分别查询两个窗口，必须得到各自专属项加通用项，未绑定窗口不得看到专属项。

## 实现与验证

- `core/learning_store.py`：为旧 `retrieval_feedback` 表执行幂等列迁移；反馈摘要键、保存、查询、撤销、健康检查均携带窗口身份；查询排序保证专属窗口反馈优先于通用反馈。
- `core/mcp_server.py`：诊断检索和反馈 API 接受同一白名单 `workflow_id`，审计回执及诊断结果保留该字段；工作流变体与反馈改写在同一查询请求中合并。
- `souldrive-ui/src/App.tsx`：检索测试和“记住改写”提交当前窗口，已生效反馈显示窗口标题，按钮明确区分当前窗口与通用提示。
- `tests/test_mcp_server_indexing.py`、`tests/test_mcp_server_learning.py`：覆盖工作流变体、旧适配器安全回退、API 保存/查询/撤销隔离。
- `souldrive-ui/scripts/learning-review.test.mjs`：校验前端实际携带和展示 `workflow_id`。
- 定向后端回归：`26 passed, 1 warning`；前端学习审查：`4 passed`。
- 完整 `scripts/check.ps1`：后端 `1630 passed, 1 warning`；前端生产构建通过，8 组 UI 脚本合计 `62 passed`；Python compileall 与锁定 Cargo 检查通过。Vite 保留既有单 bundle 体积警告（约 `585.53 kB`）。
- 冻结记忆质量：`7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`，`stale_or_forbidden_leakage_rate=0.0`。
- 冻结 GraphRAG 路由：`4/4`；episodic workflow routing：`5/5`；研究窗口角色：`6/6`，各项子指标均为 `1.0`。
- 真实本地 GraphRAG acceptance：`index_ready`、`candidate_all_completed`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid` 等验收项全部为 `true`。全局查询仍按已验证的受控回退列出多来源证据，不能据此宣称模型已经产出高质量跨文档综合答案。原始结果：[graphrag_acceptance_after_presentation_scope_guard_20260915.json](../../eval/results/graphrag_acceptance_after_presentation_scope_guard_20260915.json)。

## 边界

- 这是检索反馈的命名空间隔离，不等于不同窗口已经拥有完全不同的模型、工具权限或数据集；这些仍由 workflow playbook、会话绑定和项目过滤分别保证。
- 只做精确窗口隔离，不自动把历史通用反馈推断成某个窗口的专属反馈；旧反馈保留为通用项。
- 不证明反馈改写带来的召回率、MRR、答案事实准确率或 p95 延迟提升；需要冻结评测集上的对照实验。
- 不证明跨主机 exactly-once、真实 Redis/PostgreSQL、真实 Tauri/WPS UI/E2E 或真实模型质量。
