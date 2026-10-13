# 长期记忆无证据拒答边界

日期：2026-10-11；第二十二轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只收敛长期记忆读取门的 `skip` 原因和最终提示边界：当本轮没有可采纳的长期记忆时，系统必须说明原因，不能把缺失的长期记忆当作已知事实；若当前仍有文档或多模态证据，不能因为记忆缺失而阻断这些独立证据。此次不修改冻结数据、gold、模型、训练、调参或外部存储协议。

验收条件：

1. 读取门在“无候选、候选全部被拒绝、预算截断、读取异常”四类状态下返回稳定的 `decision_reason`。
2. `memory_gate.decision=skip` 时，最终学习上下文不再注入长期记忆正文，并明确提示“无法从当前长期记忆确定”的边界。
3. 旧的无 `memory_gate` 上下文保持兼容；文档和多模态证据仍可独立进入回答链路；执行完整 `scripts/check.ps1`。

## 外部机制与本地缺口

- [LongMemEval 固定仓库 commit](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 将长期记忆能力、时间/知识更新和无证据边界作为独立评测关注点。本轮只借鉴“记忆不可采纳时必须显式暴露边界”的工程原则，不导入其数据、不宣称取得其分数。
- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的事实时间窗口和 provenance 分离原则继续适用于本地记忆读取：`skip` 的原因是读取决策元数据，不把不存在的事实补进正文。

原有缺口是：`gate_memory_context()` 只有 `decision=skip`，没有说明是没有候选、全部被拒绝、超预算还是读取异常；`build_learning_context()` 也没有把这个状态传给模型。因此模型可能看到空上下文，却无法区分“本轮没有长期记忆”与“长期记忆内容为空”。

## B0 与候选实现

### B0

- 失败优先回归：`tests/test_memory_gate.py -k abstention_reason`。
- 旧实现结果：**1 failed, 17 deselected**；读取门没有 `decision_reason`，访问字段直接 `KeyError`。
- 失败优先回归：`tests/test_rag_prompting.py -k abstention_boundary`。
- 旧实现结果：**1 failed, 111 deselected**；空学习上下文没有任何长期记忆缺失提示。

### Candidate

- `core/memory_runtime.py` 为读取门增加稳定原因：`memory_context_admitted`、`no_memory_candidates`、`no_admissible_memory`、`memory_context_budget`。
- 读取决策摘要把 `decision_reason` 纳入 digest；同一输入的审计摘要包含同一拒答边界，原因变化不会被静默折叠。
- `core/mcp_server.py` 的记忆读取异常回退明确返回 `memory_context_unavailable`。
- `core/rag_engine.py` 在显式 `skip` 时 fail-closed：清空长期记忆 capsule 和补充记忆，只保留安全状态说明；提示模型涉及长期记忆的事实应明确说明无法从当前长期记忆确定，同时允许本轮文档或多模态证据独立使用。原因代码采用白名单映射，不把任意上下文内容拼入系统边界。

## 验证证据

- 新增候选回归：读取门 **1 passed**；提示边界 **1 passed**。
- 记忆运行时与 gate 扩大回归：**39 passed**。
- RAG 提示构建完整回归：**112 passed**。
- 学习 HTTP/反思/记忆有效性/冲突相关回归：**17 passed, 1 warning**。
- `compileall core tests`：通过。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1741 passed, 1 warning，422.65 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/memory_runtime.py` = `C5F4BFB97181F36E5B7295AC938F96C17B57C2E700434CC8261850A129A9FF19`
  - `core/mcp_server.py` = `73C680484729C02ACFEB7F018C371C2E3CDA9F8D75C19D13F9C3FA54B8AAA1B3`
  - `core/rag_engine.py` = `7A7CACEBDBC368E4D59DB587C77516A72F71666A7341EB13D89866BB12FF6C62`
  - `tests/test_memory_gate.py` = `9ACA97D2A5C4E351C475CFB5A6945C12E9C6EF17E6E0597E1F84DFF1013DC031`
  - `tests/test_rag_prompting.py` = `19CA45CE104D1BCB137FBBF3C0BD19626BE06D1A948743DBD9AABC30EBF6174B`

## 边界与下一轮

本轮证明的是记忆上下文的可审计 abstention 边界，不是模型最终答案一定拒答：没有文档/多模态证据时，真实开放式拒答率和拒答正确率尚未评测；有文档证据时，系统仍允许回答，长期记忆缺失提示不会替代文档证据。没有 LongMemEval 分数、开放式跨会话答案质量、自动记忆抽取质量、真实 Redis/PostgreSQL、断电恢复、跨主机 exactly-once 或真实 Tauri/WPS UI/E2E 证据。

下一轮继续检查研究窗口的长期记忆路由：论文、实验对比、会议行动项三类窗口是否不仅检索关键词不同，而且在记忆 scope、workflow namespace、episodic 事件和拒答边界上产生可观察且可回归的差异；仍遵循先 B0 失败回归，再做最小实现。
