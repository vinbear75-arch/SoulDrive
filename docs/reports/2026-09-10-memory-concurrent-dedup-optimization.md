# 记忆并发精确去重优化报告

日期：2026-09-10

## 目标

补齐不同 reflection review 并发接受同一事实时的 TOCTOU 缺口。候选生成阶段的 `existing_assets` 只是快照；如果两个 review 同时生成，二者都可能认为记忆不存在，随后分别写入两条相同 active memory。

## 外部实践对照

- [Mem0 的 hash-dedup TOCTOU issue](https://github.com/mem0ai/mem0/issues/6515)明确复现了“先查快照、经过模型/嵌入处理后再插入”导致两个并发调用各自通过旧快照检查的风险，并建议在插入前重新检查。
- [Mem0 当前 Python 写入路径](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)展示了 exact hash 去重和同一批次内去重，但公开 issue 说明快照本身不能承担并发一致性。
- [Mem0 架构说明](https://github.com/mem0ai/mem0/blob/main/skills/mem0/references/architecture.md)也把 exact hash dedup 与语义冲突 resolution 分开；本轮沿用这个边界，不把“相似”直接当成“相同”。

## 发现的缺口

SoulDrive 对同一个 review 的 `decide_review` 已经使用 `BEGIN IMMEDIATE`，但两个不同 review 的接受操作在提交前没有按新事实重新查 active memory。旧实现的最小并发回归产生两条 active memory，且没有警告。

## 最小实现

- 在现有 `decide_review` 的 `BEGIN IMMEDIATE` 事务内，对 memory 做 fresh exact re-check。
- 去重条件严格为：同 subtype、同 scope、同 project_id、标题和内容规范化后完全相同，且旧记忆在新事实的参考时间上有效。
- 命中后不创建第二个 memory/chunk；接受结果返回已有记忆并标记 `deduplicated=true` / `deduplicated_to`。
- 将 `deduplicated_memory_id` 写回加密 reflection candidate payload，保证 review 重放能还原同一 accepted asset，而不会因为候选没有自己的 `source_candidate_id` 被误报为丢失。
- 使用已有 SQLite 写事务串行化同一工作区的提交；没有新增锁服务、向量数据库或 schema fingerprint 列。
- 明确保留 ponytail 上限：这是小型单机工作区的有界本地扫描；只有真实记忆量使其成为可测瓶颈时，才考虑增加 keyed fingerprint/索引。

## 测试证据

- 先写回归测试：旧实现的两个不同 review 同时接受后得到 `2 != 1` 条 active memory。
- 修复后 `tests/test_learning_store.py::test_concurrent_distinct_reviews_deduplicate_exact_memory_at_commit` 通过；同时验证两个 review 的重放结果仍分别为正常写入和 `deduplicated=true`。
- 学习存储、聊天学习 API、读取门和记忆运行时专项：`114 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1065 passed, 1 warning`；Python compileall 通过；前端生产构建通过；7 组前端测试 `48 passed`；Tauri `cargo check --locked` 通过。
- Vite 仍提示单 chunk 约 `576.12 kB`；Starlette/httpx 弃用提示仍是环境警告，不是本轮失败。

## 边界与未完成项

- 这是 exact duplicate 的提交前去重，不是语义相似记忆合并，也不替代 `conflicts_with` 的人工确认 supersession。
- `BEGIN IMMEDIATE` 只覆盖同一 SQLite 工作区；跨进程以外的多主机/多数据库写入不在本机边界内。
- 加密内容不能直接建立明文唯一索引，因此当前采用 bounded local scan；大规模数据需要单独的 HMAC fingerprint 迁移和基准，不在本轮猜测添加。
- 真实 Tauri/WPS、安装包、模型/视觉服务和冻结留出集上的答案质量仍需独立验收。
