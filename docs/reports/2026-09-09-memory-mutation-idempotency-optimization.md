# SoulDrive 记忆变更幂等键与耐久回执优化

日期：2026-09-09

## 外部实践对照

- [Restate HTTP invocation 文档](https://docs.restate.dev/services/invocation/http)定义了 `Idempotency-Key`：完成后的响应在保留期内持久化，同一键重试直接返回相同结果，不重新执行服务处理。
- [Restate 可靠重试说明](https://docs.restate.dev/services/invocation/http)同时区分可重试的入口/网络错误和业务 `4xx`，说明幂等键必须绑定请求输入，不能把任意失败都当成可安全重放。
- [Open WebUI memory router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py)把记忆事实写入与向量 upsert 分成两个阶段，并在写入前验证操作；这提醒我们必须把“主记忆事实”和“派生检索/归档同步”分别记录，不能把一次 HTTP 返回当成所有层都已原子提交。
- [Mem0 memory core](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)和[Add Memory 文档](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx)把 `user_id`、`agent_id`、`run_id` 等标识作为记忆作用域，并要求后续搜索继续带着一致的范围；这对应 SoulDrive 的 `project_id`，不能只在主库校验而在重放投影时丢失。
- [Debezium Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)把业务事务中的 outbox 记录作为后续事件投递边界，并要求事件 ID 可用于去重；这为本机 SQLite 版本提供了一个更小的迁移方向，而不需要先引入消息代理。
- [Restate Error Handling](https://docs.restate.dev/guides/error-handling)将可重试基础设施错误与终止性业务错误区分，并以指数退避、最大间隔和最大尝试次数控制恢复压力；本地实现只借鉴退避与可观测 pending，不引入 Restate 运行时。

这些项目没有授权 SoulDrive 引入 Restate 或 Open WebUI 运行时。本轮只迁移两个原则：幂等键要绑定规范化输入，同键重试不得重复执行；本机主事实库先形成可恢复的耐久边界，再明确跨存储同步仍不是分布式 exactly-once。

## 发现的本地缺口

项目已经有：

- 聊天 `request_id` 回执租约；
- 运行时自动记忆的 `request_id` 回执；
- 审核决定按 review 与 decision digest 重放；
- 记忆编辑的 `expected_updated_at` 乐观并发校验。

但画像管理器的编辑、停用和删除 API 还没有客户端 `Idempotency-Key`。网络超时后用户重试编辑时，第二次请求只会因为旧的 `expected_updated_at` 返回冲突；撤销/删除则可能在第一次已经完成后返回“找不到”，客户端无法区分“未执行”和“已完成但响应丢失”。

## 最小修复

### 1. LearningStore 耐久回执

新增本地 SQLite 表 `memory_mutation_receipts`，保存：

- 幂等键、操作类型、记忆 ID；
- 规范化输入的 HMAC 摘要；
- 加密的主库变更结果。

带幂等键的 update/revoke/delete 使用 `BEGIN IMMEDIATE`，将“检查同键 -> 主记忆变更 -> 加密回执写入”放在同一个 SQLite 事务中：

- 首次请求完成一次主库变更并保存结果；
- 同键且输入完全一致时返回已保存的记忆结果，不增加版本、不重复删除/归档；
- 同键但输入不同返回冲突，阻止键复用；
- 进程在事务提交前退出时，SQLite 同时回滚主库变更和回执，后续重试仍可执行。

### 2. API 与 UI

- `/learning/memories/{id}`、`/revoke` 和 `DELETE` 接受 `Idempotency-Key` 请求头。
- API 重放主库结果时不重复执行主记忆变更，但会重试幂等的归档/向量同步，并显式返回 `idempotent_replay=true`；首次带键请求返回 `false`。这样第一次同步返回 `degraded` 时，同键重试仍有机会修复派生层。
- UI 的编辑、停用、删除和运行时自动记忆撤销请求均发送随机幂等键；服务端仍以键绑定的请求摘要为准，不信任客户端自行声明的结果。
- UI 现在读取 `storage_sync.status`：主记忆已经提交但派生归档/检索返回 `degraded` 时，显示“待恢复”而不是误报“检索索引已同步”；提示同时告知后续记忆管理器/问答入口会自动重试。

### 3. 本地派生同步 outbox

- `learning_sync_outbox` 与主记忆表位于同一个加密 SQLite 工作区；审核接受、update/revoke/delete 在主事实变更提交前写入加密事件，活动记忆使用同一条可重放的 upsert 路径，事件 ID 由操作、记忆 ID 和记录版本组成，重复写入不会生成第二条同义事件。
- 正常请求在归档/向量同步成功后标记事件完成；如果同步返回 `degraded`，事件保留为 `pending`。工作区首次加载、重新打开记忆管理器或进入问答上下文时，均可先用 SQLite `BEGIN IMMEDIATE` 领取短租约再重放事件；因此“主库已提交、进程在派生同步前退出”的窗口有一个可恢复边界，同时同一事件不会被两个并发入口同时领取。首次失败允许一次快速恢复，第二次及以后写入 `next_attempt_at` 做指数退避（1、2、4…秒，上限 5 分钟），避免永久性下游故障在每次请求中热循环。
- 事件载荷不写明文标题、内容或来源；工作区重开后通过同一密钥解密，失败尝试保留次数和最近错误，便于后续可观测性接入。

## 失败优先与验收证据

- 先新增 `test_memory_update_replays_a_durable_idempotency_key`；修复前因 `LearningStore.update_memory` 不接受 `idempotency_key` 而失败。
- `tests/test_learning_store.py`：`46 passed`，覆盖跨连接重放、版本不重复增加、同键换输入冲突、撤销/删除重放、outbox 跨连接恢复、claim/lease 互斥重试、连续失败指数退避，以及完成/失败操作的 claim fencing。
- `tests/test_mcp_server_learning.py`：`52 passed, 1 warning`，覆盖 API 编辑重放、同键冲突、撤销重放、记忆管理器重新打开时重放 pending 事件、审核接受同步退化后的恢复、问答上下文加载前的自动重放、工作区加载时的一次启动扫描、项目范围撤销回放，以及完成标记写入失败后的可重放恢复。
- 其中故障注入覆盖“第一次派生同步 `degraded`、第二次同键重试 `ok`”，并确认主记忆版本仍保持为 2、归档撤销被安全重放两次。
- `souldrive-ui/scripts/learning-review.test.mjs`：`3 passed`，确认画像管理 UI 发送幂等头。
- Python `compileall`：通过。
- 先执行定向回归：`97 passed, 1 warning`（`tests/test_learning_store.py tests/test_mcp_server_learning.py`），Python `compileall` 通过。
- 随后执行仓库级 `scripts/check.ps1`：后端 `1043 passed, 1 warning`；前端生产构建通过，7 组测试共 `44 passed`；Tauri `cargo check --locked` 通过。

## 明确边界

- 本轮保证的是本地学习 SQLite 主事实、幂等回执和派生同步 outbox 事件的原子性，不是跨 SQLite、向量索引、会话归档和审计文件的分布式 exactly-once。
- outbox 当前由工作区首次加载、记忆管理器 GET 或问答上下文触发重放，没有常驻后台 worker；租约过期后可被下一次访问重新领取。首次失败保留一次快速重试，连续失败从第二次起指数退避并公开 `next_attempt_at`；已用故障注入证明“下游返回成功但完成标记写入失败”时事件会保持 pending 并可再次重放，但这仍不是跨存储 exactly-once：下游操作必须依靠自身幂等性承受重复投递。
- 本轮覆盖审核接受、记忆 update/revoke/delete 和运行时自动记忆共用的活动记忆同步路径；Skill 资产仍沿用自己的同步路径，尚未被统一成一个泛化事件总线。
- 幂等键没有替代 `expected_updated_at`：前者处理同一次请求的重试，后者处理两个不同编辑者之间的真正并发冲突。

## 2026-09-09 后续故障闭环

- 修复 `_replay_learning_memory_outbox` 的撤销/删除分支：回放 `memory.project_id`，避免项目记忆在归档/向量同步时退化成无范围写入。
- 新增 `test_memory_sync_outbox_replay_preserves_project_scope_for_revoke`：先失败后通过的回归证明项目范围确实传入下游。
- 新增 `test_memory_sync_outbox_retries_when_completion_marker_write_fails`：模拟下游同步已经成功、完成标记抛错，确认租约释放、错误保留、事件仍 pending，并在下一次访问完成。
- 新增 `test_memory_sync_outbox_applies_retry_backoff_after_failure`：确认第一次失败允许快速恢复，第二次失败后立即领取被抑制，达到 `next_attempt_at` 后再恢复，且第二次退避长于第一次。
- 新增 `test_memory_sync_outbox_completion_requires_the_active_claim`：确认无 claim token 的普通路径不能覆盖或释放另一个工作者已领取的事件，持有租约的工作者仍可完成。
- 修复直接 API 的幂等撤销/删除补偿：首次请求和同键重放调用归档/向量投影时都显式传递 `memory.project_id`，避免下游 PostgreSQL/归档适配器在重放路径丢失项目范围；新增 `test_memory_update_idempotency_key_replays_without_second_write` 断言撤销两次均带范围，并新增删除重放范围回归。
- 这轮先在旧代码上复现为两次下游调用都收到 `{}`，修复后记忆模块组合回归为 `98 passed, 1 warning`；仓库全量随后为后端 `1043 passed, 1 warning`，前端 `44 passed`，Tauri 检查通过。
