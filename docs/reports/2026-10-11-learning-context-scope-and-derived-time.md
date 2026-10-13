# 学习上下文范围绑定与记忆派生时间线

日期：2026-10-11；第二十六轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮处理两个由交叉审计发现的本地缺口：

1. RAG 入口不能只相信传入 `learning_context` 自己声明的项目/窗口；它必须按当前请求的 `interaction_context` 对 memory 和 episodic context 做 fail-closed 绑定。
2. 记忆 canonical row 与 `learning_chunks` 派生 row 的更新时间必须使用同一单调边界，否则 top-k tie 排序会把刚编辑的旧 memory 排在较新的 memory 后面。

本轮不修改冻结评测集、gold、模型、索引内容、业务标签或外部后端。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 将 episode/group provenance 作为读取范围的一部分；本轮把同一原则落到本地项目与 workflow 身份，不引入图数据库。
- [LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 将跨会话、时间更新和历史边界作为独立能力评估；本轮只补本地读取边界，不把 scope 通过写成跨会话答案质量。
- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 的运行/指标/产物 lineage 提醒派生结果不能脱离其事实来源单独排序；本轮只修本地 canonical memory 与加密检索 chunk 的时间线一致性，不引入 MLflow。

## B0 与候选实现

### RAG 范围绑定 B0

- 跨窗口 episodic 反例：`paper_review` 请求携带 `meeting_actions` 活动时，旧入口会把会议活动用于检索计划。
- 跨项目 memory 反例：`project-b` 请求携带 `project-a` 的结构化 memory capsule 时，旧提示会显示私有内容。
- 两项反例在候选前均失败；失败证明不是“窗口没有差异”，而是请求级绑定缺失。

### RAG Candidate

- `core/rag_engine.py` 增加请求级 learning context binding：顶层 project/workflow 不匹配时清空学习上下文并记录 `learning_context_scope_mismatch`；episodic metadata 和逐事件 project/workflow 不匹配时跳过事件。
- 绑定后的 memory 继续复用既有 `_learning_memory_scope_is_allowed`，不复制一套新的记忆权限规则。
- `rag.retrieval_planned` 增加 `episodic_selection_reason`，可区分 `scope_mismatch` 与普通 `not_selected`。
- 旧的只带正文范围、结构化 item 不带 scope 的项目约束 fixture 被补为显式结构化字段；不能仅凭正文标签证明项目归属。

### 记忆派生时间 B0

- 新增反例先失败：将较新 memory 的 `learning_chunks.updated_at` 人为设为高于所有 canonical memory 后，再编辑旧 memory；旧实现只比较 `memories.updated_at`，搜索仍返回较新的 memory。
- 失败结果：**1 failed, 100 deselected**。

### 记忆派生时间 Candidate

- `update_memory()` 的单调更新时间边界同时读取 active `memories` 与 active memory `learning_chunks` 的最大值，再用 `math.nextafter` 推进。
- 这样 canonical row 与派生检索 row 同时越过已有时间线，保持 score tie 下“最近编辑优先”的可复现排序。

## 验证证据

- 范围绑定定向回归：**7 passed, 111 deselected in 0.15s**。
- RAG 提示全量：**118 passed in 1.80s**。
- 学习存储全量：**101 passed in 24.21s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1751 passed, 1 warning，413.86 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/rag_engine.py` = `496D3710DC016D91EA7BAC96A78F114BF8F7CB9062CF5F3FF4BC07B32373C694`
  - `tests/test_rag_prompting.py` = `9D8B67B1F2D20F81A2B4882052F789B07AB98A50A43CF93089203E7D88FFA9AC`
  - `core/learning_store.py` = `C6624F22276C4CB838D0FB9062F170BF73F9AFA865574199934443668FDB1446`
  - `tests/test_learning_store.py` = `675267F00AA06237BD05F17BE92F57C433E3429ECF5EC006DDF895ED7575D3D6`

## 边界与下一轮

本轮只证明本地 RAG 入口会拒绝明显跨项目/跨窗口的传入学习上下文，并保证本地 memory/chunk 时间排序一致；不证明调用方永远提供正确 scope、不证明历史日志迁移、真实外部图/向量后端、跨主机 exactly-once、开放式记忆答案质量、LongMemEval 分数或真实 UI/E2E。无 project/workflow 身份的直接 `build_answer_messages()` 调用仍只能得到有限的兼容性保护，不应被当作生产读取权限门。

下一轮继续检查 workflow memory 与 episodic activity 的“无身份/全局身份”边界，以及 scope mismatch 是否贯通到 metrics/audit；随后再检查证据引用 namespace 是否和同一 binding 一致。
