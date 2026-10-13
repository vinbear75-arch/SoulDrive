# 学习上下文记忆胶囊文本绑定

日期：2026-10-11；第三十轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮继续检查最终 RAG prompt 的 `memory_capsule` 边界。胶囊同时携带预渲染 `text` 和结构化 `items`；目标是显式项目/窗口范围下，预渲染文本不能比已通过范围门的 item 多出未绑定内容。

本轮不修改记忆存储 schema、冻结评测集、gold、模型、索引或外部后端，也不迁移历史胶囊。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 episode/group provenance 强调事实与来源身份一起传递；本轮把结构化 memory id 作为预渲染文本进入 prompt 的最低绑定凭据。
- [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的 metadata filtering 机制提示组件边界不能只信任已经渲染好的文本；本地缺口是 scope 过滤了 item，却没有检查 text 是否包含额外行。
- [LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 只作为跨会话记忆边界参考，本轮没有把其评测结果外推到 SoulDrive。

## B0 与 Candidate

### B0 反例

构造显式 `project-a/paper_review` 上下文：`items` 只有一条当前项目记忆，但 `memory_capsule.text` 额外夹带一行没有任何准入 memory id 的 `project-b` 内部口径。旧实现只在 item 级别过滤，仍把整段 text 写入 prompt；失败优先结果为：**1 failed, 122 deselected**。

### Candidate 实现

- 新增 `_learning_memory_capsule_text_is_bound()`，收集已准入 item 的 `memory_id/asset_id`，要求显式范围下的每一条非空预渲染文本行都包含对应的 `memory_id=` provenance token。
- 只要任一行无法绑定，整段预渲染 text fail-closed；这样宁可暂时不注入胶囊，也不把无法证明来源的额外行当作记忆事实。
- 原有 item scope mismatch 仍清空 text；无显式范围的旧兼容调用保持原行为，避免把没有请求身份的 legacy 结构直接误改成新协议。
- 由 `build_memory_capsule()` 生成的正常文本本来就带 `memory_id=` provenance；合法项目约束、记忆去重和 capsule 预算路径未改变。

## 验证证据

- 失败优先反例：**1 failed, 122 deselected**；修复后胶囊/项目约束/技能组合：**4 passed, 119 deselected**。
- RAG prompt、memory runtime、memory gate、learning validator 组合：**172 passed in 3.64s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1756 passed, 1 warning，415.74 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前 SHA-256：
  - `core/rag_engine.py` = `C92CC5CA301C0F346A616D58F34DB106E8FF36E834057C4ECDC9FFEEDEB770E8`
  - `tests/test_rag_prompting.py` = `825E5D393CC91DAF8C0D3CA0ADAE0E2A955B12A90BFE6278AA21858655CB005B`

## 边界与下一轮

本轮只证明显式范围下预渲染胶囊的每行至少绑定到一个已准入 memory identity；不证明文本内容与 item 内容的密码学等价，不证明历史胶囊迁移、真实 Redis/PostgreSQL、跨主机 exactly-once、开放式记忆答案质量或真实 Tauri/UI/E2E。当前工程门禁的 Starlette/httpx 弃用提示和 Vite 单 chunk 体积提示仍为非阻断警告。

下一轮继续审计 memory gate 的 `accepted_memory_ids`、capsule `items` 与最终 prompt 去重是否保持同一顺序/集合，重点寻找“审计说已接受但最终注入了另一条同名/同展示名记忆”的反例。
