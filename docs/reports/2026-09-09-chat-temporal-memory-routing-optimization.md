# SoulDrive 聊天历史时点记忆路由优化

日期：2026-09-09

## 外部实践对照

- [Graphiti/Zep Adding Episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)把 episode 的参考时间与摄取时间分开，支持按时间点追溯事实来源。
- [Zep Facts](https://help.getzep.com/facts)保留事实的有效与失效边界，而不是只保留一条当前文本。
- [Mem0 Update Memory](https://docs.mem0.ai/core-concepts/memory-operations/update)强调更新记忆的时间/过期字段后要重新验证检索结果。

SoulDrive 当前规模不需要引入 Graphiti/Mem0 runtime；本轮只把已有 `valid_from/invalid_at/as_of` 接到普通聊天记忆注入链路。

## 发现的缺口

`/learning/search` 已支持显式 `as_of`，但普通 `/chat` 只调用当前时点搜索。用户问“请回顾上个月的实验结论”时，聊天层没有把这个时间意图传给记忆检索，历史有效事实无法稳定进入上下文。

## 最小实现

- 新增 `core/temporal_memory.py`，使用本地 `Asia/Shanghai` 时区解析三类相对历史表达：`上个月`、`上周`、`去年`；
- 支持单一明确日期（`2026-08-12` / `2026年8月12日`），解析为该本地日结束时点；
- 聊天学习上下文把解析结果同时传给 `list_memories` 与 `LearningStore.search` 的 `as_of`；
- `memory_time_scope` 进入审计和 RAG prompt，提示模型不要以当前事实替代历史时点；
- 多个明确日期不自动选一个，返回未解析状态，继续使用实验对比会话的显式 run 选择契约；
- 无历史表达时保持当前行为：按当前时点过滤记忆。

## 验收证据

- `tests/test_temporal_memory.py` 覆盖上个月、明确日期、多日期歧义和 prompt 时点标记；
- `tests/test_mcp_server_learning.py` 覆盖聊天学习上下文将同一 `as_of` 传给列表与检索；
- 定向 `tests/test_temporal_memory.py tests/test_mcp_server_learning.py tests/test_rag_prompting.py`：`121 passed, 1 warning`；
- 仓库级 `scripts/check.ps1`：后端 `1009 passed, 1 warning`，前端构建通过、7 组测试共 `44 passed`，Tauri `cargo check --locked` 通过；
- `git diff --check`：通过，剩余 CRLF 提示为 Git 工作树换行提示。

## 边界

这不是完整时间推理：不从自然语言中抽取复杂事件关系，不自动让新事实失效，不把多个日期猜成基线/当前运行，也不等于真实模型回答质量已经提升。实验数值比较仍必须通过 `experiment_compare` 会话和显式运行选择完成。
