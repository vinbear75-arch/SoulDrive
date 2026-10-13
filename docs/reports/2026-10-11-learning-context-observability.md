# 学习上下文范围拒绝的 Metrics/Audit 贯通

日期：2026-10-11；第二十七轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮承接第二十六轮的范围绑定修复，只处理一个可观测性缺口：跨项目或跨窗口的 `learning_context` 已经会在入口 fail-closed，但检索规划和完成轨迹仍可能只显示“0 条记忆”，无法区分“本来没有历史”与“历史因范围不匹配被拒绝”。

本轮验收条件：

1. 范围绑定结果在 `rag.retrieval_planned`、状态栏和 `rag.completed` 中使用同一份结构化结果。
2. 明显的 `scope_mismatch` / `event_scope_mismatch` 能进入运行指标；普通无记忆请求不增加该计数。
3. 不改变记忆正文、检索候选、冻结评测集、gold、模型、索引内容或外部后端。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 将 group/episode provenance 作为读取边界；本轮把范围绑定的决定也作为可追踪运行状态保留下来。
- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 强调运行、指标和产物 lineage 的关联；本轮让 RAG 的拒绝原因和完成轨迹使用同一 binding 摘要，而不是从计数反推原因。
- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 的状态/运行观测思路提示 durable 或可恢复流程必须区分状态结果与普通空结果；本轮只在本机运行指标和 best-effort audit 层落地，不引入 Temporal。
- [LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 将跨会话边界作为独立记忆能力；本轮只补轨迹可解释性，不宣称记忆答案质量或评测分数提升。

## B0 与 Candidate

### B0 反例

新增“跨项目 memory 被拒绝后仍应进入 RAG audit/metrics”的回归。旧实现没有 `learning_context_binding` 字段，先失败：**1 failed, 118 deselected**。这确认问题是运行轨迹缺字段，而不是测试只检查了提示词正文。

### Candidate 实现

- `_bind_learning_context_to_interaction()` 可向调用方返回 `none / allow / skip / filter` 与原因；顶层或 episodic 元数据不匹配使用 `skip/scope_mismatch`，逐事件范围不匹配使用 `filter/event_scope_mismatch`。
- `rag.retrieval_planned`、Agent 状态栏和 `rag.completed` 写入同一份 `learning_context_binding`，因此可以区分未提供、范围匹配、整体拒绝和逐事件过滤。
- 新增 `RuntimeMetrics.learning_context_scope_mismatches`，仅对两类范围原因累计；指标快照可由现有本机运行诊断接口读取。
- 绑定与拒绝内容保持原有 fail-closed 行为，不把被拒绝 memory 或跨窗口活动重新放入 prompt、检索变体或答案证据。

## 验证证据

- 新增范围拒绝 audit/metrics 回归及既有跨项目回归：**2 passed, 117 deselected**。
- RAG 提示、审计日志和 API 指标组合：**140 passed**。
- `git diff --check`：通过；仅有现有工作区的 LF/CRLF 转换提示，无 whitespace error。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1752 passed, 1 warning，414.06 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/rag_engine.py` = `A82A46DA831F79446809C4DFAB1C9732B8B5B0AD8EB34252A655ACB0FB8434AF`
  - `core/observability.py` = `83745A52FA3D15D725760C536B40614C23DC6E272B69B29031D4549A36ED8592`
  - `tests/test_rag_prompting.py` = `6F2B988E6DD2B79E3351CEE8B949F29E9FBDFA2B55C3EA71EADE68B0428303F0`

## 边界与下一轮

本轮只证明本机 RAG 入口的范围绑定决定能进入本机指标和审计；不证明外部观测系统、历史日志迁移、跨进程/跨主机 exactly-once、真实外部向量/图数据库、开放式记忆答案质量或真实 Tauri/UI/E2E。`build_answer_messages()` 的直接调用仍没有独立的 audit 事件，它只复用范围保护逻辑。

下一轮继续检查：无 `project_id` 的 workflow memory、显式 global memory 与 episodic activity 的边界是否一致；随后核对引用 namespace 是否携带同一项目/窗口 binding，避免“记忆范围正确但引用来源范围不明”。
