# 会话读取与 outbox 重放故障隔离

日期：2026-10-10。本轮检查会话读取在 durable outbox 重放异常下的边界，不改变会话正文、项目范围、claim 租约、重试次数或冻结评测数据。

## 外部机制与本机缺口

[Dapr transactional outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/)把已提交的业务状态与后台事件投递分开：状态提交成功后，应用可以继续，投递失败由后台重试。SoulDrive 的 conversation outbox 已有本地事实归档和派生投影，但 `ProductionSessionStore.load_context()` / `load_before()` 在读取事实前无条件调用 `replay_outbox()`；只要派生重放因为临时锁、连接或投影错误抛出异常，已落盘会话历史也不能读取。

本轮只验证并隔离这个读路径放大，不引入 Dapr 或新的重试框架，也不把“读取事实成功”写成“派生投影已完成”。

## 改动前验收条件

- `load_context` 和 `load_before` 各有一个临时 outbox 重放异常反例。
- 反例要求：异常先按当前实现抛出，证明读取路径确实被派生步骤阻断。
- 候选要求：保留本地事实读取，记录固定 warning；不吞掉事实读取本身的异常，不伪造 outbox 完成结果。
- 测试不读取待审核种子、论文正文或冻结答案，不修改冻结输入。

## 运行记录

- B0 反例：新增的 `load_context` / `load_before` 两个回归在未修复代码上均失败，都是 `OSError` 从 `replay_outbox()` 直接穿透；原有的两条失败收据隔离回归保持 `2 passed`。
- 最小候选：两个读入口分别捕获 outbox 重放异常并记录固定 warning，随后继续读取本地事实；没有捕获事实归档读取，也没有生成虚假的 replay completed 结果。
- 候选定向：`tests/test_conversation_outbox_failure_isolation.py` `4 passed`。
- 扩大邻域：新增回归 + `tests/test_conversation_store.py` + `tests/test_memory_runtime.py` 共 `120 passed`。
- 全量 `scripts/check.ps1`：后端 `1728 passed, 1 warning, 442.50s`，退出码 0；记忆质量 `7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`、stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；workflow 角色 `6/6`。前端生产构建成功，8 组 UI 脚本共 `66 passed`，锁定 Cargo 检查通过。既有 Starlette/httpx 弃用警告和 Vite chunk 体积提示仍在。
- 冻结输入复核：动态读取 `eval/fixtures/business_seed_review_v0.1/manifest.json` 并重新计算 7 个声明源的 SHA-256，`frozen_sources_match=True`；没有修改冻结输入、gold、评分或待审核种子。
- 关键文件 SHA-256：`core/state_backends.py`=`2d3e556b1c9fa1876f39602dad4be1b903e622bc791f9885a74d73efc4cf1577`；`tests/test_conversation_outbox_failure_isolation.py`=`8cab6fdaba2924fe67a7ceb76f9702b8c8404acf81ade5de8f2705b6b54e66c4`。

## 边界与下一轮

本轮只证明同进程内、读取本地事实时可隔离派生 outbox 重放异常；不证明真实 Redis/PostgreSQL、跨进程或跨主机 exactly-once、断电恢复、自动退避或模型/RAG 答案质量。归档事实读取本身的错误仍会暴露，不能把事实可读写成投影已完成。
