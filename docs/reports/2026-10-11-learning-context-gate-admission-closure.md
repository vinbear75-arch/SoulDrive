# 学习上下文记忆准入结果闭合

日期：2026-10-11；第三十一轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮检查 `memory_gate.accepted_memory_ids`、`memory_capsule.items`、补充 `memories` 与最终 prompt 是否使用同一准入集合。目标是 gate 只接受 A 时，capsule 或补充列表中的 B 不能因为范围相同、标题相似或仍带有合法元数据而进入回答。

本轮不修改记忆存储 schema、冻结评测集、gold、模型、索引、历史数据或外部后端，也不把本地集合过滤写成跨存储事务性证明。

## 外部机制与本地缺口

- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 的 run/lineage 观测边界提示“记录的准入结果”和“实际消费的对象”必须能按稳定身份对齐；本轮把 gate 的 accepted IDs 作为 prompt 入口的权威集合。
- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 provenance/group 约束与 [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的 metadata filtering 共同说明：范围通过不等于读取门通过，最终消费边界还要复核稳定对象身份。
- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 仅作为状态/重放身份的外部参考；本轮没有引入 Temporal 或宣称本地代码具备跨主机 durable execution。

## B0 与 Candidate

### B0 反例

构造 gate 只声明 `accepted_memory_ids=[memory-approved]`，但 capsule 仍携带同项目的 `memory-unapproved`。旧实现只做安全与 scope 检查，没有把 gate accepted IDs 复用到 capsule，导致未获准入记忆进入 prompt；失败优先结果为：**1 failed, 123 deselected**。

### Candidate 实现

- 新增 `_learning_memory_gate_ids()`，区分“没有 accepted IDs 字段”的兼容旧上下文与“字段存在但集合为空”的明确空准入结果。
- `build_learning_context()` 对 capsule items 和补充 memories 都复用 `_learning_memory_identity()` 与同一个 accepted-ID 集合；不在集合中的条目被过滤，若 capsule 因此失配，预渲染 text 继续 fail-closed。
- 现有 project/global/workflow scope、敏感内容和 capsule 文本 provenance 门保持不变；正常 `gate_memory_context()` 生成的 accepted IDs 与 capsule items 本来一致，因此正常路径不丢数据。

## 验证证据

- 失败优先反例：**1 failed, 123 deselected**；修复后 gate/capsule 反例：**1 passed, 123 deselected**。
- RAG prompt、memory runtime、memory gate、learning validator 组合：**173 passed in 2.93s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1757 passed, 1 warning，462.07 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前 SHA-256：
  - `core/rag_engine.py` = `C80C0E30B74059BDFB2BA7F6DBE5532F7FF2DB61580C929F64728A1A5A36B6AB`
  - `tests/test_rag_prompting.py` = `29878069133C11648BD2AD8CF3CEA525CA83D43FF1427A04A356BEDD48914052`

## 边界与下一轮

本轮只证明本机最终 prompt 会复用 gate 的稳定 memory identity 集合；不证明 gate 与外部 Redis/PostgreSQL 事务同步、不证明跨主机 exactly-once、不证明历史上下文迁移、不证明长期记忆开放式答案质量或真实 Tauri/UI/E2E。Starlette/httpx 弃用和 Vite 单 chunk 体积仍为非阻断警告。

下一轮继续核对 gate 的 `decision_digest` 是否覆盖最终 accepted-ID 集合和最终 capsule text；重点构造“输入 gate 收据已生成，但上下文被修改后 digest 仍可被接受”的反例，避免审计收据与实际 prompt 脱节。
