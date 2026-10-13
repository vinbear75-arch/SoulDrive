# SoulDrive Agentic RAG 与 Durable Execution 审计

日期：2026-09-09；最后更新：2026-09-13

本报告承接本轮“外部优质开源项目对照 -> 本地链路审计 -> 最小实现 -> 回归验证”。外部项目的做法只作为可迁移的设计证据，不代表 SoulDrive 引入对应框架或依赖；现有本机单用户、项目边界、证据门和人工审核门保持不变。

## 结论先行

截至 2026-09-13 本轮最新全量为后端 `1205 passed, 1 warning`；本轮新增的 GraphRAG 范围契约专项为 `30 passed`，普通 RAG/GraphRAG/安全范围组合为 `143 passed`，诊断/评测/时间语义定向回归为 `133/63/18 passed`，会话/MCP 组合为 `112 passed`，GraphRAG/图存储/索引器范围专项为 `59 passed`，GraphRAG/项目诊断新增组合为 `89 passed`，前端生产构建与 7 组测试共 `49 passed`，Tauri `cargo check --locked` 通过。此前的研究窗口/RAG、TaskJournal/PPT、项目 API/目录、关键词索引/加密向量库/检索、父块扩展/知识库/GraphRAG、会话/学习存储/幂等与 durable execution 专项仍按各自报告保留；以下历史条目仍按各自引入时的证据保留。

SoulDrive 当前不是“只有一次向量召回”的 RAG：已经包含项目优先/全局回退、关键词与向量混合召回、查询扩展、查询改写、知识图谱路由、重排、父块/结构上下文扩展、证据门、一次有界的纠错检索和引用校验重试。这是一条受约束的 agentic retrieval 链路，而不是自由循环的多智能体系统。

本轮没有直接引入新的 Agentic RAG 框架。原因是当前缺少同一数据、同一模型、同一延迟预算下，证明自由工具循环优于现有有界链路的留出集证据；贸然增加循环会扩大延迟、显存、提示注入和项目越界的审计面。后续只有在 held-out 评测证明增益时，才考虑增加“搜索/精确查看/证据复核”的有限工具循环。

本轮优先补了两个能直接减少真实重试损耗的缺口：任务级幂等和已完成聊天结果复用。

## 外部实践与本地映射

### 1. Durable execution 与幂等

[LangGraph 的 Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)要求把非确定性调用和副作用放进可持久化的 task，并在重复执行时使用幂等键或先检查已有结果；[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)则以 thread/checkpoint 保存可恢复状态。[Inngest 的执行模型](https://www.inngest.com/docs/learn/how-functions-are-executed)把步骤结果单独持久化，重放时跳过已完成步骤，并要求外部调用放在 step 中。[Temporal](https://docs.temporal.io/)将同类目标概括为崩溃后仍可继续的 durable workflow。

SoulDrive 原有 `TaskJournal` 已经具备加密输入、步骤状态、步骤输出、失败重试、进程退出恢复和完成步骤跳过。本轮增补：

- `runtime_tasks` 新增任务级 `idempotency_key` 与输入摘要，并建立 `(kind, idempotency_key)` 唯一约束。
- 同一任务类型和幂等键、输入与步骤相同，直接返回既有任务；同键但输入或步骤不同，明确拒绝，避免静默复用错误任务。
- PPT 计划和确认后的生成任务使用稳定幂等键；重复请求不会再创建另一条同逻辑任务。
- 审核接受和记忆 update/revoke/delete 在本地事实事务内写入加密 `learning_sync_outbox`；同步失败的 pending 事件可在重新打开记忆管理器时通过 SQLite 短租约重放，避免把一次 HTTP 返回误当成跨存储提交完成。

这仍然不是“所有外部副作用自动 exactly-once”。如果进程在外部写入完成、`complete_step` 尚未提交之间崩溃，步骤恢复可能再次调用外部系统。因此每个不可逆副作用仍需使用业务侧幂等键、结果查询或人工确认；当前 PPT 产物目录和项目产物表已有自己的幂等约束，真实邮件/日历写入仍保持人工授权边界。

### 2. 文件知识库的异步状态与可诊断性

[Open WebUI 的 Knowledge 文档](https://docs.openwebui.com/features/workspace/knowledge/)把知识调用拆成搜索、精确查询和查看，并强调服务端状态、进度、失败和重试信息；其[API 文档](https://docs.openwebui.com/reference/api-endpoints/)要求在文件处理完成前等待处理状态，避免直接把未完成文件加入检索。

SoulDrive 本轮已把索引器的逐文档阶段投影为稳定的 `indexed`、`processing`、`pending`、`failed` 状态，并公开受限的失败阶段、错误分类和观测信息。该变化解决的是“索引失败后用户不知道下一步”的可运维问题，不是检索质量提升。

### 3. 路由、工作流和 RAG 框架

[Haystack 的 ConditionalRouter](https://docs.haystack.deepset.ai/docs/conditionalrouter)和[Pipeline](https://docs.haystack.deepset.ai/docs/pipelines)展示了按条件选择路径、让组件保持可观测的做法；[LlamaIndex Workflows](https://llamaindex.openml.io/python/framework/getting_started/concepts/)和[Agent](https://llamaindex.openml.io/python/framework/understanding/agent/)把 RAG 工具纳入可编排工作流。

SoulDrive 已有 `plan_retrieval`、workflow/playbook、项目/资产/时间过滤和证据门，因此目前更适合继续收紧现有有界路由，而不是再叠加一层通用 Agent 框架。当前有效的“复杂度预算”是一次有限纠错检索和一次引用修复，不是模型自行决定的无限循环。

## 本轮代码改动

### 任务级幂等

- `core/task_journal.py`
  - `create_task` 接受可选 `idempotency_key`。
  - 持久化输入/步骤摘要，检测同键同输入复用和同键异输入冲突。
  - 通过 SQLite 唯一索引覆盖进程内外的并发创建竞争。
- `core/presentation_service.py`
  - 计划任务使用 `presentation.plan:{request_id}`。
  - 生成任务使用 `presentation.generate:{plan_task_id}:{plan_digest}`。

### 聊天请求重试复用

- `core/conversation_store.py`、`core/state_backends.py`
  - 新增按 `session_id + request_id` 查找消息的能力，可按角色和完成状态筛选。
- `core/mcp_server.py`
  - 同一 `request_id` 且原问题一致、已有 `assistant/completed` 结果时，直接流式返回归档答案，不重新加载 RAG/模型。
  - 同一 `request_id` 换问题时返回 `409`，避免幂等键被错误复用。
  - 已完成结果的重放优先于索引忙状态，但仍受工作区解锁门保护。

### 会话角色不可临时漂移

- `/chat` 现在同时校验已绑定专用 workflow 的会话和通用会话：前者拒绝请求级角色冲突，后者拒绝临时注入 `paper_review`、`experiment_compare` 或其他专用 workflow；调用方必须新建或选择对应角色会话。
- 这使不同研究窗口的差异成为服务端持久化契约，而不只是 UI 选项或提示词差异；同一 `session_id` 不会在不同请求间改变业务语义。

### 明确保留的边界

- 同一 SQLite 工作区内，两个请求在第一条用户消息落盘前同时到达时，已由 `request_receipts` 首请求租约、续租和 epoch fencing 串行化；旧租约不能在接管后提交。跨主机分布式租约、时钟/网络分区和多数据库一致性不在当前本机边界内。
- 任务步骤的 `idempotency_key` 可用于业务副作用，但 `TaskJournal` 不会猜测外部系统的查询或回滚语义。
- 失败回答目前允许后续重试；只有 `completed` 结果自动复用，避免把暂时失败永久缓存为成功答案。
- 没有新增云端队列、工作流服务、向量数据库或多智能体依赖。

## 验证

- 初始定向回归：`tests/test_task_journal.py`、`tests/test_conversation_store.py`、`tests/test_mcp_server_learning.py`：`57 passed, 1 warning`。
- 新增覆盖：任务同键复用/异输入冲突、会话按 request_id 查找完成答案、聊天重试不触发 `ensure_runtime_loaded`、聊天同键换问题返回 `409`。
- 既有弃用警告来自环境中的 `starlette.testclient` 与 `httpx` 兼容提示，不是本轮失败。
- 后续定向回归又覆盖了项目/资产、会话角色、邮件幂等、同名附件隔离和重复预览拒绝：`104 passed, 1 warning`；RAG/GraphRAG/上下文门/重排/提示词：`198 passed`；PPT/实验台账/会话存储/交互上下文：`77 passed, 1 warning`。
- 当前最新全量 `scripts/check.ps1`：后端 `1153 passed, 1 warning`；Python compileall 通过；前端生产构建通过；前端 7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。此前报告中的 `1031`、`1036`、`1039`、`1040`、`1041`、`1042`、`1043`、`1044`、`1045`、`1046`、`1047`、`1049`、`1056`、`1060`、`1061`、`1062`、`1063`、`1064`、`1065`、`1066`、`1067`、`1068`、`1069`、`1071`、`1072`、`1073`、`1074`、`1075`、`1076`、`1077`、`1078`、`1079`、`1080`、`1081`、`1083`、`1085`、`1087`、`1090`、`1091`、`1094`、`1100`、`1101`、`1102`、`1103`、`1104`、`1105`、`1106`、`1107`、`1109`、`1110`、`1111`、`1112`、`1116`、`1117`、`1118`、`1119`、`1120`、`1121`、`1122`、`1123`、`1125`、`1126`、`1127`、`1128`、`1129`、`1130`、`1132`、`1137`、`1138`、`1139`、`1140`、`1141`、`1142`、`1143`、`1144`、`1145`、`1147`、`1148`、`1150`、`1151`、`1152` 都是本轮新增回归前的历史快照，不能混作同一次运行。

## 后续边界修补

- 邮件附件上传现在建立项目资产版本绑定；草稿按 `project_id` 解析来源，不接受另一项目的同名附件；发送优先读取草稿绑定的加密对象并校验 MD5，避免用全局文件名猜测附件。
- 预览遇到同名加密文档时改为 `409` 拒绝猜测；会议 Playbook 的预检新增独立 `text_note`，用户直接输入的会议/阅读记录不会再被标成语音转写。
- PPT 任务的详情、确认、恢复、取消和列表按持久化 `project_id` 做精确范围校验；前端轮询与任务操作均回传该范围，避免只凭全局 `task_id` 跨项目读取或变更任务。

## 下一轮候选与进入条件

按风险和收益排序：

1. **跨主机 request receipt / in-flight lease**：本机 SQLite 版本已完成；若未来支持跨主机并发，必须先确定时钟/网络分区、超时接管、失败重试和用户可见状态，再选择外部协调存储并写故障注入测试。
2. **本地 outbox 下游结果幂等**：已补当前工作区首次加载时的一次启动扫描、项目范围回放、完成标记故障注入和连续失败指数退避；GET/问答入口仍会重试挂起事件，首次失败保留快速恢复，重复失败从第二次起退避。不能把 claim/lease、退避或启动扫描当作跨存储 exactly-once，也没有建设常驻后台 worker。
3. **证据工具循环**：先建立 text-only、现有 bounded retrieval、有限“搜索/精确查看/复核”三组同条件评测，再决定是否引入 Agentic RAG。
4. **副作用适配器**：为邮件、日历、PPT/WPS 等分别定义幂等键、结果查询和人工确认，不使用一个泛化的“恰好一次”承诺。
5. **RAG 评测深化**：继续冻结数据版本、逐样本证据覆盖、串项目率、引用正确性和 P50/P95 延迟；仅有 `Recall@K` 或 `MRR@K` 不代表回答质量提升。

以上候选都不改变当前本机单用户边界，也不替代真实 Tauri/WPS UI 验收。

## 本轮后续 RAG/耐久执行补丁

- GraphRAG local 社区打分纳入 `relationships` 文本，并以关系词反例验证不会退化到无关社区。
- 检索评测持久化 `graph_route`、图阶段是否执行和图上下文是否非空，区分“来源命中”与“图路径真的贡献了上下文”。
- 图路由统一基于 contextual retrieval query；纠错检索完成后才发送检索完成回调，回调携带最终路径、模式和纠错标志。
- workspace load 首次尝试重放加密学习 outbox；失败事件保留 pending，后续问答/记忆管理入口继续尝试。新增启动扫描不改变下游跨存储 exactly-once 边界。
- 记忆管理 UI 读取 `storage_sync.status`；主事实已提交但派生同步退化时显示“待恢复”，不再把 API 的主库成功误报为检索索引已同步。
- Skill `plan_only` 计划记录现在用工作区内 `Idempotency-Key` 绑定输入摘要；重试返回原运行记录，同键异输入冲突，并发测试确认不会产生重复计划。
- 多模态工作流目录/计划和运行时推理/上传推理在进入 Playbook、感知事件或模型调用前校验项目存在性；该门禁与文档索引/证据授权门分开，不把项目存在误报成证据可用。
- `/chat` 在幂等回放与 RAG 之前拒绝跨项目感知事件/候选目标；项目范围回归覆盖事件本身和候选目标两条路径。
- Chat request receipt 绑定有效会话上下文摘要；同一 `request_id` 的相同显式选区可重放，换选区返回 `409`，旧 query-only 进行中收据继续保留租约串行化兼容。
- `/chat` 现在同时接受 JSON `request_id` 和标准 HTTP `Idempotency-Key`；只给 header 时进入同一 request receipt/已完成答案重放链路，body/header 同时存在但不一致会在进入会话和 RAG 前返回 `409`。这只是协议兼容，不扩大本机 at-least-once 与外部副作用边界。
- 邮件草稿 API 与 UI 已接入 artifact 输入绑定的幂等键；相同草稿重放同一 artifact，改变内容时同键冲突，不扩张到真实邮件发送。
- 会话创建严格保持全局/项目范围不可迁移，并把显式 ID 的同输入重试标成 `replayed`；UI 对项目会话保留稳定身份，避免重试生成第二个研究窗口。
- 记忆读取门新增同 subtype/同标题事实冲突裁决：复用项目范围、人工审核、新鲜度和相关性权威顺序，排除项记录 `conflicting_memory_topic` 与胜出记忆 ID；这是有界启发式，不是通用实体消歧。
- 记忆审核接受路径新增受限 supersession：只有候选明确给出 `conflicts_with` 且旧事实与新事实完全同范围、同 subtype、同标题时才归档旧 memory/chunk，并把撤销事件写入已有本地 outbox；没有把 Mem0/Graphiti 的自动语义失效照搬进来。
- 不同 review 并发接受同一记忆时，提交事务内重新检查当前 active memory；精确重复复用已有结果并持久化 `deduplicated_memory_id`，重放不再因为候选没有自己的 memory 行而失败。
- 记忆读取 gate 和 `learning.injected_into_rag` 审计事件增加最终 `memory_ids`，只记录身份不复制正文；需要内容复核时仍通过现有项目范围和 source trace 读取链路完成。
- 记忆胶囊现在在不携带 `source_excerpt` 的前提下保留 `memory_id`、项目范围、有效期和来源消息索引；模型可区分历史/项目记忆，原始对话仍留在受控读取链路。
- `/chat` 不再把 Playbook 标题和输出契约拼进 RAG query；用户原始问题进入检索，`workflow_id` 作为结构化状态进入输出/证据门。论文精读与实验对比回归确认 query 与角色上下文分别保留，避免窗口控制文字污染召回。
- 比较/多来源查询的纠错检索现在不得用单来源高分结果替换首轮多来源上下文；重试审计记录 `source_diversity_preserved`，覆盖不足时继续走证据门而不是生成片面比较。
- GraphRAG 路由新增局部关系反例回归：当“整体/总体”等宽泛词与“影响链、路径、多跳、上下游、依赖”等明确实体关系信号同时出现时，优先走 local；“这些论文共同思路”等全局归纳仍走 global。新增 `22` 项 GraphRAG 定向回归，RAG/检索组合定向回归 `72 passed`，避免用全局社区摘要代替局部关系检索。
- 研究窗口的 `session_instruction` 现在作为服务端绑定的系统级会话约束进入回答提示词；用户原始问题仍单独进入 RAG query，不再让窗口说明污染召回。研究窗口/提示词定向回归 `110 passed, 1 warning`。
- 记忆管理列表保留显式项目下的活动记录（包括已过期和计划生效的可编辑记录），无项目请求仍只列全局记忆；服务端返回 `validity_status`，UI 显示“当前生效/计划生效/已过期”，运行时注入仍使用严格的当前/历史 `as_of` 过滤。学习模块组合回归 `111 passed, 1 warning`。
- 可选 Redis 语义记忆投影现在写入 `memory_vector_v2`、scope、project、active 状态和有效期；向量查询在项目/全局和 `as_of` 上做硬过滤，旧无范围向量安全拒绝。该补丁没有把尚无查询 embedding/质量评测的语义记忆接入普通 RAG；向量/会话定向回归 `24 passed`。
- 参照 [Mem0 的多信号记忆检索](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)、[LangGraph 的语义 Store](https://langchain-ai.github.io/langgraph/how-tos/persistence-functional/) 和 [Redis 的 KNN 距离返回](https://redis.io/docs/latest/develop/ai/search-and-query/query/vector-search/)，普通 `/chat` 现在在“Redis 已有 `memory_vector_v2` 投影 + 知识库 embedding 模型可用”时生成一次查询向量，将语义分数与 SQLite 关键词分数合并后再进入原有时间/敏感性/冲突/预算门；Redis Search 路径显式返回 `distance`，并在合并前归一化。新增“关键词未命中时语义记忆命中”和“语义后端失败仍保留关键词路径”回归，均通过。
- 该路径是可选增强，不是新的事实源或自由 Agent 循环：没有 Redis、没有向量投影或没有模型时仍回退 SQLite 关键词检索；当前没有自动回填历史记忆向量，也没有冻结业务集证明答案质量提升，因此仍不能把默认本机运行宣称为完整语义记忆检索。
- 评测链路新增位置敏感的 `context_precision`，与来源覆盖率、引用支撑和拒答指标分开；评测/schema/诊断定向 `55 passed`，该轮总检查为后端 `1083 passed, 1 warning`。这仍不是答案事实正确率或 LLM judge 结果。
- 邮件外部副作用新增 `uncertain` 状态：发送调用开始后的未分类异常不再伪装成可安全重试的 `failed`；同键重放被阻断并要求提供方核验，发送前本地准备失败仍可重试。邮件/API 专项 `35 passed`，该轮总检查为后端 `1085 passed, 1 warning`；这仍不是 SMTP exactly-once。
- 本地 ICS 导出使用确认草案的 `created_at` 作为稳定 `DTSTAMP`；同一 artifact 的重复导出路径和字节均稳定；长 UTF-8 文本按 RFC 5545 折叠、CRLF 不会注入物理行、`ATTENDEE;CN` 对保留分隔符使用 quoted-string。日历导出新增回归 `6 passed`，当前最新总检查为后端 `1111 passed, 1 warning`；这仍不是外部日历 exactly-once。
- 记忆耐久同步现在按 `record_version` 做派生写入的单调保护：旧归档/Redis/PostgreSQL/outbox 重放不会回退新记忆；后端 memory outbox 事件键包含版本、操作类型和状态。新增耐久定向回归 `4 passed`，当前最新总检查为后端 `1111 passed, 1 warning`；这仍不是跨存储 exactly-once。详见 `docs/reports/2026-09-10-memory-durable-version-ordering-optimization.md`。
- 参照 Graphiti 的“事实失效但历史保留”时间语义，`archive_memory` 现在在停用当前记忆时为最后有效版本写入带 `invalid_at` 的 `memory_versions` 快照，同时保留既有历史版本；当前问答不会读到已停用记忆，显式历史 `as_of` 仍可回溯。新增回归与组合回归均通过，详见 `docs/reports/2026-09-10-memory-revoke-history-optimization.md`。
- PPT `confirm_plan` 现在覆盖“生成任务已先持久化、项目产物确认尚未写入”这一跨存储恢复窗口：重试会复用已有生成任务并幂等修复草稿产物为 `confirmed`，已取消产物不会被恢复。PPT/TaskJournal 定向回归 `37 passed, 1 warning`；这是本地恢复补偿，不是跨存储原子提交或外部 exactly-once。
- 反思草稿生成现在绑定输入摘要和可选 `Idempotency-Key`：相同键与相同触发消息只返回原 review/candidate，不再次调用本地模型；同键异输入、body/header 不一致均在生成前拒绝。SQLite 部分唯一索引负责落库并发兜底，详见 `docs/reports/2026-09-10-reflection-draft-idempotency-optimization.md`。
- 参照 Letta `/doctor` 的重复/放置审计、LangGraph 的可检查持久化记忆分层和 Mem0 对重复写入的说明，`LearningStore.memory_health()` 增加只读 memory doctor：报告同范围精确重复活动事实、活动事实缺少检索块、孤立/范围不匹配块、历史版本孤儿/非法/重叠区间和待同步 outbox；`GET /learning/memories` 与偏好画像展示摘要。新增 health 回归 `5 passed`，不自动合并、删除或消费 outbox；详见 `docs/reports/2026-09-10-memory-health-doctor-optimization.md` 与 `docs/reports/2026-09-10-memory-version-overlap-doctor-optimization.md`。
- 参照 LangGraph 的短期 thread/checkpointer 与跨会话 Store 分层，修复 Redis 会话 TTL 复用于语义记忆投影的隐式耦合：向量投影默认不设过期并清理历史 TTL，可用独立 `SOULDRIVE_REDIS_MEMORY_VECTOR_TTL_SECONDS` 显式启用临时 TTL；health 暴露投影策略。新增失败优先回归与 `94 passed, 1 warning` 组合回归，详见 `docs/reports/2026-09-10-memory-vector-ttl-durability-optimization.md`。
- 参照 Qdrant 的事件/批量同步和 Mem0 的更新时重嵌入/清理语义，修复活动记忆更新但 embedding 缺失时旧 Redis 向量继续可检索的问题：无 fresh embedding 时发出带版本的 `redis.memory_vector_delete`，失败进入本地 outbox，关键词路径保留。新增失败优先回归与 `152 passed, 1 warning` 组合回归，详见 `docs/reports/2026-09-10-memory-stale-vector-cleanup-optimization.md`。
- 参照 Qdrant 的派生索引同步/embedding 迁移、Microsoft GraphRAG 的增量输出和 Chroma 持久化故障案例，将已生成向量作为加密派生字段保存并记录模型 ID；启动及 Redis 恢复后按稳定 memory-id 游标分批恢复，旧模型只跳过不混用，恢复状态进入 health。新增 5 个失败优先回归；记忆/会话/MCP 组合 `135 passed, 1 warning`，该阶段全量快照为后端 `1119 passed, 1 warning`，当前最新见本报告结论，详见 `docs/reports/2026-09-10-memory-vector-rehydrate-optimization.md`。
- 参照 Graphiti 的事实时间/失效处理和 Qdrant 的派生索引同步原则，GraphRAG 构建产物新增源图快照指纹；当前图快照变化、切片项目归属变化或读取失败时，检索拒绝陈旧社区索引并暴露 `graphrag_index_stale`/`graphrag_source_snapshot_unavailable`/`graphrag_scope_metadata_unavailable`。GraphRAG/图存储/项目诊断/RAG 提示词组合 `138 passed, 1 warning`，该阶段全量快照为后端 `1119 passed, 1 warning`，当前最新见本报告结论，详见 `docs/reports/2026-09-10-graphrag-scope-freshness-optimization.md`。

## 2026-09-10 Agentic RAG 终止决策与来源覆盖证据

- 对照 [LangGraph Agentic RAG](https://langchain-ai.github.io/langgraph/tutorials/rag/langgraph_agentic_rag/) 的“相关性评分 -> 改写/回答条件路由”和 [Microsoft GraphRAG Query Overview](https://microsoft.github.io/graphrag/query/overview/) 的 local/global 路径后，本轮没有引入自由循环 Agent，而是审计现有一次纠错检索的最终停止状态。
- 修复前，检索完成回调会把重试耗尽的拒答也报告为 `status=pass`；同时，单份高分文档若同时提到比较双方，可能因证据分数足够而绕过上下文门的来源多样性 `retry`。
- 现在回调使用 `agentic_retrieval_trace_v1`，显式返回 `agentic_decision`、`stop_reason`、`correction_attempted`、`retry_used`、`passes_used`、`max_passes`、最终 `context_gate` 与 `evidence_gate`；来源不足时在生成前拒绝。
- 新增两个失败优先回归，覆盖弱证据两轮耗尽和单来源高分比较；RAG/GraphRAG/上下文门/答案质量/安全检索定向回归 `178 passed`，该阶段全量快照为后端 `1119 passed, 1 warning`，当前最新见本报告结论；前端 `48 passed`，Tauri `cargo check --locked` 通过。
- 该修复提升的是链路语义准确性和可观测性，不证明答案质量提升，也不等于完成无限工具循环 Agentic RAG；真实模型、冻结业务答案集和 UI/E2E 仍需单独验收。

## 2026-09-10 派生同步与历史记忆时间副作用证据

- 参照 AWS/microservices.io 的 outbox 消费者幂等、Redis `WATCH/MULTI/EXEC` 的 CAS 语义和 Qdrant 的写入顺序约束，同一 `ProductionSessionStore` 实例的 outbox 重放现在串行化；删除后的摘要事件和旧摘要版本仍由既有存在性/版本门拒绝。详见 `docs/reports/2026-09-10-outbox-replay-serialization-optimization.md` 与 `docs/reports/2026-09-10-session-summary-ordering-optimization.md`。
- 参照 Graphiti 的 bi-temporal 事实时间与 Mem0 的搜索/更新分离，历史 `as_of` 记忆查询不再调用 `touch_memories` 写入当前 `last_used_at`；历史回顾不会改变今天的记忆新鲜度排序。详见 `docs/reports/2026-09-10-historical-memory-readonly-optimization.md`。
- 当前组合回归 `281 passed, 1 warning`，记忆/RAG/学习组合为 `246 passed, 1 warning`，回答质量/RAG/GraphRAG/上下文门/检索组合为 `225 passed`，会话/outbox 专项为 `40 passed`，学习存储/记忆运行时/记忆门专项为 `83 passed`，最新全量为后端 `1155 passed, 1 warning`；TaskJournal/PPT lease fencing 专项为 `38 passed, 1 warning`，memory outbox 过期 claim fencing 专项为 `3 passed`；这些结果仍不提供跨进程 exactly-once、答案准确率提升或真实 Tauri/WPS/UI 交付证明。记忆管理列表、待审核列表和 outbox 顺序的独立记录分别见 `docs/reports/2026-09-12-learning-store-list-deterministic-order-optimization.md`、`docs/reports/2026-09-12-learning-review-list-deterministic-order-optimization.md` 与 `docs/reports/2026-09-12-outbox-oldest-first-order-optimization.md`。

## 2026-09-12 结构化实验运行检索焦点

- 对照 Microsoft GraphRAG 的按查询类型路由和 LangGraph 的显式条件边，发现 `experiment_compare` 在只绑定可比基线/当前运行、没有 `document_names` 时仍跳过实验专属检索焦点。
- 修复后，只要结构化 `M#` 证据通过授权门，实验窗口也会生成指标/数据集/版本/验证实验变体；论文窗口和会议记录窗口的既有输入边界不变。研究窗口/RAG/交互专项 `145 passed, 1 warning`。
- 最新完整 `scripts/check.ps1` 为后端 `1156 passed, 1 warning`，前端构建及 7 组测试 `48 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-12-experiment-workflow-focus-routing-optimization.md`。
- 该修复只证明工作流路由与审计行为一致，不证明检索召回、答案事实正确率、因果分析质量或 Agentic RAG 收益提升。

## 2026-09-12 输出契约内容与技能列表确定性

- 参照 Haystack 的结构化校验、LlamaIndex 的 StructuredLLM 和 GraphRAG 的 response/context 分层，修复 `evaluate_workflow_output_contract()` 只看标题、不看章节内容的漏洞；必需章节为空时进入既有的一次有界重试，`待确认` 仍是显式缺证据内容。
- 参照 Graphiti 的时间/身份分离、Letta 的显式 memory ID 和 MemFS 的可重新发现对象，修复 `list_skills()` 同一 `updated_at` 下依赖数据库返回顺序的问题，改为 `updated_at DESC, skill_id ASC`。
- 空章节工作流专项为 `5 passed, 1 warning`，学习存储/记忆运行时/记忆门专项为 `84 passed`；最新全量为后端 `1159 passed, 1 warning`，前端 7 组测试 `48 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-12-workflow-output-content-contract-optimization.md` 与 `docs/reports/2026-09-12-skill-list-deterministic-order-optimization.md`。
- 这两项只收紧输出完整性和可重复排序，不等于事实正确率、技能选择质量、跨进程 exactly-once 或真实 UI 交付已证明。

## 2026-09-12 PPT 证据收集项目范围补强

- 公开实现对照显示，文件名只能用于候选解析，不能替代知识库/租户访问范围：Open WebUI 先求可访问文件集合，Qdrant 在检索前做 payload filter，GraphRAG 为文档维护稳定身份。
- 本地失败优先回归发现，无 `project_id` 的 PPT 兼容请求会按展示文件名收集不同项目的同名父文档；现在项目绑定元数据在没有明确项目范围时一律拒绝，真正全局材料仍可被项目显式引用。
- PPT/证据收集专项 `23 passed`，最新全量后端 `1165 passed, 1 warning`，前端 `48 passed`，Tauri `cargo check --locked` 通过。该修复是单机范围的 fail-closed 证据门，不是多用户 RBAC 或跨存储 exactly-once。

## 2026-09-12 文档预览范围与 PPT 证据门对齐

- 文档预览曾按展示文件名直接聚合父块；现在与 PPT 证据门共享“无项目只读全局、项目请求只读同项目+全局”的范围语义，前端项目空间显式传递 `activeProjectId`。
- 跨项目同名父块的失败优先预览回归通过；文档/项目/PPT 组合 `67 passed, 1 warning`，前端文档范围 `16 passed`，全量后端 `1166 passed, 1 warning`。
- 该修复是单机证据范围补强，不等于多用户 RBAC、跨存储 exactly-once 或真实 UI/E2E。

## 2026-09-12 PPT 来源大小写兼容

- PPT 证据收集对索引来源和请求文件名统一采用 Windows 兼容的 `casefold()` 匹配，并把记录回写为请求规范名；稳定 parent/asset/version 身份没有被文件名替代。
- `report.pdf`/`REPORT.PDF` 失败优先回归与 PPT/API 专项 `26 passed, 1 warning` 通过；最新全量后端 `1167 passed, 1 warning`，前端 `49 passed`，Tauri 通过。
- 该修复只解决展示名的确定性与兼容性，不等于完成 Agentic RAG 或证明答案质量提升。

## 2026-09-12 项目与联系人发现列表确定性

- 顶层项目目录与联系人候选发现也补上唯一 ID 末级排序，避免同时间/同名称时把一次查询顺序误当作 durable 状态或候选优先级。
- 项目/联系人组合回归 `41 passed, 1 warning`，最新全量后端 `1169 passed, 1 warning`，前端 `49 passed`，Tauri 通过。
- 该修复只改善可重放发现顺序，不扩大 Agentic RAG、联系人自动选择或跨存储 exactly-once 的承诺。

## 2026-09-12 TaskJournal 任务列表确定性

- 参照 Temporal Visibility 的 `run_id` 末级稳定键，修复 TaskJournal 任务列表只按 `updated_at` 排序导致同时间任务顺序依赖 SQLite 返回顺序的问题。
- 失败优先回归、TaskJournal/PPT 专项 `37 passed` 和全量 `1160 passed, 1 warning` 均已通过；详见 `docs/reports/2026-09-12-task-list-deterministic-order-optimization.md`。
- 这只是任务发现顺序的确定性修补，不改变 lease、恢复、步骤幂等或跨进程 exactly-once 的边界。

## 2026-09-12 项目总览确定性

- 项目总览的最近实验、项目任务和决策列表分别补上 `run_id`、`task_id`、`decision_id` 稳定末级排序，避免并列时间/标题时调试与研究窗口发现顺序漂移。
- 项目 API/目录专项 `39 passed, 1 warning`，全量 `1161 passed, 1 warning`；详见 `docs/reports/2026-09-12-project-overview-deterministic-order-optimization.md`。
- 这只是可重复发现顺序修补，不改变实验比较、任务执行或决策语义。

## 2026-09-12 关键词检索同分确定性

- SQLite FTS 关键词查询补上 `doc_id` 末级 tie-breaker；失败优先回归、检索专项 `54 passed` 和全量 `1163 passed, 1 warning` 已通过。
- 该修复只收紧候选顺序，不等于 BM25、向量召回、重排或答案准确率提升；详见 `docs/reports/2026-09-12-keyword-retrieval-deterministic-order-optimization.md`。

## 2026-09-12 父块扩展同分确定性

- 来源多样性下的父块扩展最终重排补上候选 `id` tie-breaker；专项 `90 passed`，全量 `1164 passed, 1 warning`。
- 该修复只保持来源证据 Top-K 的可重复顺序，不引入自由 Agentic RAG 循环，也不代表答案准确率提升；详见 `docs/reports/2026-09-12-parent-expansion-deterministic-order-optimization.md`。

## 2026-09-12 邮件 durable execution 边界

- SMTP 调用后的 `EmailDeliveryError` 现在进入 `uncertain`，同键参数摘要不一致拒绝重放；邮件/目录专项 `40 passed, 1 warning`，最新全量后端 `1171 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。
- 该修复只收紧外部副作用状态机和请求级幂等，不等于 SMTP exactly-once 或提供方状态查询；详见 `docs/reports/2026-09-12-email-delivery-boundary-and-request-digest-optimization.md`。

## 2026-09-12 GraphRAG 诊断范围复核

- 带范围指纹的 GraphRAG 索引在没有 metadata provider 时不再返回 `ready=true`；同工作区知识库已加载时才复用 chunk 范围提供器，未加载时诊断返回 `unverified` 而不主动加载模型。
- GraphRAG/项目诊断定向 `88 passed, 1 warning`，完整后端 `1204 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-12-graphrag-diagnostics-scope-verification-optimization.md`。

## 2026-09-13 GraphRAG 旧项目索引范围 fail-closed

- 对照 Graphiti 的显式 group 过滤和 Microsoft GraphRAG 的 Document/TextUnit provenance，复核旧项目级 payload 是否能证明 chunk 仍属于当前项目。
- 失败优先回归复现带 `project_id`/源快照指纹但缺少范围指纹的旧索引；现在检索、诊断和项目+全局组合均返回 `graphrag_scope_metadata_unverified`，项目错误不会被 `global_shared` 静默掩盖。
- GraphRAG 专项 `33 passed`，GraphRAG/项目诊断组合 `89 passed, 1 warning`，完整后端 `1205 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-graphrag-legacy-project-scope-fail-closed-optimization.md`。
- 该证据只证明诊断状态与运行时范围校验边界一致，不证明 GraphRAG 召回率、社区摘要质量、答案准确率或真实 UI/E2E。

## 2026-09-13 工作流记忆命名空间

- 参照 [Letta blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/)、[Mem0 v2 filters](https://docs.mem0.ai/platform/features/v2-memory-filters) 和 [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)，把现有 `paper_review`、`experiment_compare`、`meeting_actions` 从“会话标签”推进为可验证的记忆读取范围。
- `workflow_fact` 现在由源会话绑定 `workflow_id`；跨窗口读取和无窗口 context-only 读取 fail-closed，共享偏好/项目事实保持共享。Redis 语义投影只是派生候选，SQLite 仍执行权威范围门。
- 该实现没有扩大 Agentic RAG 循环，也没有把窗口隔离误写成答案正确率提升；仍需冻结三类窗口评测集，比较检索/证据/输出契约。
- 学习存储 `70 passed`，MCP/会话组合 `113 passed, 1 warning`，完整后端 `1210 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-workflow-memory-namespace-optimization.md`。

## 2026-09-13 工作流记忆 Doctor

- fail-closed 只有在“隔离原因可发现”时才适合长期运行；本地 `memory_health()` 现报告活动/历史 `workflow_fact` 的未绑定问题，沿用 scope 过滤与敏感正文不回显原则。
- 这不是自动迁移或 Agentic RAG 机制。要修复存量归属，仍需人工确认目标工作流，然后使用已有幂等写入/outbox 路径。
- LearningStore `71 passed`，完整后端 `1211 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-workflow-memory-doctor-optimization.md`。

## 2026-09-13 Durable workflow 身份投影

- PostgreSQL 是派生投影，不再丢失会话/记忆的 `workflow_id`；session-created、session-message outbox 和重放均以本地 manifest 为权威来源。记忆更新的时间戳在同一活动集合中严格递增，避免重试/连续编辑改变 tie 结果。
- 第一次全量检查复现后修复 memory tie 反例；最终后端 `1214 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。
- 本轮仍未引入 Temporal/Restate 服务，也未验证真实 PostgreSQL 迁移、跨进程 exactly-once 或真实 UI/E2E。详见 `docs/reports/2026-09-13-durable-workflow-identity-projection-optimization.md`。

## 2026-09-13 研究窗口评测契约

- 对照 [Ragas 指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)、[DeepEval Faithfulness](https://deepeval.com/docs/metrics-faithfulness) 和 [Langfuse 数据集实验](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/experiments/datasets.mdx)，把 `workflow_id` 纳入离线评测样本和结果身份；未知、缺失或错配窗口在评分前 fail-closed。
- 检索评测会复用窗口专属 focus variants，答案评测会传递结构化 workflow context，并分别报告来源命中、Context Precision、引用支撑和输出章节契约；这验证窗口行为可观测，不把窗口契约通过率写成答案事实准确率。
- 窗口评测专项 `50 passed`，评测/工作流/RAG 提示组合 `170 passed, 1 warning`，完整后端 `1219 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-workflow-evaluation-contract-optimization.md`。
- 当前正式企业评测集尚未添加未经确认的窗口标签，因此三类窗口的业务质量对比仍是待冻结数据后的下一道质量门；自由循环 Agentic RAG 仍未引入。

## 2026-09-13 评测数据版本指纹

- 对照 [OpenAI Evals 的版本命名](https://github.com/openai/evals/blob/main/docs/build-eval.md) 和 [Phoenix 数据集版本/稳定样本 ID](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-datasets/updating-datasets)，评测结果现在绑定评分字段的规范化 SHA-256 指纹；rubric、关键词、拒答/多文档/图谱标记、项目、窗口或文档身份变化会阻断旧结果重用。
- 无指纹的历史 JSONL 只兼容读取并显式标为 legacy_unbound；结果部分带指纹、多个版本混合或指纹不匹配均 fail-closed。该指纹只证明评测标签版本一致，不证明模型/索引/硬件/外部数据源一致。
- 评测资产 `52 passed`，评测/窗口/RAG 提示组合 `172 passed, 1 warning`，完整后端 `1221 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-evaluation-dataset-fingerprint-optimization.md`。

## 2026-09-13 评测运行清单

- 对照 [OpenAI Evals run-evals](https://github.com/openai/evals/blob/main/docs/run-evals.md) 的 run ID/本地记录和 [Phoenix 实验流程](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments)，普通 RAG 评测现在可用 `--save-run-manifest` 复用既有 `souldrive_run_manifest_v0.1`，记录数据集指纹、仓库状态、环境、模型、Top-K、检索开关和指标摘要。
- 旧无指纹结果仍能兼容读取，但清单标为 `legacy_unbound`；仓库状态或外部模型/索引无法证明时，不写成完整复现证据。
- 评测/窗口/RAG 提示组合 `173 passed, 1 warning`；完整工程门已通过：后端 `1222 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-evaluation-run-manifest-optimization.md`。

## 2026-09-13 记忆质量评测闭环

- 对照 [Mem0 memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) 的 Ingest → Search → Evaluate、[LangMem 的记忆类型](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[Graphiti 的时间事实/来源历史](https://github.com/getzep/graphiti) 和 [Letta 的 core/archival 分层](https://github.com/letta-ai/skills/blob/main/letta/agent-development/references/memory-architecture.md)，本地新增 `scripts/eval_memory_quality.py` 与冻结数据集 `eval/memory_quality_v0.1.jsonl`。
- 评测通过真实 `LearningStore` 临时工作区检查基础召回、项目/研究窗口隔离、更新后当前事实、历史时间视图和显式冲突淘汰；6 个场景、7 个查询全部通过，专项 `2 passed`，召回率 `1.0`，旧/越界事实泄漏率 `0.0`。该结果只证明冻结小集的契约，不证明真实用户长期记忆效果。
- 完整工程门已通过：后端 `1224 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-memory-quality-evaluation-optimization.md`。

## 2026-09-13 RAG 索引 lineage

- 对照 [DVC 的内容校验和指针](https://github.com/treeverse/dvc.org/blob/main/content/docs/user-guide/project-structure/dvc-files.md)、[MLflow 的运行输入/输出 lineage](https://github.com/mlflow/mlflow/blob/master/docs/docs/classic-ml/tracking/tracking-api/index.mdx)、[Qdrant 的 snapshot 校验边界](https://github.com/qdrant/qdrant/blob/master/src/common/snapshots.rs) 和 [LlamaIndex 的多 store 持久化](https://docs.llamaindex.ai/en/v0.9.48/module_guides/storing/customization.html)，普通 RAG run manifest 现在记录固定允许列表中的本机派生索引 fingerprint。
- 评测资产专项 `53 passed`；运行清单不再只绑定代码和标签，还能显式说明索引是否 `captured`、`unbound`、`missing_workspace` 或 `empty`。它仍不是原子 DB snapshot，也不证明外部模型完全复现。
- 完整工程门已通过：后端 `1224 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-rag-index-lineage-optimization.md`。

## 2026-09-13 GraphRAG 路由质量门

- 对照 [Microsoft GraphRAG 的 Local/Global 查询划分](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md)、[Local Search 的图+原文上下文](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)、[LightRAG 的显式 query mode](https://github.com/HKUDS/LightRAG/blob/main/lightrag_webui/src/api/lightrag.ts) 和 [Haystack ConditionalRouter](https://docs.haystack.deepset.ai/docs/conditionalrouter)，本地新增 `scripts/eval_graphrag_route.py` 与冻结路由数据集。
- 4 个路由样本全部通过，专项 `2 passed`；普通事实未升级 GraphRAG，关系问题走 local，跨文档总结走 global，关系信号覆盖“整体”时 local 优先。该证据只覆盖路由契约，不覆盖实体/社区/答案质量。
- 完整工程门已通过：后端 `1226 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-graphrag-route-evaluation-optimization.md`。

## 2026-09-13 研究窗口角色质量门

- 参照 [LangGraph 的显式状态/条件路由](https://langchain-ai.github.io/langgraph/how-tos/state-reducers/) 和 [LangMem 的 semantic/episodic/procedural 记忆边界](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)，新增 `eval/workflow_role_quality_v0.1.jsonl` 与 `scripts/eval_workflow_roles.py`，直接评测真实 `plan_workflow()`、窗口检索 profile/focus、RAG 开关、输出章节和引用命名空间。
- 论文/实验/会议窗口各 2/2 通过；缺论文、缺第二份实验材料、缺会议记录的样本均按预期拒绝，6/6 和各项契约指标均为 `1.0`。该证据只证明角色契约，不证明答案质量，也不引入无限 Agentic RAG 循环。
- 工作流/RAG/角色专项 `122 passed, 1 warning`；完整工程门已通过：后端 `1228 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。详见 `docs/reports/2026-09-13-workflow-role-quality-optimization.md`。

## 2026-09-13 TaskJournal durable step 尝试历史

- 对照 [DBOS 的 durable workflow step/checkpoint](https://docs.dbos.dev/python/tutorials/workflow-tutorial)、[workflow recovery](https://docs.dbos.dev/production/workflow-recovery) 和 [并发执行边界](https://docs.dbos.dev/explanations/concurrent-executions)，新增 `runtime_task_step_attempts`，保留每次 step 的幂等键、lease epoch、状态、时间和错误；`get_task()`/既有公开投影均可查看历史。
- 失败优先回归复现旧代码重试时丢失上一轮错误；实现后失败重试、数据库重开、孤儿恢复和迟到回调保护均通过。TaskJournal `17 passed`，任务/PPT/会话/sidecar 组合 `89 passed`。
- 完整工程门已通过：后端 `1229 passed, 1 warning`，前端生产构建和 7 组测试 `49 passed`，Tauri `cargo check --locked` 通过。PPT 面板现在可展开查看执行轨迹；该优化只提高本地恢复与审计可见性，不引入 DBOS/Temporal，也不宣称外部 exactly-once；详见 `docs/reports/2026-09-13-task-attempt-history-optimization.md`。

## 2026-09-13 记忆类型投影与上下文治理

- 参照 [Microsoft Memora](https://github.com/microsoft/Memora)、[zmem](https://github.com/zerkerlabs/zmem) 和 [projectmem](https://github.com/riponcm/projectmem) 的类型/准入/确定性投影思路，SoulDrive 现将用户偏好、项目/工作流事实和 Skill 显式标记为 `policy / semantic / procedural`；未知 subtype 进入 `unknown`。
- `gate_memory_context()` 不再把“能搜到”直接当成“可注入”：未知 family 和声明与 subtype 不一致的 family 都被拒绝并记录原因；`memory_health()` 报告存量未分类项但不回显正文、不自动改型。现有工作流/项目/时间范围门仍是权威约束。
- 该专项 `94 passed`，完整工程门后端 `1234 passed, 1 warning`、前端 `49 passed`、Tauri 通过。情景记忆仍由会话、实验台账和 TaskJournal 提供，尚未新增统一 episodic 检索投影；本轮不宣称长期记忆效果或答案准确率提升。详见 `docs/reports/2026-09-13-memory-family-governance-optimization.md`。

## 2026-09-13 研究窗口记忆搜索命名空间 API

- `/learning/search` 现在与聊天运行时共享 `workflow_id` 白名单和 LearningStore 范围门，合法窗口可以回放自己的 `workflow_fact`，未知窗口在请求模型层返回 422；审计只记录窗口、项目和计数。
- 窗口搜索专项 `2 passed, 1 warning`；含该修复的完整后端 `1235 passed, 1 warning`、前端 `49 passed`、Tauri 通过。该修补只证明运行时/诊断入口一致，不等于完成统一 episodic memory 或答案质量提升。详见 `docs/reports/2026-09-13-workflow-memory-search-namespace-optimization.md`。

## 2026-09-13 记忆搜索入口读取门

- `/learning/search` 现在在 LearningStore 结果之后复用 `gate_memory_context()`，并返回不含正文的 `memory_gate` 决策；未知 family、敏感/指令型存量不能绕过读取门直接进入调用方上下文。该行为与 [zmem](https://github.com/zerkerlabs/zmem) 的 search/inject 分离原则一致，但不引入其 Merkle receipt。
- 学习 API/记忆组合专项 `170 passed, 1 warning`；完整后端 `1237 passed, 1 warning`、前端 `49 passed`、Tauri 通过。该优化只收紧本地准入边界，不等于完成统一 episodic projection、事实真实性或外部 exactly-once。详见 `docs/reports/2026-09-13-memory-search-read-gate-optimization.md`。

## 2026-09-10 存量危险记忆诊断

- `memory_health()` 复用记忆写入/读取门的明确危险内容检测，报告 `memory_untrusted_instruction` / `memory_sensitive_content`，但只输出 ID 和范围，不把存量正文复制到诊断响应。
- 该只读诊断与现有 `memory_payload_unreadable`、chunk 完整性、版本时间区间和 pending outbox 检查共存；不自动修复或扩大项目读取范围。
- 历史版本与 pending outbox 载荷专项的学习/会话/RAG/记忆组合回归为 `280 passed, 1 warning`；最新 salience guard 后的组合回归为 `281 passed, 1 warning`，记忆/RAG/学习组合为 `246 passed, 1 warning`，回答质量/RAG/GraphRAG/上下文门/检索组合为 `225 passed`，会话/outbox 专项为 `40 passed`，学习存储/记忆运行时/记忆门专项为 `83 passed`，完整回归为后端 `1155 passed, 1 warning`；窗口检索焦点新增回归为 `2 passed, 1 warning`，来源消息绑定专项为 `3 passed, 1 warning`，outbox 完成回执专项为 `2 passed, 1 warning`，Skill run 完成态专项为 `2 passed, 1 warning`，memory capsule 去重专项为 `1 passed`，TaskJournal/PPT lease fencing 专项为 `38 passed, 1 warning`，memory outbox 过期 claim fencing 专项为 `3 passed`；另有聊天自动 Skill 运行幂等、完成态单调性和记忆来源会话 provenance 回归；紧预算 provenance、同分稳定排序、记忆管理列表确定性排序、待审核列表确定性排序、outbox 最老优先排序、RAG 来源身份、GraphRAG 观测身份、检索质量观测身份和回答质量来源印证身份证据分别见 `docs/reports/2026-09-12-memory-capsule-provenance-budget-optimization.md`、`docs/reports/2026-09-12-memory-deterministic-tiebreak-optimization.md`、`docs/reports/2026-09-12-learning-store-list-deterministic-order-optimization.md`、`docs/reports/2026-09-12-learning-review-list-deterministic-order-optimization.md`、`docs/reports/2026-09-12-outbox-oldest-first-order-optimization.md`、`docs/reports/2026-09-12-rag-source-diversity-identity-optimization.md`、`docs/reports/2026-09-12-graphrag-source-observability-identity-optimization.md`、`docs/reports/2026-09-12-retrieval-observability-source-identity-optimization.md`、`docs/reports/2026-09-12-answer-quality-source-corroboration-optimization.md`；详见 `docs/reports/2026-09-10-memory-health-unsafe-content-optimization.md`、`docs/reports/2026-09-10-memory-health-outbox-payload-optimization.md`、`docs/reports/2026-09-10-memory-salience-guard-optimization.md`、`docs/reports/2026-09-10-workflow-focus-routing-optimization.md`、`docs/reports/2026-09-10-chat-skill-run-idempotency-optimization.md`、`docs/reports/2026-09-10-skill-run-terminal-monotonicity-optimization.md`、`docs/reports/2026-09-10-memory-source-session-provenance-optimization.md`、`docs/reports/2026-09-12-memory-source-message-binding-optimization.md`、`docs/reports/2026-09-12-memory-outbox-completion-ack-optimization.md`、`docs/reports/2026-09-12-skill-run-completion-fencing-optimization.md`、`docs/reports/2026-09-12-memory-capsule-id-dedup-optimization.md`、`docs/reports/2026-09-12-task-journal-lease-fencing-optimization.md` 与 `docs/reports/2026-09-12-memory-outbox-lease-expiry-fencing-optimization.md`。

## 2026-09-13 记忆读取决策摘要指纹

- 对照 [zmem 的 action/context receipt](https://github.com/zerkerlabs/zmem) 与 [功能指南](https://github.com/zerkerlabs/zmem/blob/main/docs/ZMEM_FEATURE_GUIDE.md)，`gate_memory_context()` 现在返回 `sha256:` 格式的 `decision_digest`；它承诺当前查询摘要、准入 ID、拒绝原因、预算和 capsule 摘要对应的读取决定，但不回显查询或记忆正文。
- 先在旧实现上复现 `decision_digest` 缺失，再以逆序候选稳定性和准入集合变化回归验收；运行时专项 `10 passed`，学习 API/读取门组合 `172 passed, 1 warning`。
- 完整工程门：后端 `1239 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。该指纹不是 Merkle/签名证明，不替代记忆事实核验、答案质量指标或跨存储 exactly-once；统一 episodic projection 仍未实现。详见 `docs/reports/2026-09-13-memory-read-decision-digest-optimization.md`。

## 2026-09-13 情景记忆只读投影

- 对照 [Graphiti 的 episode group/reference time/provenance](https://github.com/getzep/graphiti) 和 [projectmem 的 event-sourced deterministic projection](https://github.com/riponcm/projectmem)，新增 `core/episodic_view.py`，把已有会话 manifest、实验运行、项目决策和 durable task 统一成有界、带来源 ID 与时间语义的活动视图。
- `/projects/{project_id}/overview` 暴露 `souldrive_episodic_view_v0.1`；跨项目会话、无范围 durable task 和过期 `as_of` 事件不会进入结果，非法时间边界拒绝。专项 `3 passed`，项目接口组合 `24 passed, 1 warning`。
- `view_digest` 绑定排序后的可见事件、项目/时间边界、来源计数和受限摘要；输入顺序变化不改变摘要，事件内容或集合变化会改变摘要。完整工程门：后端 `1244 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。该视图不复制事实源、不自动抽取情景记忆、不注入 RAG，也不等于 Graphiti 双时间图谱、Agentic RAG 或答案质量提升。详见 `docs/reports/2026-09-13-episodic-view-optimization.md`。

## 2026-09-13 durable task 重复失败守卫

- 对照 [projectmem 的 Memory-as-Governance](https://github.com/riponcm/projectmem)，TaskJournal 现在按 `kind + input_digest + steps` 查找历史 `failed/interrupted` 任务，返回 `allow/warn` 及不含失败正文的结构化摘要；PPT 状态面板在告警时提示先检查执行轨迹。
- 失败优先先复现缺少 `repetition_guard` 的 AttributeError；TaskJournal/PPT/项目组合 `63 passed, 1 warning`，完整工程门后端 `1245 passed, 1 warning`，前端 `49 passed`，Tauri `cargo check --locked` 通过。
- 该守卫只做精确输入提醒，不阻断、不做语义相似推断、不自动写长期记忆或 RAG；新的 request/id/artifact 被纳入输入摘要时可能不会命中，不能把它描述为全面重复检测。详见 `docs/reports/2026-09-13-task-repetition-guard-optimization.md`。
