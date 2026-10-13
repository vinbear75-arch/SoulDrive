# 项目删除耐久执行优化记录

日期：2026-09-09  
范围：项目索引、会话、原始项目目录和项目目录记录的删除链路

## 结论

本轮发现一个比普通接口重试更严重的 durable execution 缺口：项目删除会依次清理派生索引、会话、原始目录和 SQLite 项目记录，但没有持久化删除阶段。若进程在项目记录删除后、原始目录删除前退出，下一次请求只能看到 `unknown project`，既无法继续清理，也无法准确判断上次操作是否已经完成。

现在项目目录数据库新增不依赖 `projects` 外键的 `project_deletions` 回执表：

- 删除开始时先保存项目快照和原始目录路径；
- 索引、会话和原始目录清理可以在失败后重试；
- 原始目录先清理，项目目录记录最后删除；
- 完成回执保留在 tombstone 中，重复 DELETE 返回同一完成结果并标记 `idempotent_replay`；
- 已完成删除的 `project_id` 不允许复用，避免旧回执误操作后来创建的同名项目。

## 外部实践对照

- [Temporal Durable Execution](https://docs.temporal.io/)把可恢复工作流的状态持久化，并在失败后从最近状态继续；[Activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx)说明丢失或超时的活动会按重试策略重新执行，因此每个阶段必须可重试或具备业务幂等语义。
- [Temporal retry policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)强调重试应围绕具体失败点，而不是把整个多步骤业务盲目从头执行。SoulDrive 没有引入 Temporal，而是用本机 SQLite 回执记录删除边界。
- [SQLite Atomic Commit](https://www.sqlite.org/atomiccommit.html)说明事务可以保证单个数据库提交在崩溃后回滚或完整生效，但它不能把 SQLite 事务自动扩展到向量索引、会话库和文件系统；因此本轮把跨存储操作拆成可重放阶段，并让目录记录最后提交。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)强调重试需要稳定幂等键和完成结果留存；本地实现使用稳定的 `project_id` 删除意图和持久 tombstone，不声称跨主机 exactly-once。
- [Open WebUI 文件访问控制](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/access_control/files.py)的删除/写权限会沿文件所属资源重新校验；本轮保持项目删除的现有用户确认边界，不把“已经拿到项目 ID”误当成跨阶段完成凭据。

## 实现

- `core/project_catalog.py`
  - schema 从 8 升到 9，新增不依赖项目外键的 `project_deletions` 表。
  - 增加删除快照、开始/恢复、失败记录和完成回执方法；回执保留快照、结果和错误信息。
  - 已完成删除的项目 ID 保留为 tombstone，不允许再次创建同 ID 项目。
- `core/mcp_server.py`
  - 删除入口先创建/恢复回执；已完成回执直接重放。
  - 将原始项目目录清理移到项目目录记录删除之前；跨存储步骤失败时保留项目记录和可重试回执。
  - 若进程在项目记录删除后、完成回执写入前退出，下一次请求使用快照识别“项目记录已完成”，继续收尾并写入完成回执。
- `tests/test_mcp_server_projects.py`
  - 新增“第一次原始目录清理失败、第二次重试成功、第三次 DELETE 重放完成结果”回归。
  - 保留已有索引失败回归，确认失败时项目目录记录和原始目录仍保留。
- `tests/test_project_catalog.py`
  - 校验新表和 schema 版本迁移；校验完成删除回执锁定项目 ID。

## 验证

- 项目目录和项目 API 定向回归：`35 passed, 1 warning`。
- 删除专项：首次失败返回 `500` 且保留项目，重试返回 `200 deleted`，再次请求返回 `200` 且 `idempotent_replay=true`。
- 仓库级 `scripts/check.ps1`：后端 `1049 passed, 1 warning`；前端生产构建通过，7 组测试共 `46 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单进程/单工作区的 durable receipt，不是跨主机分布式工作流，也不提供外部文件系统的原子事务。
- 索引删除、会话删除和目录删除仍要求各自操作可重放；回执只记录阶段意图和结果，不伪造跨存储 exactly-once。
- 项目删除仍是不可逆操作，真实 UI 的确认对话框和本地解锁门保持不变；本轮没有扩大删除授权范围。
