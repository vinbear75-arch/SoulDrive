# 加密语义向量归档与模型匹配恢复优化

日期：2026-09-10

## 结论

本轮补齐了可选 Redis 语义记忆投影在进程重启、Redis 短暂不可用和模型切换后的恢复边界。

此前 Redis 只保存派生向量，SQLite/加密记忆归档只保存文本事实。Redis 重启或投影丢失后，事实记忆仍在，但没有一个不调用模型的恢复路径；如果模型已经切换，也没有办法安全判断旧向量能否继续使用。

现在的行为是：

- 只有已经生成的向量才作为加密派生字段写入记忆归档，保留 `semantic_embedding_model`；不在恢复阶段重新调用 embedding 模型；
- 进程启动时执行固定上限的 `rehydrate_memory_vectors(limit=100)`，只恢复活动记忆/技能，且要求归档模型 ID 与当前 Ollama provider/model 完全一致；超过单批上限时通过现有加密 SQLite 保存稳定 memory-id 游标，后续启动/健康检查继续推进；
- 模型不匹配、缺少模型标识、非活动记录或非法向量只计入 `skipped`，不会把旧模型向量伪装成当前语义检索结果；
- Redis 初始不可用时，文件事实源仍可用；后续健康检查发现 Redis 恢复，会再次执行一次有界恢复；
- 恢复页按稳定 `memory_id` 排序，返回 `cursor/has_more/cycle_completed`；embedding 模型变化会重置游标，从新向量空间完整遍历一轮；
- 恢复失败会进入 `memory_vector_rehydrate` 的 `degraded/error` 状态并使存储健康结果降级；旧 outbox 错误不会跳过这条独立恢复路径；
- 当前 Redis 仍是可重建派生投影，事实源仍为本地加密归档/SQLite；没有增加向量数据库、后台队列或模型调用依赖。

## 外部实践对照

- [Qdrant Data Synchronization](https://qdrant.tech/documentation/data-synchronization/) 将向量索引视为需要与源数据同步的派生系统，给出事件驱动、批量重建和双写后 reconciliation 等路径；本地单机实现选取最小的“事实归档 + 有界重放”。
- [Qdrant embedding model migration](https://qdrant.tech/documentation/tutorials-operations/embedding-model-migration/) 强调迁移应分批执行、保留持久进度，并避免旧批次覆盖更新版本；SoulDrive 当前不做自动模型迁移，而是严格 model ID 匹配，避免静默混用不同向量空间。
- [Qdrant synchronization with Postgres](https://qdrant.tech/documentation/data-synchronization/with-postgres/) 展示了数据库事实源与向量投影分离、按版本或时间继续同步的做法；本地实现沿用既有 `record_version`/加密 outbox fencing，不引入 Postgres/Qdrant 才能运行的前提。
- [Microsoft GraphRAG incremental update configuration](https://microsoft.github.io/graphrag/config/yaml/) 将增量更新输出与原始输出分离，并保留可继续运行的索引状态；[GraphRAG input schema](https://microsoft.github.io/graphrag//index/inputs/) 使用基于文本哈希的稳定文档 ID。SoulDrive 采用同一类“稳定身份 + 持久进度”原则，但把 checkpoint 放进现有加密 SQLite，不新增存储服务。
- [Chroma 持久化问题 7463](https://github.com/chroma-core/chroma/issues/7463)、[问题 6975](https://github.com/chroma-core/chroma/issues/6975)、[问题 7238](https://github.com/chroma-core/chroma/issues/7238) 反映了持久化目录、索引状态和模型/维度变更不能仅凭“文件存在”判断可恢复；本轮因此把恢复状态、扫描/跳过/失败计数和模型匹配写入 health，而不是假定投影天然可靠。

## 代码变化

### 1. 加密归档保留已生成向量

`ProductionSessionStore.persist_memory` 在调用方明确传入 `vector` 时，将向量和 `semantic_embedding_model` 写入现有加密记忆文件。`core/mcp_server.py` 的自动记忆路径传入稳定的 `ollama:<model>` 标识；无向量时不写入伪造的模型标识。

这不是把向量提升为事实源：正文、范围、状态、有效期和版本仍由原有记忆记录决定，Redis 只负责可选语义投影。

### 2. 有界、模型匹配、可继续的恢复

新增 `ProductionSessionStore.rehydrate_memory_vectors`：

- 每次最多扫描 100 条归档记录，并将调用上限限制在 1–1000；归档按稳定 `memory_id` 分页，不再永远只看最近文件；
- checkpoint 记录当前 embedding model、cursor 和 `has_more`，保存在现有加密 SQLite；重启后从上次已完成页继续，单页失败不推进游标；
- 只接受 `status=active`、非空列表向量和精确匹配的 `semantic_embedding_model`；
- 恢复时复用现有 scope/project/status/validity/record_version 过滤字段；
- 结果写入 `memory_vector_rehydrate_v1`，包含 `scanned`、`attempted`、`restored`、`skipped`、`failed`、`cursor`、`has_more`、`cycle_completed` 及必要的截断错误信息。

Redis 查询同时要求当前模型 ID匹配，旧投影或旧 RediSearch schema 缺少模型字段时安全拒绝，不把模型空间不一致的距离送入语义合并。

### 3. 恢复与健康边界

- 初始化把 outbox 重放与向量恢复拆成两个独立的错误边界；旧密钥/旧格式 outbox 只记录 outbox 降级，不再阻断向量恢复；
- `health()` 在 Redis 后续恢复且上次状态为 `unavailable/error/not_attempted` 时触发有界重放；
- `degraded` 现在包含向量恢复的 `degraded/error`，使“事实源可用但语义派生投影未完全恢复”可被诊断。

## 失败优先与验证

本轮先加入回归再修改实现。新增覆盖：

- 匹配当前模型的加密向量可恢复到 Redis；旧模型向量被跳过；
- 旧 outbox 错误不会跳过向量恢复；
- 向量恢复失败会使存储健康降级；
- Redis 后续恢复会触发向量重放。

验证结果：

- 记忆/会话/MCP/GraphRAG 组合：`135 passed, 1 warning`；
- 相关向量/TTL/旧投影/恢复定向：`6 passed`；
- `scripts/check.ps1`：后端 `1118 passed, 1 warning`；Python compileall 通过；前端生产构建通过；前端 7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过；
- `git diff --check` 无空白错误；仅报告 Git 对现有工作区 LF/CRLF 的提示。

## 未宣称的能力与后续边界

- 这不是 embedding 模型迁移：模型变更后旧向量被跳过，需要后续显式重嵌入流程；
- 每批最多 100 条是小项目的启动预算；超过上限的归档会在后续健康检查/重新打开工作区时继续推进，但当前没有独立后台 reconciliation worker；
- 当前没有冻结业务评测集，因此不能声称回答准确率、记忆召回率或语义质量提升；
- 没有改变 `ConversationStore` 的事实源，也没有把 Redis/PostgreSQL 变成跨存储 exactly-once；
- 真实 Redis 重启、真实 Ollama embedding、Tauri 窗口和打包安装仍需独立环境门验证。
