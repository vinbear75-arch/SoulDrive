# 会话摘要版本冲突与投影幂等优化

日期：2026-09-14

## 发现

会话摘要以 `session_id + version` 作为本地回放身份。旧实现遇到同一版本再次写入时直接返回或覆盖，没有验证重试携带的摘要文本、覆盖区间和来源消息是否仍是同一份输入；Redis 与 PostgreSQL 投影的版本判断也不完全一致。进程重启、旧 outbox 延迟回放或并发压缩因此可能把不同摘要静默当作合法重试。

## 公开实践对照

- [LangGraph persistence/checkpointer](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)：checkpoint 由线程、命名空间和 checkpoint identity 组成，更新应创建新的 checkpoint，而不是静默改写既有历史。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：幂等键用于把同一次请求的重试合并为同一结果；输入身份变化不能被当作同一重试。
- [LangGraph durability issue](https://github.com/langchain-ai/langgraph/issues/8234)：持久化顺序和恢复一致性必须作为显式契约测试，不能只依赖“最终能读到一条记录”。

本轮只复用“稳定身份绑定输入、冲突显式失败”的原则，没有引入 LangGraph、Restate 或新的数据库运行时。

## 实现

- 在本地加密摘要事实源为摘要版本计算稳定 `identity_digest`，覆盖会话、版本、摘要文本、覆盖序号、来源消息、输入窗口和 excerpt 元数据；同版本同摘要回放，不同 payload 返回 `summary version conflict`。
- Redis 热投影对同版本执行相同 identity 校验；旧版本仍保持不回退，新版本正常写入。
- PostgreSQL 投影先锁定同版本记录；新记录以 `ON CONFLICT DO NOTHING` 写入，竞争冲突后重新读取并比对 `summary_hash` 与新 metadata 中的 identity hash，禁止静默覆盖。旧行没有 identity hash 时仍保留内容 hash 兼容路径。
- 现有完整消息和摘要文件仍是本机事实源；Redis/PostgreSQL 只是可重建投影，投影失败不会回写或改变已提交的本地摘要。

## 验收

- 失败优先新增三条反例：本地同版本改摘要、Redis 同版本改摘要、PostgreSQL 同版本改摘要；修复前均放行，修复后均拒绝且保留第一次结果。
- 会话、Redis/PostgreSQL 投影、运行时审计与 TaskJournal：`91 passed`。
- 单独冲突回归：`3 passed`。
- 本轮改动后的完整工程门禁已通过：后端 `1501 passed, 1 warning`；前端生产构建及 8 组脚本测试通过；`compileall` 与 `cargo check --locked` 通过。当前已知的既有边界仍包括真实 Redis/PostgreSQL 实例级联调、跨进程 exactly-once 和完整 Tauri/WPS UI/E2E。

## 边界

这保证的是同一摘要版本在本机事实源和可选派生投影中的输入一致性，不证明模型生成摘要的事实准确率，也不把本地多存储写入宣称为跨存储原子事务。
