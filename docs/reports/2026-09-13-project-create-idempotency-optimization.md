# 项目创建请求幂等优化

日期：2026-09-13

## 发现

项目创建接口原先每次没有显式 `project_id` 都随机生成新的项目身份。若客户端在项目目录已经创建成功后因网络中断没有收到响应，再次提交相同表单会产生第二个项目；已有项目主键只能阻止“同 ID”重复，不能识别同一请求的重试。

## 公开实践对照

- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http) 将客户端幂等键绑定到持久化 invocation，重复请求回放已有结果而不重新执行；本地只借鉴持久化 key 与输入冲突原则。
- [Temporal Workflow ID/Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid) 将稳定业务身份与一次执行身份分离；本轮把项目创建的 `Idempotency-Key` 作为请求身份，把 `project_id` 作为创建结果的持久化业务身份，不把随机 UUID 当作重试依据。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 要求恢复时能够依据持久化状态继续，而不是靠内存标志；项目创建 key 现在保存于本机项目目录 SQLite，不依赖 UI 内存。

## 实现

1. `projects` 表新增可空唯一 `idempotency_key`，旧数据库通过 schema 迁移补列和唯一索引；无 key 的旧调用行为保持不变。
2. `ProjectCatalog.create_project()` 按 key 查询并核对名称、模式、目录、说明和记忆策略；同输入返回原 `project_id`，不同输入返回冲突，不静默合并。
3. `POST /projects` 接受 `Idempotency-Key`，重试返回 `status=replayed`，首次创建返回 `status=created`；重复请求不会重复写 `project.created` 审计事件。
4. UI 项目创建按输入表单生成稳定 key；网络错误时保留 key，用户改变项目输入时生成新 key，成功后清理，避免把下一次不同项目误当作重试。后续竞争修复又让 catalog 显式返回首次/回放状态，并在无显式 `project_id` 时由 key 派生项目身份。

## 失败优先与验收

- 旧实现反例：同 key 同输入的第二次请求返回第二个随机项目，第三次改名也不会冲突。
- 修复后 API 回归覆盖同 key 同输入回放、同 key 异输入 409 和数据库只保留一个项目；项目/目录专项 `45 passed, 1 warning`。
- UI 文档范围专项 `20 passed`，确认实际项目创建入口携带输入绑定 key。
- 完整 `scripts/check.ps1`（本轮竞争修复后）：后端 `1274 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单工作区 SQLite 的资源创建幂等，不是跨机器请求协调或外部 exactly-once。
- 无 key 的旧调用仍可能在网络重试时重复创建；调用方应使用 header。项目目录创建属于普通文件系统副作用，SQLite 回执与目录创建不构成跨存储原子事务。
- 该证据不覆盖真实 Tauri 网络断线注入、安装包冷启动、用户项目恢复演练或跨进程数据库锁竞争的压力测试。
- 竞争结果与派生项目身份的专项边界详见 `docs/reports/2026-09-13-project-create-race-optimization.md`。
