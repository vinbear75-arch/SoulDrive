# Redis 语义记忆预过滤与候选召回优化

日期：2026-09-14

## 发现

`RedisSessionStore.search_vector()` 过去先取近邻候选，再在 Python 中过滤 `embedding_model`、`valid_from`/`invalid_at`、workflow 和项目范围。若过期向量、其他 embedding 模型或其他 workflow 的向量占据前 `candidate_k`，合法记忆可能在后置过滤前就被截掉，导致“索引里有可用记忆但搜索返回空/不足”的可复现候选召回缺口。

## 公开实践对照

- [Redis vector search concepts](https://redis.io/docs/latest/develop/ai/search-and-query/vectors/) 将 KNN 的 primary filter 定义为向量搜索前的筛选条件，而不是只在结果返回后过滤。
- [Redis filtered vector sets](https://redis.io/docs/latest/develop/data-types/vector-sets/filtered-search/) 支持 `VSIM FILTER`，并提供 `FILTER-EF 0` 在稀疏过滤条件下继续扫描直到找到足够候选。
- [Redis tag filters](https://redis.io/docs/latest/develop/ai/search-and-query/advanced-concepts/tags/) 支持 TAG 的精确、负向和组合筛选，适合当前的 project/status/model/workflow 元数据。

本轮只收紧当前 Redis 派生语义投影的候选查询，不引入新的向量数据库或重型 RAG 框架。

## 实现

- RediSearch KNN 查询将 `projection_version`、`status`、项目/global scope、session、`embedding_model`、workflow（指定 workflow 与无绑定共享记忆）以及有效时间范围放入 primary pre-filter。
- Redis 8 Vector Set 查询将必需的版本、状态、项目/session 和 embedding model 元数据放入 `FILTER`，并使用 `FILTER-EF 0`，避免稀疏 scope 过滤过早停止；workflow/时间仍由后置 scope gate 裁决以兼容缺失属性的旧投影。
- 保留 `_vector_result_matches_scope()` 的后置 fail-closed 校验，且过滤命令在旧部署不支持时降级到原查询，不把一次可选能力失败变成记忆读取失败。
- 不改变本地关键词记忆事实源、embedding 模型绑定、project/global 语义或向量 TTL 策略。

## 验证

- 失败优先：新增回归在修复前确认 RediSearch/Vector Set 查询没有携带预过滤，修复后通过。
- 会话存储、Redis 向量 scope、记忆投影和 durable 回归：`75 passed`。
- 完整工程门禁：后端 `1510 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。本机无真实 Redis 服务，因此查询语法、版本差异、过滤召回率和真实 p95 仍未完成实例级验证。

## 边界

这改善的是候选集进入后置 scope gate 之前的召回完整性，不等于 embedding 质量、答案事实准确率或跨存储 exactly-once 证明；Vector Set 的 workflow/时间可选属性仍依赖后置门，不能把本轮写成完整向量索引迁移或 Agentic RAG 验收。
