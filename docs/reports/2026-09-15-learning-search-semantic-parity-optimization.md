# Learning 搜索入口的语义检索对齐

日期：2026-09-15  
范围：`/learning/search`、LearningStore 权威记忆、Redis 语义投影、记忆读取门

## 外部实践与真实缺口

对照 [LangMem search memory tool](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/tools.py) 和 [LangGraph Store semantic search](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/stores.mdx)，语义检索应在显式 namespace 内执行，并保留 limit/filter 等边界；[Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 也把自然语言语义、关键词、元数据范围和可选重排作为独立信号。

SoulDrive 聊天上下文已经把关键词召回与 Redis 语义投影合并，但 `/learning/search` 作为 UI、诊断和重放入口此前只调用 `LearningStore.search()` 的关键词路径。一个没有关键词重合、但项目内语义相同的记忆在聊天可见、在搜索/诊断不可见，入口无法复现运行时行为。

## 失败优先与最小实现

新增 API 反例：关键词结果为空、当前项目 LearningStore 有一条有效记忆、语义投影返回同一 `memory_id`；旧实现返回空列表，修复后才允许命中。

实现保持现有边界：

1. `/learning/search` 先沿用既有项目/global、时间和 workflow namespace 过滤，再从同一批权威 `list_memories()` 建立 `memory_id -> record_version` 表。
2. Redis 只提供语义分数；候选事实仍来自 LearningStore，经过非有限值、版本/ID 投影栅栏和既有 `memory_gate` 后才返回。
3. 返回 `memory_retrieval` 诊断计数，区分关键词计数、语义投影计数、后端和过期/无效投影；旧 store 适配器缺少 `list_memories` 时保留关键词兼容路径。

## 回归证据

- API 语义反例修复后定向：`3 passed, 121 deselected, 1 warning`。
- 语义分数、版本/ID 栅栏回归：`3 passed, 122 deselected, 1 warning`。
- 完整 `tests/test_mcp_server_learning.py`：`125 passed, 1 warning`。
- 本轮收尾工程门禁：后端 `1552 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 检查通过。
- 冻结评测保持：记忆质量 `7/7` 场景、`8/8` 查询通过，`memory_recall_at_k=1.0`、陈旧/禁止泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口情景 runtime `5/5`（baseline `2/5`）。

## 边界

这只保证聊天与 Learning 搜索入口在本地语义检索契约上对齐，不等于语义召回率、答案准确率、重排收益、真实 Redis/RediSearch 性能或真实 UI/E2E 已达标；候选事实仍严格受 LearningStore 和 memory gate 控制。
