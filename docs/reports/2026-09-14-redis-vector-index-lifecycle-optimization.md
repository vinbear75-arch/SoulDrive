# Redis 向量索引初始化竞态与维度校验优化

日期：2026-09-14

## 结论

本轮收紧了 Redis 向量索引启动契约的两个缺口：

1. 多个进程同时初始化 RediSearch 时，一个进程在 `FT.INFO` 后、`FT.CREATE` 前被另一个进程抢先创建索引；后来的进程收到 `Index already exists` 后，过去会直接尝试 Vector Set，在 Redis 7/仅 RediSearch 部署中误报向量索引不可用。
2. 已存在的 RediSearch 索引过去只要 `FT.INFO` 成功就被标记为 ready，没有读取 `VECTOR` 字段的 `DIM`；embedding 维度改变时，错误配置会延迟到写入或查询阶段才暴露。

现在 `FT.CREATE` 异常后先重新 `FT.INFO`，并兼容 RESP2/RESP3 结构读取 `VECTOR DIM`。已知维度不匹配时 fail-closed；无法从旧版本返回值解析维度时保留兼容行为，但不伪造额外校验结论。

## 外部实践对齐

- [Redis FT.CREATE](https://redis.io/docs/latest/commands/ft.create/) 明确规定索引已存在时返回错误，因此客户端初始化必须把“被其他进程创建”与“创建失败”分开处理。
- [Redis FT.INFO](https://redis.io/docs/latest/commands/ft.info/) 将 `attributes` 定义为索引 schema 字段及类型信息，向量字段的维度属于索引配置而非请求参数。
- [Redis vector search concepts](https://redis.io/docs/latest/develop/ai/search-and-query/vectors/) 要求向量字段携带固定 `DIM`；本项目因此在派生投影写入前拒绝已知维度不兼容，而不引入新的向量数据库。

## 实现边界

1. RediSearch 已存在索引的 dimension 读取兼容 RESP2 键值列表与 RESP3 map/list 结构。
2. 竞态重检只认 `FT.INFO` 的成功结果；重检仍失败才尝试 Redis 8 Vector Set。
3. 只改启动探测与 fail-closed 诊断，不自动删除/重建已有索引，避免破坏旧投影；真实迁移仍需明确的索引重建流程。

## 回归证据

- 失败优先：并发创建场景修复前返回不可用；维度不匹配场景修复前错误返回 ready。
- Redis 向量定向回归：`11 passed`。
- 完整工程门禁：后端 `1515 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；`compileall` 与锁定 Cargo 通过。本机没有真实 Redis/RediSearch 实例，因此仍需在可控集成环境验证真实 RESP 版本、索引 schema 和故障转移行为。
