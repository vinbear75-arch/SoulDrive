# 学习检索派生 chunk 损坏隔离

日期：2026-10-10。本轮只处理 `learning_chunks` 派生检索层的单条密文不可读问题，不改变权威 memory 事实、项目/workflow 范围、冻结评测集或答案质量结论。

## 外部机制与当前缺口

本轮对照 [Microsoft GraphRAG indexing dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/) 和 [GraphRAG indexing architecture](https://microsoft.github.io/graphrag/index/architecture/)：原始文档先变成 TextUnit，再派生实体、关系、社区报告和 embedding；派生项应保留 provenance，索引/查询层的故障不能被误认为原始事实已经消失。SoulDrive 不引入 GraphRAG 新框架，只把同一事实/派生边界应用到已有加密 `learning_chunks`。

当前 `LearningStore.search()` 对候选 chunk 直接解密，一条损坏的 AES-GCM payload 会让整次 memory/skill 检索抛出 `ValueError`，即使其他候选健康。`memory_health()` 只验证 memory chunk 是否存在、是否孤儿和范围是否一致，没有验证 chunk 密文可读性，因此不能向用户暴露可修复的 opaque chunk ID。

## B0 反例

两个行为缺口回归在修复前失败：

- 检索回归在 `InvalidTag` 处穿透为 `ValueError`，没有返回后面的健康 memory。
- doctor 回归得到 `degraded`，没有 `memory_chunk_payload_unreadable`；现有 pending sync warning 不能替代派生 chunk 损坏诊断。

另加一条边界回归：权威 `memory_versions` 密文损坏时必须继续抛出认证错误，不能被“跳过坏派生项”的容错逻辑吞掉。

## 最小候选改动

- `LearningStore.search()` 只对 `learning_chunks` 派生索引的单行解密捕获 `ValueError`；坏派生项跳过，健康候选继续评分、排序和返回。
- `memory_health()` 读取 memory chunk 的密文并校验认证；失败只报告 `memory_chunk_payload_unreadable`、chunk ID、memory ID、scope/project，不返回标题/正文，并保留其他孤儿/范围检查。
- 历史 `memory_versions` 仍直接解密；权威事实认证失败会显式失败，避免把事实缺失伪装成“没有检索结果”。
- 不自动删除、重建或覆盖坏 chunk；修复动作仍由后续索引/人工流程决定。权威 `memories` 密文损坏仍由事实读取路径暴露，不在本轮静默跳过。

## 验证记录

- B0：两项行为回归失败，分别暴露检索异常穿透和 doctor 漏报；权威历史事实失败传播回归用于锁定不应容错的边界。
- 候选定向：`tests/test_learning_search_corruption_isolation.py`，`3 passed in 0.82s`（含权威历史事实边界）。
- 扩大邻域：新增回归 + `tests/test_learning_outbox_corruption_isolation.py` + `tests/test_learning_store.py`，`102 passed in 26.23s`。
- 全量 `scripts/check.ps1`：退出码 0；后端 `1733 passed, 1 warning in 444.38s`；记忆质量 `7/7` 场景、`8/8` 查询、`memory_recall_at_k=1.0`、stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；workflow 角色 `6/6`；前端生产构建成功，UI 脚本 `66 passed`；`cargo check --locked` 与编译检查通过。既有 Starlette/httpx 弃用警告和 Vite chunk 体积提示仍在。
- 全量日志：`build/quality-gates/learning-search-corruption-isolation-final-full-check-20261010.log`。
- 当前关键文件 SHA-256：`core/learning_store.py`=`1216764d043d9673ee4063fbdf8a7434a4b5525cff1e7351ae6b5092220d2abf`；`tests/test_learning_search_corruption_isolation.py`=`a060d95b2cd2ed377efdb8d7809aab928d58946a2b6ce235531fea7efdfdf31d`；`tests/test_learning_outbox_corruption_isolation.py`=`bc486eaa2a2cfb3c259c7de52a4259d24a2490618dc8acfb9443bf5958649a74`。

## 评测与冻结边界

本轮没有读取或修改待审核种子的 gold、训练数据、提示词或调参配置，没有下载/替换来源。动态复核 `eval/fixtures/business_seed_review_v0.1/manifest.json` 声明的 7 个来源哈希应为 `frozen_sources_match=True`。

本轮只证明同进程本地 SQLite、构造的 AES-GCM 派生 chunk 损坏、健康候选继续检索、权威历史事实失败显式传播和 opaque doctor 诊断；不证明真实 Redis/PostgreSQL、跨进程/跨主机 exactly-once、自动重建、索引全量一致性、长期性能、检索召回率、引用正确性或模型答案质量。跳过坏派生候选也不等价于回答仍完整；调用方应结合 doctor/索引状态处理降级。
