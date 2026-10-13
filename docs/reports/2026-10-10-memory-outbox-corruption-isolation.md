# 记忆同步 outbox 损坏载荷隔离

日期：2026-10-10。本轮只处理本地加密 memory sync outbox 中单条 payload 不可解密时对后续读取/claim 的放大，不删除损坏事件、不伪造 payload、不改变冻结评测数据。

## 外部机制与当前缺口

本轮对照 [DBOS transactional outbox](https://docs.dbos.dev/python/examples/outbox) 与 [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery)：数据库事实提交后，外部通知步骤应作为可恢复的后续步骤；失败应保留可观测状态并允许恢复，不能让一次不可恢复的步骤异常静默污染或阻断其他独立工作。SoulDrive 不引入 DBOS，而是把这一原则对齐到已有 SQLite + AES-GCM outbox。

`LearningStore.memory_health()` 已经能够逐条捕获不可解密的 `learning_sync_outbox` payload，并报告 `sync_payload_unreadable`。但普通的 `pending_memory_syncs()` 和无项目范围的 `claim_pending_memory_syncs()` 直接解密返回值：一条损坏 ciphertext 会让整个读取/claim 抛出 `ValueError`。原有 SQL `LIMIT` 还会使坏事件占据小批量的首位，健康事件无法补位。

## B0 反例

新增两个回归后，暂时回放修补前实现运行：

- `test_pending_reads_skip_corrupt_event_and_fill_requested_limit` 失败：`pending_memory_syncs(limit=1)` 从 `InvalidTag` 穿透为 `ValueError`。
- `test_unscoped_claim_skips_corrupt_event_without_starving_healthy_event` 失败：无范围 claim 在解密首个损坏事件时中止，后续健康事件未被领取。

这证明问题是当前实现的真实读取/claim 阻断，而不是只由测试构造出来的预期行为。

## 最小候选改动

- `pending_memory_syncs()` 按创建顺序扫描 pending 事件；单条 payload 解密失败就跳过，继续寻找健康事件，直到填满请求的有效 `limit`。
- 无范围 claim 去掉会被坏事件占位的 SQL `LIMIT`，先逐条验证 payload，再收集最多 `limit` 个健康事件并领取；损坏事件保持原记录、未增加 attempts、未获得 claim。
- `memory_health()` 继续作为损坏事件的可观察入口，返回固定的不可读问题和 opaque `event_id`；本轮没有删除、自动修复或伪造成功回执。
- 项目范围 claim 原本已有逐条解密跳过逻辑；本轮只让公共读取和无范围 claim 与相同边界一致。

## 验证记录

- 候选定向：`tests/test_learning_outbox_corruption_isolation.py`，`2 passed in 0.69s`。
- 扩大邻域：新增回归 + `tests/test_learning_store.py` + `tests/test_mcp_server_learning.py`，`243 passed, 1 warning in 270.58s`。警告是既有 `StarletteDeprecationWarning`。
- 全量 `scripts/check.ps1`：退出码 0；后端 `1730 passed, 1 warning in 431.91s`；记忆质量 `7/7` 场景、`8/8` 查询、`memory_recall_at_k=1.0`、stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；workflow 角色 `6/6`；前端生产构建成功，UI 脚本 `66 passed`；`cargo check --locked` 与编译检查通过。Vite chunk 体积提示和既有 Starlette/httpx 弃用警告未改变。
- 全量日志：`build/quality-gates/memory-outbox-corruption-isolation-full-check-20261010.log`。该 transcript 由 PowerShell 保存，工程摘要以本轮终端输出为准。

## 评测与冻结边界

本轮没有读取或改写待审核种子的 gold、训练数据、提示词或调参配置；没有下载或替换候选来源。动态复核 `eval/fixtures/business_seed_review_v0.1/manifest.json` 声明的 7 个来源哈希应保持 `frozen_sources_match=True`。

本轮只证明同进程本地 SQLite、可构造的 AES-GCM payload 损坏和有效批量补位；不证明真实 Redis/PostgreSQL、磁盘/断电恢复、跨进程或跨主机 exactly-once、损坏 ciphertext 的自动修复、长期 outbox 性能、RAG 召回或模型答案质量。扫描全部 pending 记录是为了在坏事件前后填满有效 limit，未来若 outbox 规模扩大应单独评估分页/游标性能。
