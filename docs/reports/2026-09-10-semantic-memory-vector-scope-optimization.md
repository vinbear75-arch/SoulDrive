# SoulDrive 可选语义记忆投影范围与时间过滤优化

日期：2026-09-10

本轮只处理“已有 embedding 投影是否安全可用”，没有把未完成的语义记忆能力包装成已接入 RAG。

## 外部实践对照

- [Mem0 Search Memory](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx)要求搜索带实体过滤，例如 `user_id`，并把 `top_k`、threshold、rerank 和过滤作为独立的检索参数；这对应 SoulDrive 的项目/全局范围、活动状态和时间窗口。
- [LangGraph Stores](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/stores.mdx)把 namespace、semantic query、filter 和分页分开；[LangGraph Store Base](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/store/base/__init__.py)明确语义索引是可选能力，未配置索引时不能假定语义搜索存在。
- [Redis vector search](https://redis.io/docs/latest/develop/ai/search-and-query/query/vector-search/)支持在 KNN 前使用过滤表达式；[Qdrant filtering](https://qdrant.tech/documentation/search/filtering/)把 payload 过滤作为向量搜索的硬条件，并建议为常用过滤字段建立索引。两者都不支持从向量相似度本身推断租户、项目或有效期。

## 当前代码缺口

SoulDrive 在 `SOULDRIVE_EMBEDDING_PROVIDER=ollama` 时，会在长期记忆准入旁路生成 embedding，并通过 `ProductionSessionStore.persist_memory` 写入可选 Redis 向量存储。但旧向量记录只保存 `session_id`、`memory_type`、`memory_id` 和归档路径；`search_vector` 也没有 `project_id`、活动状态或 `as_of` 参数，而且当前核心问答没有调用它，普通 `/learning/search` 仍是受项目/时间过滤的 SQLite 关键词链路。

如果未来直接把旧向量接进 RAG，最坏情况是向量相似度先命中另一个项目，再由调用方补救范围；这与当前“项目过滤先于召回”的安全契约冲突。

## 最小实现

`core/state_backends.py` 的 Redis 向量投影现在：

- 写入 `projection_version=memory_vector_v2`、`scope`、`project_id`、`status`、`valid_from`、`invalid_at`；RediSearch 新索引同时声明这些可过滤字段；
- Vector Set 和 RediSearch 查询均接受 `project_id`、`include_global`、`as_of`，并在返回前强制检查 scope、active 状态和有效窗口；
- 没有新投影版本或缺失范围元数据的旧向量不返回，宁可暂时降低召回，也不把旧数据猜成全局数据；
- `ProductionSessionStore` 的 Redis outbox 重放携带同一组元数据，避免实时写入和恢复写入产生不同范围语义；
- 仍未接入普通 `/chat` 或 `/learning/search`，因为当前尚无查询侧 embedding、混合关键词/向量排序、阈值校准和冻结评测集。

## 回归证据

- `tests/test_conversation_store.py`：`24 passed`，覆盖 Vector Set 写入、项目/全局过滤、未来有效期过滤、ProductionSessionStore 元数据转发和会话范围。
- `scripts/check.ps1`：后端 `1081 passed, 1 warning`；Python compileall 通过；前端构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 下一步进入条件

只有同时具备以下条件，才考虑把语义记忆接入普通问答：

1. 查询 embedding 的本机提供方、失败降级和显存/延迟预算；
2. 与 SQLite 关键词记忆检索的同数据、同项目、同时间窗口比较；
3. 逐样本报告记忆 Recall@K、项目串扰率、时间有效性错误、最终回答引用/拒答影响和 P50/P95；
4. 旧 `memory_vector_v1` 投影的重建或明确清理方案。

在这些证据出现前，当前结论是“可选投影已具备安全范围契约”，不是“语义记忆 RAG 已完成”。
