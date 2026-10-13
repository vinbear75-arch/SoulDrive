# 2026-09-14 Durable 会话命名空间与 Skill 作用域边界优化

日期：2026-09-14  
范围：会话 manifest、聊天幂等请求、workflow memory namespace、项目 Skill front matter/cache 与写入边界。

## 1. 本轮真实缺口

继续沿着“研究窗口必须有不同且可验证的作用”审计后，发现四类旁路：

- 会话创建与读取没有统一拒绝未知 `workflow_id`；旧 manifest 的文件名与内部 `session_id` 不一致时，可能被当成合法会话加载；
- `/chat` 接受 `request_id`/`Idempotency-Key` 但没有 `session_id` 时，无法建立可恢复的持久请求回执，重试可能重新处理；
- `workflow_fact` 虽然已有读取门，但未知 workflow namespace 仍可能先进入候选或存储层，形成不可复用的孤儿记忆；
- Skill 的未知 `workflow_ids` 被归一化为空列表，而空列表代表“全局 Skill”；项目 Skill 的 front matter 和旧 adapter cache 也会丢失这个错误状态。

这些问题共同指向同一条工程不变量：客户端传入的窗口名称、持久会话身份、记忆 namespace 和程序性 Skill 可见范围，必须在最终写入/匹配边界 fail-closed，不能把“无法识别”降级为“全局”。

## 2. 对照的公开实现机制

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 使用稳定的 `thread_id` 作为可恢复会话状态边界；本地将持久 manifest 身份与 archive path 一致性作为同类不变量。
- [LangGraph stores](https://docs.langchain.com/oss/python/langgraph/stores) 使用 namespace 区分长期记忆；本地把正式 workflow id 目录和来源会话绑定前移到写入层。
- [Mem0 metadata filters](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 要求通过显式 identity/metadata 过滤检索记忆；本地沿用同一原则，不依赖 query 文本推断窗口。
- [Graphiti MCP provenance](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 以 group/episode 来源保留记忆归属；本地继续使用 session/project/workflow 三元范围，不引入外部运行时。

这些项目只提供机制参考，不代表 SoulDrive 已接入对应框架，也不构成答案质量证明。

## 3. 失败优先与最小实现

先加入反例，再修复：

1. 未知 workflow 的会话创建、旧 manifest 加载和 manifest/path 身份不一致均被拒绝；会话列表过滤坏 manifest，而不是把它列入可选窗口。
2. `/chat` 在存在请求幂等键而没有 `session_id` 时返回 `422`，避免把不可持久化的请求伪装成 durable replay。
3. `workflow_fact` 的候选校验、LearningStore 写入、读取 gate 与 health 诊断统一使用 `SUPPORTED_WORKFLOW_IDS`；当前和历史版本都能报告未知 namespace。
4. Skill 作用域解析返回“已知 token + invalid 标记”；未知或非法形状不再变成全局可见，运行时匹配、项目同步和候选写入均拒绝；adapter cache 命中同时校验 invalid 状态。
5. 项目 Skill front matter 保留原始 workflow scope，再由统一 normalizer 产生 `workflow_scope_invalid`，从发现层开始阻断错误作用域。

主要落点：

- `core/conversation_store.py`
- `core/mcp_server.py`
- `core/learning_validator.py`
- `core/learning_store.py`
- `core/memory_runtime.py`
- `core/skill_runtime.py`
- `core/project_skills.py`

## 4. 验证证据

- Skill、项目发现、安装、学习存储和 MCP 入口组合：`235 passed, 1 warning`。
- 最新完整 `scripts/check.ps1`：后端 `1473 passed, 1 warning`，`compileall` 通过；前端生产构建通过；8 组 UI 脚本共 `62 passed`；`cargo check --locked` 通过。
- 既有 warning 为 Starlette/httpx 测试客户端弃用提示，以及 Vite 的 chunk-size 提示，均未导致门禁失败。

## 5. 明确边界

本轮证明的是本机单用户服务内的会话、workflow namespace、Skill 可见性和幂等前置条件的一致性；不证明身份认证、跨进程 exactly-once、外部 SMTP/日历、真实 Tauri/WPS UI/E2E 或模型答案准确率。`normalize_skill_workflow_ids()` 的返回值仍只表达已知 token，调用方必须同时通过 `normalize_skill_payload()`/`is_skill_visible_in_workflow()` 的 invalid 标记，不能把单独的空列表当作合法全局范围。
