# 2026-10-11 记忆变更回执 schema/version 边界

日期：2026-10-11；第三十四轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围

本轮审计 `memory_mutation_receipts` 的加密 envelope。AES-GCM 能证明密文完整，但不能单独证明解密后的 JSON 仍是本项目当前回执协议；旧实现没有 schema/version 检查，错误 envelope 可能被当成正常幂等重放。

本轮没有修改 SQLite 表结构、冻结集、gold、训练/调参配置、索引、模型或外部后端。

## 外部机制对照

- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 的运行/产物 lineage 提示消费结果必须能回指可识别的版本化对象。
- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 的 replay 语义要求状态输入与当前协议一致；本地只借鉴回放前的协议检查，不宣称具备跨主机 workflow replay。
- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 provenance/实体边界对应本地回执需要保留可解释的结构化响应，而不是只凭密文可解读就信任。

## B0 失败优先

新增测试前临时按旧实现运行，旧回执没有 `schema_version`，测试以 `KeyError: 'schema_version'` 失败（`1 failed, 102 deselected`）。这确认旧路径没有写入或检查回执 envelope 版本。

## 最小实现

- 新写入的加密响应 envelope 固定为 `memory_mutation_receipt_v1`。
- 同一 store 继续兼容缺失 `schema_version` 的 legacy envelope，只要其中 `memory` 是对象；不做静默迁移或覆盖历史回执。
- 未知版本、非对象 envelope、缺少对象型 `memory` 响应均 fail-closed，避免以 `memory=null` 或不完整响应伪造成功回放。
- SQLite 表结构与既有输入 digest/幂等键绑定保持不变；真正的 mutation 不因回执校验而重复执行。

## 验证证据

- 版本回执、legacy 回放与未知版本拒绝定向测试：`3 passed`。
- 学习存储/API 组合回归：`251 passed, 1 warning`。
- 完整 `scripts/check.ps1`：退出码 `0`；后端 `1761 passed, 1 warning，443.37 秒`；记忆质量 `7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口角色 `6/6`；前端生产构建成功；UI 脚本 `66 passed`；锁定 Cargo 检查通过。
- 当前变更文件 SHA-256：
  - `core/learning_store.py` = `A4DAAE93F99DAA3885151FACC5C1FC9E8D45ADB86400EF28770447DAD7F48352`
  - `tests/test_learning_store.py` = `8E0A9A1F245CC508EEC4B165A27B4F4B8A91CD7105FDAD38EF77EE266C17F57C`

## 边界与下一轮

本轮证明的是本机单用户 SQLite/AES-GCM 回执在协议版本和响应形状上的 fail-closed 边界，以及缺失版本 legacy 的兼容路径；不证明回执跨进程/跨主机同步、断电原子性、真实 Redis/PostgreSQL、外部 exactly-once、下游副作用完成、历史异常回执自动迁移、开放式记忆答案质量或真实 Tauri/WPS UI/E2E。

既有 Starlette/httpx 弃用警告和 Vite 单 chunk 体积警告仍为非阻断项。下一轮继续审计 Skill/会话/PPT 等其他 durable receipt 是否有同类协议版本或恢复阶段边界。
