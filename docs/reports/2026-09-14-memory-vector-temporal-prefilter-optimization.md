# 记忆 Vector Set 时间范围预过滤优化

日期：2026-09-14

## 外部实践与缺口

对照 [Redis Vector Set filtered search](https://redis.io/docs/latest/develop/data-types/vector-sets/filtered-search/)，`VSIM FILTER` 支持数值比较，`FILTER-EF 0` 用于在过滤条件稀疏时继续寻找足够候选。SoulDrive 的 RediSearch 记忆投影已经把 `valid_from/invalid_at` 推入查询过滤器，但 Redis 8 Vector Set 分支只按项目、会话、workflow 和 active 状态预过滤，时间边界留到 KNN 之后处理。

这会产生一个具体风险：过期或尚未生效的记忆占据近邻候选后，后置时间门把它们丢掉，剩余有效记忆可能不足 `top_k`；这不是串项目泄漏，但会造成历史时点查询的召回损失。

## 实现

- 新写入的 Vector Set metadata 始终带有时间边界；没有显式起止时间时使用宽范围数值哨兵，避免 Redis 把缺失字段视为不匹配。
- `VSIM` 的 Vector Set filter 增加 `.valid_from <= as_of` 与 `.invalid_at > as_of`；未指定 `as_of` 时使用当前时间，与既有后置校验一致。
- 过滤查询返回不足 `top_k` 时，以无过滤查询补读旧版缺少时间字段的 projection，再通过既有权威 scope/time gate 和 memory ID 去重；过滤命令失败时保留原兼容路径。
- 没有改变本地 SQLite 事实源、历史版本、项目/workflow 范围规则，也没有把 Redis 结果当作事实源。

## 回归证据

失败优先用例先证明 Vector Set 查询没有时间过滤；修复后验证：

- `VSIM` filter 包含 `valid_from` 和 `invalid_at` 数值边界；
- 旧 projection 缺少时间字段时，过滤查询后会回退并仍能召回 legacy memory；
- 项目/全局、workflow、RediSearch 时间过滤和 durable memory projection 相关 `tests/test_conversation_store.py`：`81 passed`。
- 学习存储、历史时间读取、运行时记忆、MCP 学习接口和质量评测组合：`313 passed, 1 warning`；该组合之后又加入了记忆 projection replay 范围回归，当前完整工程门禁为后端 `1545 passed, 1 warning`，前端构建成功、UI `62 passed`，`compileall` 与锁定 Cargo 通过。

## 边界

这证明查询边界与旧投影兼容回退，不证明真实 Redis 8/RediSearch/Vector Set 实例联调、FILTER-EF 的实际 p95、向量召回率或答案准确率；没有真实 Redis 的情况下，外部后端回归仍是 fake-client 契约测试。
