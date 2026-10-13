# 记忆语义投影版本栅栏优化

日期：2026-09-15  
范围：LearningStore 权威记忆、Redis 语义投影、聊天学习上下文

## 外部实践与真实缺口

对照 [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 和 [Mem0 how it works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)，本地 SQLite/加密记忆事实源应是权威记录，向量库只是派生检索层，并且检索结果要在调用方范围内再次校验。

SoulDrive 原有语义记忆合并只按 `memory_id` 取 Redis 相似度。若记忆正文已经更新到 `record_version=2`，而旧向量 `record_version=1` 因投影延迟仍处于 active，旧向量的高分会被直接加到新正文上。它不会造成跨项目泄漏，但会造成“内容已经更新，语义排序仍由旧 embedding 决定”的可复现一致性问题。

## 最小实现

- `_learning_context_for_query()` 从当前有效的 LearningStore 记录建立 `memory_id -> record_version` 权威表。
- `_semantic_memory_scores()` 对 Redis 返回的显式版本进行严格比较；版本不一致或显式版本不可解析的投影不参与分数合并，并返回 `stale_projection_count` 供检索审计观察。
- 老适配器若完全没有 `record_version` 仍保持兼容；生产 RediSearch 旧 schema 不返回该字段时，`search_vector()` 从同一派生 hash 回读 `record_version`，不改变旧索引 schema。
- Redis 语义结果仍不能单独产生记忆候选；最终候选必须来自 LearningStore 当前有效记录并经过既有项目、工作流、时间和内容门。

## 失败优先与证据

先加入旧投影高分反例；旧实现因 `_semantic_memory_scores()` 不接受权威版本表而失败。修复后：

- 旧向量 `stale-memory:v1` 被拒绝，新向量 `fresh-memory:v2` 保留；`stale_projection_count=1`。
- 原有“关键词未命中但项目语义记忆命中”回归保持通过。
- RediSearch 旧 schema 的 `hget` 兼容读取回归确认能恢复 `record_version`。
- 学习上下文与会话存储组合：`209 passed, 1 warning`；完整工程门禁：后端 `1546 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。

后续非有限分数修复后的收尾复核：完整工程门禁后端 `1550 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过；冻结记忆质量 `7/7` 场景、`8/8` 查询通过，GraphRAG 路由 `4/4` 通过，研究窗口 runtime `5/5` 通过。

## 边界

这证明版本身份会在本地调用链上闭合，不证明真实 Redis/RediSearch 故障下的跨存储原子提交、投影延迟 p95、向量召回率或答案事实准确率；没有真实 Redis 实例时，后端仍是 fake-client 契约测试。
