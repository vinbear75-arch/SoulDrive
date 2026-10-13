# 记忆列表并列时间确定性排序优化

日期：2026-09-12

## 结论

本轮修复 `LearningStore.list_memories()` 在多个记忆 `updated_at` 相同的时候依赖 SQLite 返回顺序的问题。现在仍以 `updated_at` 倒序为主排序，最后使用 `memory_id` 升序作为稳定 tie-breaker；不改变项目 scope、global 回退、有效时间区间、历史版本过滤或记忆门自己的相关性排序。

## 外部实践对照

- [Graphiti 的时间水印与 episode 排序](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py) 区分 ingestion/wall-clock 与事件 `valid_at`，并在同一时间语义内使用确定性顺序处理增量摘要。
- [Mem0 的记忆元数据与查询过滤](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 将 actor/session/run 元数据作为存储与检索边界，而不是把展示文本当作身份。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将记忆文件纳入可版本化的上下文存储，稳定的文件/条目身份是可恢复和审计的基础。

本地只补确定性排序，不引入外部记忆数据库、图数据库或新的依赖。

## 失败优先与实现

1. 新增 `tests/test_learning_store.py::LearningStoreTests::test_list_memories_uses_stable_memory_id_tie_breaker`，以 `mem-b`、`mem-a` 的逆序插入和相同 `updated_at=100.0` 复现旧结果：列表返回 `mem-b, mem-a`。
2. `core/learning_store.py::list_memories()` 现在使用 `(-updated_at, memory_id)` 排序，消除数据库物理返回顺序对 UI、调试和后续上下文准备的影响。

## 验证证据

- 学习存储、记忆运行时、记忆门专项：`82 passed`。
- `scripts/check.ps1`：后端 `1152 passed, 1 warning`，Python compileall 通过。
- 前端生产构建通过，7 组前端测试共 `48 passed`。
- Tauri `cargo check --locked` 通过。

## 边界

本轮只证明同一更新时间下列表顺序确定，不证明记忆内容正确性、时间有效性判定、语义检索质量、答案准确率、跨进程 exactly-once 或真实 UI/E2E。历史版本同一 `memory_id` 的更细粒度版本排序仍由版本有效性与调用方门控负责。
