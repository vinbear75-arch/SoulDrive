# 会话 outbox：失败收据故障隔离

日期：2026-10-10。本轮只处理 `ProductionSessionStore` 的本地/ PostgreSQL conversation outbox 重放异常边界，不改变消息正文、项目范围、claim 租约、重试次数或冻结评测数据。

## 外部机制与本机缺口

[AWS Prescriptive Guidance 的 Transactional Outbox](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)说明 outbox 用于避免主数据写入与事件通知的双写不一致，并提醒消费者必须能承受重复事件；[Retry with backoff](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html)强调重试操作应具备幂等性。这里借鉴的是提交事实、独立重放、失败后可重试和下游幂等的边界，不引入 AWS 服务，也不宣称本地链路 exactly-once。

当前 `ProductionSessionStore._replay_outbox_locked` 对本地和 PostgreSQL 事件都已先 claim，再执行投影，成功后写 completed；投影异常时会写 pending 失败收据。但如果这次 pending 收据写入本身再次异常，异常会越过当前事件处理分支，阻止同一批后续独立事件继续重放。记忆 outbox 已有同类保护，但 conversation outbox 尚未对齐。

## 改动前验收条件

- 两个独立合成事件，第一条投影失败且失败收据写入抛 `OSError`，第二条投影正常；local 和 PostgreSQL 两条实现分别覆盖。
- 候选结果必须是 `completed=1/failed=1`，两条事件都被尝试；失败收据写入异常只能产生固定、不含异常正文的 warning，不能伪造完成状态。
- 现有 claim/lease 规则不变；没有真实 Redis/PostgreSQL、跨进程 exactly-once 或断电证明。
- 新测试使用临时 SQLite 或内存 fake，不读取待审核种子、论文正文或冻结答案，不修改现有冻结输入。

## 运行记录

- B0 反例：`tests/test_conversation_outbox_failure_isolation.py` 在未修复代码上 `2 failed`。两条失败均发生在失败收据写入抛出 `OSError` 后，后续事件没有继续重放。
- 最小候选：本地和 PostgreSQL 两个失败分支各自包住失败收据写入；写入失败只记录固定 warning，保留当前事件的 `failed` 计数，不把事件误报为 `completed`，也不改变下一轮重试条件。
- 候选定向：新增回归 `2 passed`；加入既有 conversation outbox/replay 与 memory outbox 相关用例共 `37 passed, 1 warning`。
- 扩大邻域：`tests/test_conversation_store.py tests/test_memory_runtime.py` 共 `116 passed`。
- 全量 `scripts/check.ps1`：后端 `1726 passed, 1 warning, 463.65s`，退出码 0；记忆质量 `7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`、stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；workflow 角色 `6/6`。前端生产构建成功，8 组 UI 脚本共 `66 passed`，锁定 Cargo 检查通过。既有 Starlette/httpx 弃用警告和 Vite chunk 体积提示仍在。
- 冻结输入复核：动态读取 `eval/fixtures/business_seed_review_v0.1/manifest.json` 并重新计算 7 个声明源的 SHA-256，7/7 匹配；没有修改冻结输入、gold、评分或待审核种子。
- 关键文件 SHA-256：`core/state_backends.py`=`c4e39e2b09123399faa2be749ef884a7679bed78f1facb9fce21ecd025cd1902`；`tests/test_conversation_outbox_failure_isolation.py`=`f594c2848485ee004f96f3185c202aa4dc1160b9f7f61300fba87d8d9227390c`。

## 边界与下一轮

本轮证明的是同一进程内、临时 SQLite/fake PostgreSQL 下，失败回执写入异常不会中止同一批后续独立事件；不证明真实 PostgreSQL/Redis、跨进程或跨主机 exactly-once、断电恢复、重试退避效果，也不证明 RAG/模型答案质量或真实 Tauri UI/E2E。后续继续检查其他 durable execution 回执与跨投影错误隔离，不把本轮工程门禁写成项目全部完成。
