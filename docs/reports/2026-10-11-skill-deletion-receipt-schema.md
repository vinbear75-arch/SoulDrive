# 2026-10-11 Skill 删除回执 schema/version 边界

日期：2026-10-11；第三十五轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围

本轮审计 Skill 删除的分阶段 durable receipt。该回执已有 `in_progress/source_removed/completed/failed` 和租约恢复，但完成响应 envelope 仍只有加密 JSON，没有 schema/version/响应形状校验；可解密的未知协议回执可能被当作完成结果回放。

本轮没有修改 Skill 删除阶段、项目范围、源路径安全规则、冻结集、gold、训练/调参配置、索引、模型或外部后端。

## 外部机制对照

- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 的状态重放边界提示阶段状态和完成响应需要可识别的协议版本。
- [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery) 强调失败后从持久化阶段恢复，而不是把不明状态当作完成；本地继续保留 `source_removed` 的阶段恢复。
- [Restate HTTP invocation idempotency](https://docs.restate.dev/services/invocation/http) 的同键回放要求完成响应稳定且可解释。

## B0 失败优先

新增测试前按旧实现运行，完成回执没有 `schema_version`，测试在 `KeyError: 'schema_version'` 失败（`1 failed, 103 deselected`）。旧实现会忽略随后注入的未知版本，而不是拒绝回放。

## 最小实现

- 新完成回执 envelope 固定为 `skill_deletion_receipt_v1`。
- 缺失版本的 legacy 完成回执在响应是对象时继续兼容，避免无必要地阻断历史本地回执。
- 未知版本、非对象 envelope、缺少对象型 `response` 均 fail-closed；不会把删除结果伪造为成功，也不会重做源目录清理。
- 既有阶段租约和 `source_removed` 接管逻辑保持不变；只在读取完成响应时增加协议边界。

## 验证证据

- Skill receipt schema、legacy 回放、未知版本拒绝与迁移组合：定向 `4 passed`。
- 学习存储/API 组合回归：`252 passed, 1 warning`。
- 完整 `scripts/check.ps1`：退出码 `0`；后端 `1762 passed, 1 warning，439.94 秒`；记忆质量 `7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口角色 `6/6`；前端生产构建成功；UI 脚本 `66 passed`；锁定 Cargo 检查通过。
- 当前变更文件 SHA-256：
  - `core/learning_store.py` = `BB4014D2D929484652F9719645E18B14BDE048F47DA5CC6FEFA16E5A56EFD59B`
  - `tests/test_learning_store.py` = `60B2F1224F436107A8AA76CC255862D9CE0DB340691F9B8581D8C4E9E093378B`

## 边界与下一轮

本轮证明的是本机单用户 SQLite/AES-GCM Skill 完成回执的协议版本、响应形状和 legacy 兼容边界；不证明文件系统与 SQLite 原子性、断电恢复、跨进程/跨主机 receipt、真实 Redis/PostgreSQL、外部 exactly-once、历史异常回执迁移、下游副作用完成或真实 Tauri/WPS UI/E2E。

既有 Starlette/httpx 弃用警告和 Vite 单 chunk 体积警告仍为非阻断项。下一轮继续检查会话请求回执和 PPT/任务阶段 marker 的协议版本、旧状态与完成响应一致性。
