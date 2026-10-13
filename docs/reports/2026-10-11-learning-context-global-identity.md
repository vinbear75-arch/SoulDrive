# 学习上下文全局记忆身份一致性

日期：2026-10-11；第二十九轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮承接 learning context 的项目、窗口和技能范围绑定，继续检查记忆条目的 `scope` 与 `project_id` 是否自洽。目标是拒绝同时声明 `scope=global` 和具体项目身份的矛盾记录；合法全局记忆仍必须使用 `project_id=null`，合法项目记忆仍必须匹配当前项目。

本轮不修改记忆存储 schema、历史数据、冻结评测集、gold、模型、索引或外部后端，也不把本地结构门当作跨存储 exactly-once 证明。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 group/episode provenance 表明范围身份应随事实记录保留，不能只依赖展示文本；本轮把这一要求落实到最终 RAG prompt 的 memory 准入边界。
- [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的 metadata filtering 机制提示过滤条件需要在组件边界保持一致；本地 LearningStore 已有 `scope/project_id` 字段，但旧 prompt gate 未拒绝 global/project identity 矛盾的 legacy 记录。
- [LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 的跨会话记忆边界被用于保持当前项目/窗口身份约束；本轮没有把其论文评测分数外推为本项目质量结论。

## B0 与 Candidate

### B0 反例

新增反例向 `build_answer_messages()` 传入一条 `scope=global`、但带有 `project_id=project-a` 的记忆，同时当前请求为 `project-b` 且允许全局记忆。旧实现只检查 `scope=global` 和 `include_global_memory`，会把矛盾记录写入 prompt；失败优先结果为：**1 failed, 121 deselected**。

### Candidate 实现

- `_learning_memory_scope_is_allowed()` 对 `scope=global` 增加身份一致性要求：`project_id` 必须为空；否则 fail-closed。
- `scope=project` 仍要求请求带项目身份且记录 `project_id` 精确匹配；workflow fact 仍额外要求 workflow 身份匹配。
- 既有 capsule/item 过滤、memory gate、审计与 scope mismatch 观测保持不变；合法全局记录和合法项目记录不改变行为。
- 没有迁移、删除或改写历史异常记录，避免把结构校验误当成数据修复。

## 验证证据

- 失败优先反例：**1 failed, 121 deselected**；修复后身份边界定向回归：**5 passed, 117 deselected**。
- RAG prompt、memory gate、memory runtime、learning validator 组合：**171 passed in 1.80s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1755 passed, 1 warning，413.36 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前 SHA-256：
  - `core/rag_engine.py` = `BC70AFF885100DAD536DD63498F7D00F5153F8A59B6FE30824E414B15EF9BC5A`
  - `tests/test_rag_prompting.py` = `6C747BA1B3EE33F21AE245BAD78AE04A9F49636593FC3D6F7C07B4B7D782572A`

## 边界与下一轮

本轮只证明本机最终 prompt 入口会拒绝明显矛盾的 global/project memory metadata；不证明历史库已经清理、不证明外部 Redis/PostgreSQL 投影一致、不证明跨进程或跨主机 exactly-once、不证明开放式长期记忆答案质量，也不证明真实 Tauri/UI/E2E。现有工程门禁中的 Starlette/httpx 弃用提示和 Vite 单 chunk 体积提示仍是非阻断警告。

下一轮继续审计结构化 memory capsule 的“预渲染 text 与 item metadata”是否始终一一对应，避免 item 已通过范围门但 capsule text 仍夹带未绑定内容；若能构造真实反例，再按 B0 → 最小修复 → 全量门禁推进。
