# 记忆 outbox 项目范围 claim 优化

日期：2026-09-14

## 结论

本轮修复了记忆管理与问答入口的派生同步范围旁路：`/learning/memories?project_id=A` 和项目上下文在查询前会 replay memory outbox，但原实现直接 claim 工作区内最早的所有事件，因此 A 的读取请求可能替 B 项目租约并执行向量/文件投影。返回数据仍有项目过滤，但副作用没有跟随项目边界。

现在，项目查询将 `scope_project_id` 与 `include_global` 传入 claim；SQLite 在同一写锁内解密事件 payload、校验 global/project 范围后才写入 claim/lease。范围外事件既不 claim，也不因该请求被标记暂停；未指定范围的启动 replay 仍保持工作区级恢复语义。扫描不设任意事件数上限，避免大量其他项目积压导致匹配项目事件饥饿。

## 外部实践对齐

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 以 `group_id` 组织 episode，并按 group 串行处理，说明记忆的来源/作用域应在执行边界前确定；本项目用现有加密 payload 的 project/global 元数据完成轻量本地分区。
- [Mem0 memory operations](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx) 将用户/agent/run identifiers 同时用于写入和后续搜索作用域；本项目沿用“写入/派生同步前先绑定作用域”的原则，不引入外部记忆服务。
- 本轮没有把 outbox 误升级成跨机器队列：仍是单机 SQLite durable outbox + 本地投影，真实 Redis/PostgreSQL 由各自 backend 处理。

## 实现边界

1. 过滤发生在 claim 之前，解决的是项目范围副作用；不会改变 memory source of truth、记忆内容或检索排序。
2. 全局记忆在项目策略允许时与该项目事件一起 replay；`project_only` 策略不 claim global 事件。
3. 未指定项目的 workspace startup replay 继续处理全局待同步事件，旧调用方兼容；不宣称跨机器 exactly-once、真实 Redis/PostgreSQL 联调或性能提升。

## 回归证据

- 失败优先：两个项目各有 pending sync 时，项目 A 的 scoped claim 修复前会同时租约 B；修复后只 claim A，B 保持无 `claim_token`。
- 管理入口回归：`GET /learning/memories?project_id=project-a` 只触发 A 的投影，B 事件保持 pending。
- 同步/暂停/租约/Chat 上下文定向回归：`17 passed, 1 warning`；新增管理入口回归 `1 passed, 1 warning`。
- 完整工程门禁：后端 `1520 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。前端仍有既有单 chunk 大小警告。
