# 文档删除 durable receipt 与幂等回放优化

日期：2026-09-14

## 发现的真实缺口

上一轮已经把“索引清理成功后审计失败”与“业务删除成功”分开，但文档删除仍没有持久化删除回执。客户端在源文件删除完成、HTTP 响应丢失后重试，会收到 `404 document not found`，无法区分“从未删除”和“删除已完成但响应丢失”。更危险的是，同一个显示名被重新导入后，旧请求的重试可能把新文档当成原对象再次删除。

失败优先新增的两个反例在旧实现上分别得到：

- 已删除源文件的同键重试为 `404`；
- 同键重试遇到重新导入的新内容仍执行删除，返回 `200`。

## 公开实践对照

- [DBOS Transact](https://github.com/dbos-inc/dbos-transact-py) 使用稳定事件/工作流身份启动一次 durable workflow，并通过持久 checkpoint 在恢复时复用已完成步骤；本地只借鉴“稳定身份 + 本地回执”的最小原则，没有引入 Postgres 或 DBOS。
- [Restate HTTP invocation](https://docs.restate.dev/services/invocation/http) 将 `Idempotency-Key` 与持久化请求结果绑定，重复请求回放已完成结果而不是重新执行；本地删除仍保留可选 header 兼容旧客户端。
- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle) 要求把已提交副作用、处理中状态和未知结果区分开；本轮因此没有把源文件缺失一律解释成“已完成”。

## 最小实现

1. `ProjectCatalog` 新增不依赖文档外键的 `document_deletion_receipts` 表，保存显示名、源内容 SHA-256、`in_progress/failed/uncertain/completed` 状态、租约和错误指纹。回执不随文档本体删除而消失。
2. `POST /documents/delete` 接受可选 `Idempotency-Key`：
   - 首次请求先以“名称 + 内容摘要”绑定删除身份；
   - 同键并发请求在租约内返回 `409 in_progress`；进程中断后过期租约可被新请求恢复；
   - 已完成且源已不存在时返回 `200`、`replayed=true`，不再次执行索引或文件删除；
   - 已完成回执遇到重新导入的同名文档，或同键内容摘要不同，返回 `409`，不会碰新对象；
   - 源删除后无法提交完成回执时标记/返回 `uncertain`，不伪装成可安全重试的普通失败。
3. 文档库 UI 为一次确认操作生成并保留稳定删除键；网络异常后再次点击复用同一键，成功回放会明确显示“已回放之前的删除结果”。

## 验收证据

- 旧实现失败优先：新增的两个 API 反例 `2 failed`，分别复现 `404` 和误删重新导入文档。
- 实现后：文档 API、项目目录和安全回归 `56 passed, 1 warning`；前端文档契约 `26 passed`；Python `compileall` 通过。
- 完整 `scripts/check.ps1`：后端 `1433 passed, 1 warning`；前端生产构建通过，8 组脚本合计 `62 passed`；锁定 Cargo 检查通过。既有警告为 Starlette/httpx 弃用提示和 Vite 大 bundle 提示。

## 边界

- 这是本机 SQLite 回执、租约和内容摘要边界，不是跨存储原子删除，也不是文件系统或外部服务 exactly-once。
- 未提供 `Idempotency-Key` 的旧 API 调用保持兼容，但不获得删除回放保证；当前 UI 已始终携带该键。
- `uncertain` 只说明删除与回执提交之间存在不可判定窗口，不自动猜测外部状态、不自动重新导入、不引入回收站；真实 Tauri/UI 点击、安装包冷启动和长期故障演练仍需独立验收。
