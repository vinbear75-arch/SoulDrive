# 记忆读取冲突裁决优化报告

日期：2026-09-10

## 目标

补齐记忆读取链路中“同一主题的全局事实和项目事实同时进入回答上下文”的可复现缺口。当前 SoulDrive 已有项目范围过滤、时间有效区间、版本历史和 `source_trace`，本轮只补读取门的最小冲突裁决，不引入新的记忆框架或图数据库。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 将记忆事实建模为带 `valid_at`/`invalid_at` 的时间事实，并保留事实来源的 episode；同一 group 的 episode 顺序处理，避免只按相似度把历史事实和当前事实混在一起。
- [Graphiti edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 在抽取阶段区分 duplicate fact 与 contradicted fact，说明“相关”不能等价为“可以同时注入”。
- [Letta memory schemas](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 的 archival memory 结果保留时间戳、标签和可回溯的结果字段，读取层需要把来源和时间元数据一并带出。
- Graphiti 的[失效事实检索问题](https://github.com/getzep/graphiti/issues/1728)也说明，仅在写入时处理失效还不够，读取端必须继续过滤或裁决过期/冲突事实。

SoulDrive 已经具备时间有效性和来源追踪，因此本轮采用更小的本地方案：在最终 memory read gate 增加保守的同主题裁决，而不是引入 Graphiti 的在线实体解析。

## 发现的缺口

`core/memory_runtime.py::gate_memory_context` 之前只识别少数固定词对的“偏好冲突”。当两个 active memory：

1. 属于同一个 memory subtype；
2. 具有相同的非空标题；
3. 内容不同；

它们会同时进入 capsule。于是“全局默认数据库”和“当前项目数据库”可能同时注入，模型需要自行猜测，且 gate 没有留下明确的冲突裁决记录。

## 最小实现

- 新增 `_looks_like_conflicting_topic`：只对同 subtype、同规范化标题、内容不一致的记忆触发，避免把不同主题或不同类型的记忆误合并。
- 复用现有 `_preference_authority` 顺序：项目范围优先，其次人工审核状态、新鲜度、相关性和更新时间；不新增另一套优先级。
- 被排除项在 gate 审计中记录 `memory_id`、`reason=conflicting_memory_topic` 和 `conflict_with`；胜出项仍保留原有 `source_trace`、时间有效性和项目字段。
- 不修改时间版本规则：`valid_from`/`invalid_at` 仍由存储层决定；不把相同标题启发式冒充通用实体消歧或事实推理。

## 测试证据

- 先写回归测试：旧实现实际返回 `['global-db', 'project-db']`，测试失败。
- 修复后 `tests/test_memory_gate.py`：`6 passed`。
- 记忆相关专项：`112 passed, 1 warning`，覆盖读取门、时间版本、学习存储并发、HTTP 学习/聊天链路。
- 仓库级 `scripts/check.ps1`：后端 `1063 passed, 1 warning`；Python compileall 通过；前端生产构建通过；7 组前端测试 `48 passed`；Tauri `cargo check --locked` 通过。
- 前端构建仍有 Vite 的单 chunk 大于 500 kB 提示；Starlette/httpx 弃用提示仍是环境警告，不是本轮失败。

## 边界与未完成项

- 这是同标题的保守裁决，不是完整的实体解析、数值矛盾检测、自然语言否定推理或 GraphRAG 事实失效引擎。
- 如果不同事实使用了不同标题，仍依赖写入审核、时间有效区间、项目过滤和证据门；不能把本轮规则宣传为“所有冲突自动解决”。
- 未新增 Agentic RAG、Mem0、Graphiti、Letta、LangGraph 或向量数据库依赖。
- 真实 Tauri/WPS、安装包、视觉/语音服务和冻结留出集上的答案质量仍需单独验收；本轮测试通过不等于这些门已关闭。
