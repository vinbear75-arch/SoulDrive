# 会话消息写入项目边界优化

日期：2026-09-14

## 发现

HTTP/MCP 入口已经校验会话与项目是否一致，但 `ProductionSessionStore.append_message()` 这个更底层的 durable 写入口仍接受显式的错误 `project_id`。项目 A 的持久会话可以被调用方带着项目 B 写入；全局会话也可以被临时升级到项目范围。若可选 PostgreSQL 投影启用，后者还可能把派生会话目录写成错误项目。

## 公开实践对照

- [LangGraph checkpoint persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)：持久状态以稳定 thread/checkpoint identity 读取，调用配置不能把另一条线程的状态当作当前线程。
- [LangGraph checkpoint base](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/base/__init__.py)：`thread_id` 是保存和恢复状态的主键，状态身份不能只依赖调用方临时参数。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：重试身份与调用目标绑定，输入身份变化不能静默复用已有结果。

本轮只把已存在的 session manifest 作为写入边界，没有新增账号权限系统或远程多租户模型。

## 实现

- 写入前读取 session manifest；若会话已绑定项目，显式 `project_id` 必须与绑定值一致，否则返回 `session belongs to another project`。
- 全局会话拒绝通过单次消息写入临时升级到项目范围，返回 `global session cannot be assigned to a project`。
- PostgreSQL 投影使用经过校验的有效项目身份；会话绑定项目而调用方省略项目时沿用 manifest，避免投影被临时参数覆盖。
- 没有 manifest 的历史兼容路径保持原行为；聊天主链路仍要求持久 session，后续可单独清理无 manifest 遗留数据。

## 验收

- 失败优先新增两个反例：项目 A 会话带项目 B 写入、全局会话带项目 A 写入；修复前均成功写入，修复后均拒绝且会话没有新增消息。
- 会话与学习 MCP 组合：`184 passed, 1 warning`。
- 本轮完整工程门禁已通过：后端 `1501 passed, 1 warning`；前端生产构建及 8 组脚本测试通过；`compileall` 与 `cargo check --locked` 通过。既有边界包括未连接的真实 PostgreSQL/Redis 实例联调、跨进程 exactly-once 和真实 Tauri/WPS UI/E2E。

## 边界

该修复保证的是本机持久会话 manifest 与消息写入/project projection 的范围一致性，不替代认证授权，也不证明外部存储事务或模型答案准确率。
