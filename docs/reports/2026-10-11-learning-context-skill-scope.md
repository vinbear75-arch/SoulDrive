# 学习上下文技能范围与窗口边界

日期：2026-10-11；第二十八轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮承接前两轮的 memory/episodic 范围绑定，检查同一 `learning_context` 中的技能流程和技能执行计划。目标是保证 `project-b/meeting_actions` 不会把 `project-a/paper_review` 的流程指令带入 prompt，同时不误删当前请求可用的全局技能。

本轮不修改技能存储 schema、技能正文、冻结评测集、gold、模型、索引或外部后端。

## 外部机制与本地缺口

- [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的 metadata filtering 说明过滤必须在组件边界保留；本轮把已经存在于 LearningStore/MCP 的技能 `scope/project_id` 过滤再落实到 RAG prompt 边界。
- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 group/episode provenance 与 [LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 的跨会话边界共同提示：窗口身份不只约束事实记忆，也约束可执行流程上下文。
- [Self-RAG 论文 v1](https://arxiv.org/abs/2310.11511v1) 的受控检索/反思思路在本地只用于“先守住上下文准入边界”；本轮没有引入额外 agent 或让模型自行决定技能权限。

## B0 与 Candidate

### B0 反例

新增跨项目技能回归：输入只携带 `skills` 列表，不在顶层声明项目，但技能自身声明 `project-a`；当前请求为 `project-b/meeting_actions`。旧实现只做技能正文安全检查，不做 project/workflow scope 检查，反例先失败：**1 failed, 119 deselected**，并把“其他项目流程”写入 prompt。

### Candidate 实现

- 新增 `_learning_skill_scope_is_allowed()`，复用 `is_skill_visible_in_workflow()`，并按技能自身 `scope/project_id` 过滤项目技能；全局技能继续允许进入项目请求，但 workflow-scoped global skill 仍必须匹配当前窗口。
- RAG 绑定层同时过滤 `skills` 和 `skill_execution`，因此已匹配的执行计划不会绕过技能列表的范围门。
- prompt 最终构建再次复用同一 scope helper，保留独立调用 `build_learning_context()` 时的防御性边界。
- 技能范围被过滤时，RAG 绑定观测记录 `filter/skill_scope_mismatch`，并累计既有 `learning_context_scope_mismatches`；没有项目/窗口身份的兼容调用仍不凭空猜测范围。

## 验证证据

- 技能范围与审计定向回归：**2 passed, 119 deselected**；memory/episodic/RAG/技能组合：**147 passed**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1754 passed, 1 warning，414.67 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- `git diff --check`：通过；仅有现有工作区 LF/CRLF 转换提示，无 whitespace error。
- 质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/rag_engine.py` = `9D655F2D5E40E73162B2344AA0653EB8A10CE2FFDDD2191A3DF28CC8FEFA3AC4`
  - `tests/test_rag_prompting.py` = `D15E8DE13D61135F1482C8C7BE343F05980634BE1096D5EB0643293C43A6F0CC`

## 边界与下一轮

本轮只证明本机 RAG prompt/技能上下文对明显跨项目、跨窗口技能注入会拒绝或过滤；不证明技能正文的开放式安全性、技能执行结果质量、外部观测系统、跨主机 exactly-once、真实外部后端或真实 Tauri/UI/E2E。没有 `interaction_context` 的直接 `build_learning_context()` 兼容调用仍允许无身份技能，这是兼容边界而不是生产范围证明。

下一轮继续核对：全局 memory、全局 skill、项目约束和 episodic activity 在“当前项目为空/只给 workflow/明确 global”几种组合中的语义是否一致；并检查最终 citation namespace 是否携带足够的 project/document identity，避免范围过滤正确但引用仍只剩展示名。
