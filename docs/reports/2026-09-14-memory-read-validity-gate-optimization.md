# 2026-09-14 记忆读取时间有效性闸门优化

日期：2026-09-14  
范围：`gate_memory_context()`、点时记忆读取、项目/工作流记忆安全边界。

## 1. 本轮发现的真实缺口

`LearningStore.search()` 和 `list_memories()` 已经按 `valid_from/invalid_at` 做点时过滤，但最终把候选交给上下文的 `gate_memory_context()` 没有重复验证时间区间。只要未来新增一个旁路调用、替换检索器，或上游漏传过滤条件，过期/尚未生效的记忆就可能进入 prompt。

这不是存储查询失效，而是读取闸门缺少 fail-closed 的二次边界。对于 SoulDrive 的历史实验比较和三个研究窗口，单靠调用方自觉传 `as_of` 不够。

## 2. 对照的开源机制

- [Graphiti EntityEdge](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py) 将 `valid_at`、`invalid_at`、`reference_time` 和 `expired_at` 分开保存；事实是否在时间点可见不是排序提示，而是事实状态的一部分。
- [Graphiti edge resolution](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 在边合并/失效时校验时间区间，不能把非法或冲突区间当作普通候选继续使用。
- [Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 将 metadata/date filters 与语义搜索组合；[Mem0 score details](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 也把检索判定拆成可解释的过滤和评分结果。

本地只借鉴“时间有效性是读取契约、且最终读取层仍应可诊断”的边界，没有引入 Graphiti/Mem0 运行时或新的存储依赖。

## 3. 最小实现

`gate_memory_context()` 现在在计算词项相关性前统一执行：

1. 参考时间为显式 `reference_timestamp`；普通当前读取使用一次性捕获的当前时间，避免同一批候选跨秒产生不一致判断。
2. `valid_from > reference_time` 返回 `memory_not_yet_valid`。
3. `invalid_at <= reference_time` 返回 `memory_expired`，与 SQLite 查询原有的半开区间语义一致。
4. 非数字、非有限值或 `invalid_at <= valid_from` 返回 `invalid_memory_validity`，不猜测修复。
5. 这些拒绝原因进入既有 `memory_read_gate_v1` 的稳定 decision digest；诊断只返回 ID 和原因，不把记忆正文写入 gate。

## 4. 失败优先与验证

先加入“未来记忆 + 已过期记忆”反例，在旧实现上得到失败：未来记忆仍被 `gate_memory_context()` 接受。实现后补充损坏时间字段反例。

- 失败优先反例：旧实现 `1 failed`，确认问题来自读闸门而不是测试期望。
- 当前 `tests/test_memory_runtime.py`：`14 passed`。
- 记忆、学习存储、会话与 MCP 学习接口组合回归：`269 passed, 1 warning`（在最后一个 malformed validity 用例加入前）；新增用例随后由上述 14 项专项覆盖。
- warning 仍为 Starlette/httpx 测试客户端弃用提示，不是本轮新增。

截至本轮最新完整工程门：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。

## 5. 明确边界

- 这证明候选进入 prompt 前的确定性时间边界，不证明自动记忆抽取质量、时间表达式解析质量或模型答案准确率。
- `valid_from/invalid_at` 仍由现有人工审核、版本更新和存储校验产生；本轮不自动推断事实生效时间。
- 这不是 Graphiti 的双时间图，也不是跨存储 exactly-once；向量、关键词和图谱仍是可重建派生索引。
