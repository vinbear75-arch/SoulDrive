# SoulDrive 记忆系统外部实践对照与本轮优化

日期：2026-09-09  
范围：本机单用户、项目空间、会话记忆、长期偏好、RAG 注入与本地持久化

## 1. 结论先行

本轮没有把 Mem0、Graphiti/Zep、Letta 或 LangGraph 作为新的运行时依赖引入。对 SoulDrive 当前规模而言，最短可验证路径是保留现有“加密 SQLite 事实层 + 加密文件归档 + 可选 Redis/PostgreSQL 派生同步”，补齐它们已经被成熟实现反复证明有价值的边界：命名空间、生命周期、撤销、重试幂等和不可信数据隔离。

本轮已落地并通过定向测试的改动：

1. **审核写入可幂等重放**：审核完成后保存 `review_id`、项目范围和 HMAC 决策摘要。相同请求重试会返回原资产，不会重复创建；不同决策重试返回冲突，不会覆盖原结果。返回值显式提供 `idempotency_key` 和 `replayed`。
2. **项目记忆策略真正生效**：项目目录中已有的 `project_plus_global` / `project_only` 现在会约束长期记忆列表、学习搜索、RAG 注入和反思草稿的候选去重范围。`project_only` 不再只是展示字段。
3. **草稿绑定当前项目**：前端反思草稿请求携带 `project_id`；候选检查只比较当前项目可见范围，避免其他项目的记忆影响重复/冲突判断。
4. **记忆注入边界明确**：长期记忆和 Skill 正文在生成提示中标记为不可信数据，不能作为可执行指令，也不能替代文档证据或改变当前用户指令。
5. **归档同步不误复活已撤销记忆**：接受结果重放或存储补偿时，非活动记忆走撤销/向量移除路径；活动记忆才写入活动归档和可选向量派生存储。项目 ID 优先从资产本身取得，避免同步层丢失项目范围。
6. **显式时间有效性**：记忆新增 `valid_from` / `invalid_at` 字段；当前 RAG/学习上下文只读当前有效事实，学习搜索支持显式 `as_of` 时间点回溯，编辑接口可在保留历史资产的前提下设置或清除有效区间。
7. **编辑版本历史**：编辑记忆不再只覆盖当前加密 payload/chunk；旧版本保存到 `memory_versions`，按有效区间参与 `list_memories(as_of=...)` 与关键词 `search(as_of=...)`，删除/归档不会把旧版本重新暴露为活动记忆。

## 2. 外部开源实践与适配判断

### LangGraph：短期线程记忆与长期命名空间分开

LangGraph 将短期记忆放在 thread/state/checkpointer 中，将跨会话长期记忆放在带 namespace 和 key 的 Store 中；长期记忆的更新可以由应用逻辑显式触发，也可以由后台流程触发。参考：[长期记忆](https://docs.langchain.com/oss/python/langchain/long-term-memory)、[记忆概念](https://docs.langchain.com/oss/python/concepts/memory)、[持久化 Store](https://docs.langchain.com/oss/python/langgraph/persistence)。

SoulDrive 的最小对应关系是：

| 记忆层 | SoulDrive 当前载体 | 作用边界 |
| --- | --- | --- |
| 短期/会话 | `ConversationStore` 的 `session_id` 会话记录和摘要 | 只服务当前会话窗口，可恢复，不自动变成长期偏好 |
| 长期/全局 | `LearningStore.memories`，`scope=global` | 跨会话复用，默认只承载用户级偏好 |
| 长期/项目 | `LearningStore.memories`，`scope=project, project_id=...` | 当前项目可复用事实/工作偏好 |
| 派生索引 | `learning_chunks`、可选 Redis 向量、可选 PostgreSQL | 可重建，不取代加密事实层 |

本轮把已保存但未执行的 `memory_policy` 接到这条链路上：项目选择 `project_only` 时，长期全局记忆不会进入该项目的检索、候选判断或 RAG 注入；短期会话仍由自己的 `session_id` 隔离。

### Letta/MemGPT：常驻块、归档记忆和可撤销访问

Letta 的 memory block 会长期附着在 Agent 上并进入上下文；归档记忆则按需发现；官方文档还支持通过 attach/detach 控制访问，在不删除内容的情况下撤销当前访问。[Memory blocks attach/detach](https://docs.letta.com/tutorials/attaching-detaching-blocks/)、[MemFS 结构](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md)。

SoulDrive 不直接复制“常驻块始终注入”的做法，因为本项目必须让文档证据优先且控制上下文预算。现有 `gate_memory_context` 负责相关性、敏感内容、冲突和字符预算；本轮进一步在生成提示中将记忆正文定义为不可信数据。用户的停用/删除仍分别保留审计记录和移除活动索引。

### Graphiti/Zep：时间有效性、来源事件和混合检索

Graphiti/Zep 的事实边带有 `valid_at`、`invalid_at`、`expired_at` 等时间字段，并保留 episode 来源；检索同时使用语义和 BM25，再按图距离或其他策略重排。[Facts](https://help.getzep.com/facts)、[Adding episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)、[Searching the graph](https://help.getzep.com/graphiti/working-with-data/searching)。

本轮按最小可验证范围补上了这一缺口：`valid_from` / `invalid_at` 落在加密事实层及 `learning_chunks` 派生索引，当前上下文过滤未来或已失效事实，`/learning/search` 可用 `as_of` 做确定性历史查询；本次又增加 `memory_versions`，使“编辑前内容”也能在旧时点被召回，而不是只保留当前文本。随后又在普通聊天的记忆注入前增加了受限时间解析：单一明确日期、`上个月`、`上周`、`去年`会转换为本地截止时点并写入审计/提示；多日期比较保持未解析，继续要求显式实验选择。尚未实现 Graphiti 式 episode 图、自动事实失效和跨实体时间推理。

GraphRAG 也继续排除用户记忆：用户记忆不是文档图谱节点，避免把私人偏好误当作项目文献证据。

### Mem0：实体范围、图连接与现实中的冲突边界

Mem0 的官方实现将实体 ID（如 user/agent/app/run）与从文本提取的图实体区分开，图连接用于增强召回；其文档还提供不同范围的记忆过滤和显式更新/删除接口。[Graph Memory](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/graph-memory.mdx)、[API reference](https://github.com/mem0ai/mem0/blob/main/integrations/mem0-plugin/skills/mem0/references/api-reference.md)。

这支持 SoulDrive 继续坚持“业务范围先过滤，再评分”的顺序，而不是把向量相似度当成权限判断。本轮也没有直接采用自动语义冲突覆盖：公开仓库的讨论和问题记录显示，实际系统仍会遇到追加式记忆、并发更新和冲突合并边界；SoulDrive 当前采用人工审核、显式编辑、停用和删除，行为更可解释。

## 3. 记忆链路当前契约

```text
当前 session_id
   ├─ ConversationStore：短期历史/摘要，只绑定当前会话
   └─ /chat project_id
        ├─ ProjectCatalog.memory_policy
        ├─ LearningStore：先按 global/project 过滤
        ├─ valid_from/invalid_at 当前时点过滤
        ├─ 明确历史短语 → 受限 as_of 时点（歧义不猜）
        ├─ 关键词召回 + gate：相关性、敏感信息、冲突、时效衰减、预算
        ├─ memory capsule：最多 3 条、固定字符上限
        └─ RAG prompt：仅作不可信参考，不作文档证据或执行指令

反思草稿 → 人工决策 → HMAC 幂等写入 → 加密事实层
                                      └→ 文件归档/可选向量与 PostgreSQL 派生同步
```

不同研究会话窗口的实际作用是：

- 不同 `session_id` 拥有独立的历史、摘要和恢复位置；切换窗口不会把上一窗口的短期对话自动拼入当前窗口。
- 同一项目的长期记忆按项目策略共享；设置 `project_only` 后，该项目只读本项目长期记忆。
- 全局用户偏好默认不记录成项目事实；`user_preference` 仍按全局范围保存，但在 `project_only` 项目中不会注入。
- 记忆不是文献证据；事实性答案仍必须回到文档/实验证据并带引用。

## 4. 验收证据

- 改动前定向基线：`150 passed, 1 warning`。
- 本轮定向回归：`154 passed, 1 warning`，覆盖 `LearningStore`、memory gate/runtime、学习 API、RAG prompt。
- 当时的项目全量门为 `992 passed, 1 warning`；在后续 request receipt、租约续租、多文档评测契约、聊天完成态、时间有效性、答案来源可追溯性、TaskJournal fencing、聊天历史时点路由、记忆版本历史、工作流文档范围、中断 fencing、邮件预留竞态、长步骤恢复保护、记忆审核并发、自动记忆旁路幂等、写入边界安全、审核草稿项目绑定、自动记忆候选范围、待审核列表范围、项目记忆变更范围、GraphRAG 评测实际图路、记忆管理幂等键回执、学习同步 outbox、活动记忆接受恢复、claim/lease、问答上下文自动重放、workspace load 扫描、项目范围回放、完成标记故障注入、连续失败退避、普通路径 claim fencing、会话角色不可临时漂移和幂等派生投影范围修正循环后，最新全量门为后端 `1043 passed, 1 warning`，前端生产构建/7 组测试共 `44 passed`，Tauri `cargo check --locked` 仍通过。
- Python `compileall`：通过。
- `git diff --check`：通过；输出中的 CRLF 提示是工作树既有换行提示，不是空白错误。
- 新增回归覆盖：
  - 同一审核请求重试返回同一 `memory_id`，且不会新增第二条活动记忆；不同决策返回 409/ValueError。
  - `project_only` 在底层列表、关键词搜索和 RAG 上下文中排除全局记忆。
  - 记忆/Skill 内容以不可信数据进入生成提示。
  - 未来记忆和已失效记忆不会进入当前上下文；`as_of` 可回溯到历史有效区间，API 返回有效性字段；编辑前后的记忆版本分别在旧/新时点可检索。
  - 聊天中的单一历史时间表达会进入 `as_of`，并在 RAG prompt 标明本地截止时间；多日期比较不会自动选第一日期。

## 5. 尚未宣称完成的部分

1. **时间事实图**：已实现受限的 `valid_from/invalid_at`、显式 `as_of` 回溯和少量历史短语解析；尚未实现 `valid_at/invalid_at` episode 图、自然语言复杂日期/相对事件抽取、自动事实失效和跨实体时间推理。
2. **语义冲突合并**：尚未实现可靠的“同一事实槽位”识别和自动 UPDATE；当前以候选冲突检测 + 人工审核为准，不能声称“最新事实自动覆盖”。
3. **跨进程存储补偿**：文件归档是事实层，Redis/PostgreSQL 是可选派生层；已有加密 outbox、降级状态、workspace load 启动扫描和 SQLite claim/lease，已用测试模拟“下游成功后完成标记失败”并验证 pending 可重放，但仍未做真实进程被杀和跨存储 exactly-once 评测。
4. **回答质量指标**：153 个通过证明的是契约和回归，不等于长期记忆提升了答案准确率；仍需冻结的、项目隔离的带时间/冲突测试集和逐样本评测。
5. **真实产品链路**：本轮未把 Tauri 打包启动、真实模型生成、WPS/外部连接器作为自动测试证据。

因此本轮的状态是：记忆的范围、幂等、撤销、显式时间有效性和提示安全边界已完成并验证；时间图推理、自动冲突更新和真实运行时质量仍明确处于下一阶段，而不是隐含完成。
## 本轮后续耐久回放边界

学习同步 outbox 已增加 workspace load 的一次启动扫描：工作区解锁并首次建立知识库时，先用现有 claim/lease 尝试重放 pending 事件；失败仍保留 pending，由问答上下文或记忆管理器入口继续重试。该机制借鉴 durable execution 的“恢复时从持久化进度继续”原则，但没有常驻 worker，也没有把跨 SQLite/归档/向量同步宣称为 exactly-once。

同时补齐了直接幂等撤销/删除请求的派生投影范围：首次请求和同键重放均显式传递项目记忆的 `project_id`，与 outbox 回放保持一致；这避免“主库范围正确、重放下游范围缺失”的隐性漂移。

本轮全量门更新为后端 `1043 passed, 1 warning`，前端 `44 passed`，Tauri 检查通过；启动扫描、项目范围回放、完成标记故障注入和幂等删除派生范围单测均纳入 `tests/test_mcp_server_learning.py`。连续失败退避和普通路径 claim fencing 由 `tests/test_learning_store.py` 验证：首次失败快速重试，第二次失败后按指数间隔等待，未持有租约的路径不能完成他人事件。
