# SoulDrive 本地业务智能体：生产级记忆与存储方向

版本：v0.1  
日期：2026-08-23  
状态：本机基础设施已接入，持续按验收标准迭代

## 运行位置

本机开发阶段只保留一个入口：`D:\AI_AGENTS\SoulDrive-main\启动SoulDrive.bat`。

```text
项目工作区: D:\AI_AGENTS\SoulDrive-main\runtime\workspace
历史会话:   D:\AI_AGENTS\SoulDrive-main\runtime\workspace\runtime\history
Redis 软件: D:\SOFTWARES\Redis
Redis 数据: D:\SOFTWARES\Redis\data
PostgreSQL 软件: D:\SOFTWARES\PostgreSQL\17
PostgreSQL 数据: D:\SOFTWARES\PostgreSQL\data
PostgreSQL 端口: 5432
```

PostgreSQL 的 D 盘实例用于本机开发启动入口；C 盘旧安装保留作为迁移回滚保障，未纳入 SoulDrive 启动链路。

## 1. 结论先行

SoulDrive 继续保持 Windows 本机、单用户、项目空间隔离，但内部架构按照生产系统的可靠性标准设计。单用户不等于玩具系统；它只意味着暂时不建设云端账号、租户和远程部署。

Redis、PostgreSQL 和本地文件系统应承担不同职责：

```text
文件系统       完整会话、附件、事件和长期记忆正文的事实源
PostgreSQL     结构化状态、关系、版本、索引、审计和可恢复工作流
Redis          短期工作记忆、热点缓存、并发协调、队列和快速检索
向量索引       只服务语义检索，不替代事实存储
```

不把 Redis 当作“所有聊天内容的数据库”，也不把 PostgreSQL 当作文件仓库。每一条重要数据必须能回答：来源是什么、是否完整、何时写入、能否恢复、是否经过用户确认。

## 2. 调研边界与证据等级

本调研查看了以下公开项目和文档：

- [OpenAI Codex](https://github.com/openai/codex)：公开仓库明确定位为运行在本机终端的 coding agent。公开代码可用于学习本地代理和工程工作流，但不能据此推断 OpenAI 云端产品的内部存储实现。
- [Anthropic Claude Code](https://github.com/anthropics/claude-code)：公开仓库明确定位为理解代码库、执行例行任务和 Git 工作流的终端 agent。公开仓库不等于 Anthropic 内部服务端架构。
- [LangGraph checkpoint-postgres](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres)：PostgreSQL 用于 durable、long-running workflows and agents 的 checkpoint 持久化；首次使用需要显式建表，且强调安全反序列化配置。
- [Letta](https://github.com/letta-ai/letta)：公开定位是具有高级记忆、可随时间学习和自我改进的有状态 agent 平台。它适合作为记忆分层和 agent state 的参考，不应直接复制其平台化部署复杂度。
- [Mem0](https://github.com/mem0ai/mem0)：公开说明包含 User、Session、Agent 多级记忆，支持语义、BM25、实体匹配融合、实体链接、时间推理、ADD-only 记忆提取和公开评测框架。

其中，项目 README 是直接证据；“SoulDrive 应该如何设计”属于本产品的工程决策，不能冒充上述项目的内部实现。

## 3. Redis 的生产级职责

Redis 在 SoulDrive 中不只是学习组件，而是本地服务拓扑中的实时状态层。

### 3.1 短期会话

按时间顺序的最近消息使用 Redis Streams 或带序号的 Hash/List 保存。每条消息至少包含：

- `session_id`
- `message_id`
- `sequence`
- `role`
- `created_at`
- `content_ref`
- `summary_version`
- `source_event_ids`

完整正文不依赖 Redis。Redis 只保存热数据和可重建索引，并设置明确 TTL、命名空间和最大长度。

### 3.2 语义检索

只有在需要“从较早会话中找相关内容”时，才为摘要、事件或记忆建立 Redis Vector Index。最近消息的顺序读取不需要 embedding。

向量记录必须带过滤字段：

```text
project_id
session_id
memory_type
created_at
source_hash
visibility
```

记忆检索按作用域处理：全局记忆始终可用，当前项目记忆优先；其他项目记忆默认不注入，只有用户明确发起跨项目查找时才进入受控检索。

### 3.3 真实生产能力

Redis 还负责：

- 本地索引任务锁和会话写锁
- 推理请求去重和幂等键
- 短期任务队列
- sidecar 健康状态和热点诊断缓存
- 需要时的事件流

Redis 应启用 AOF 或等价持久化策略，明确 maxmemory 和 eviction policy，并设计重启恢复流程。不能因为它是缓存就完全不考虑崩溃恢复。

## 4. PostgreSQL 的生产级职责

PostgreSQL 是 SoulDrive 的结构化控制平面，不是简单地把 SQLite 换成更大的数据库。

### 4.1 建议保存的数据

- `projects`：项目和工作区元数据
- `sessions`：会话生命周期、标题、项目绑定和当前摘要版本
- `session_events`：用户消息、回答、工具调用、证据引用和状态变更的索引
- `workflow_checkpoints`：可恢复的索引、报告、PPT 和 Agent 工作流状态
- `memory_records`：长期记忆的类型、来源、版本、置信度和确认状态
- `outbox_events`：需要写入 Redis、文件或外部连接器的可靠事件
- `model_runs`：模型、参数、索引版本、延迟和结果引用

大正文和二进制附件仍然保存在加密文件系统，PostgreSQL 只保存内容哈希、相对路径、大小、版本和完整性状态。

### 4.2 事务边界

一次回答至少要形成可追踪关系：

```text
session_id
  -> user_message_id
  -> retrieval_run_id
  -> evidence_ids
  -> model_run_id
  -> assistant_message_id
  -> memory_candidates
```

需要跨 Redis、PostgreSQL 和文件系统写入时，采用 PostgreSQL outbox + 可重试消费者，而不是假设三个系统可以组成一个跨服务事务。每个事件必须幂等，重复消费不能产生重复消息或重复记忆。

### 4.3 PostgreSQL 是否使用 pgvector

第一阶段不让 PostgreSQL 同时承担向量库和控制平面。Redis Vector Index 用于短期热记忆，现有知识库继续负责文档 RAG。等长期记忆规模、过滤复杂度和召回指标有基线后，再通过实验决定是否使用 pgvector。

## 5. 完整会话与摘要

“摘要无损”需要严格表述：自然语言摘要本身不可能保证无损。SoulDrive 采用可恢复的压缩协议：

1. 完整消息按追加方式保存到加密 `messages.jsonl`。
2. 摘要只作为短期上下文压缩结果，不覆盖原文。
3. 摘要保存 `covered_sequence_start/end` 和 `source_message_ids`。
4. 摘要生成后执行覆盖检查，检查实体、数字、时间、约束、用户决定和未完成任务。
5. 检查失败时保留旧摘要，并把相关原文重新加载到上下文。
6. 任何时候都可以从完整会话重建摘要，因此系统级信息可无损恢复。

当前目录：

```text
runtime/workspace/runtime/history/conversations/{session_id}/
  manifest.json
  messages.jsonl.enc
  events.jsonl.enc
  tool_calls.jsonl.enc
  summaries/summary-{version}.json.enc
  attachments/
```

Redis 缺少历史消息时，读取顺序为：Redis 热数据 -> PostgreSQL 元数据和引用 -> 加密文件系统 -> 回填 Redis。读取过程必须记录 cache hit、回源原因、恢复的序列范围和摘要版本。

## 6. 长期记忆和自学习

长期记忆不等于保存所有聊天。只有具备复用价值的特定事件才进入候选：

- 用户明确表达的偏好
- 用户确认的工作规则
- 项目中稳定存在的事实
- 已验证成功的解决方案
- 用户纠正模型后的规则
- 工具执行成功且可重复的操作经验

每条记忆需要经过：

```text
提取候选 -> 去重/实体归一 -> 来源校验 -> 置信度评估 -> 用户确认或自动准入 -> 版本化保存
```

推荐状态：`candidate`、`confirmed`、`rejected`、`expired`。

记忆正文可以保存在加密文件系统，PostgreSQL 保存结构化索引，Redis 保存热点记忆和向量。每条记忆必须保留来源 `session_id`、消息序号、项目、时间、模型版本和证据引用。

自学习机制的安全边界：

- 不从模型猜测中直接形成永久偏好。
- 用户明确纠正优先级最高。
- 记忆可撤销、过期和重新验证。
- 失败经验只能作为候选，不直接改变 Agent 行为。
- 每次记忆命中都能显示来源和原因。

## 7. 本地单用户不等于玩具部署

SoulDrive 不做云端多租户，但本机仍应模拟生产级边界：

- Redis、PostgreSQL、sidecar 分成独立进程。
- 每个服务使用独立本地凭据和最小权限。
- 只监听 `127.0.0.1`，不默认暴露局域网。
- 启动、健康检查、迁移、停止和恢复都进入唯一开发启动入口。
- Redis/PostgreSQL 故障时有明确降级策略，而不是静默丢会话。
- 文件、数据库和 Redis 的写入都有版本、哈希和审计记录。

这是真实生产系统的缩小部署形态，而不是把服务“为了学习”接上后不使用其可靠性能力。

## 8. 外部连接器

邮件、日历和其他外部服务通过本地 API/Connector 调用即可，不需要云端账号系统。统一流程为：

```text
生成草稿 -> 证据校验 -> 用户确认 -> 幂等发送 -> 保存结果和外部 ID
```

OAuth、SMTP 授权码和密钥进入 Windows Credential Manager，不得进入 Redis、PostgreSQL、会话文件、日志或模型上下文。

## 9. SoulDrive 后续验收标准

后续实现不能只验收“能回答”，至少要验证：

- Redis 重启后能从 PostgreSQL/文件恢复热会话。
- PostgreSQL 重启或连接失败时不会丢失完整会话。
- 文件损坏或哈希不一致时系统明确报错，不静默生成摘要。
- 摘要覆盖范围可追溯，历史不足时能正确回源。
- 同一事件重复消费不会重复写入消息和长期记忆。
- 普通项目对话不自动注入其他项目记忆；用户明确发起跨项目查询时，应记录范围并受控召回，且不能混入当前项目长期记忆。
- 用户删除或撤销记忆后，Redis 热缓存、PostgreSQL 索引和文件记录最终一致。
- 外部邮件发送具有确认令牌和幂等键。
- Redis/PostgreSQL 延迟、命中率、回源率、摘要失败率和记忆命中率都有日志。

## 10. 实施顺序

1. 先冻结会话、事件、记忆和 outbox schema。
2. 建立当前 SQLite/文件会话的 baseline 和恢复测试。
3. 引入本机 PostgreSQL，先实现 checkpoint、session metadata 和 outbox。
4. 引入 Redis，先实现短期消息、锁、TTL、回填和故障恢复。
5. 实现摘要覆盖检查和历史懒加载。
6. 实现长期记忆候选、去重、确认、过期和自学习评测。
7. 最后接入 Redis 向量检索，并与现有 RAG 文档向量检索分开评测。

每一步都必须保留 SQLite/文件系统降级路径，直到 PostgreSQL 和 Redis 的恢复、幂等和一致性测试通过。
