# 文档删除后置审计故障边界优化

日期：2026-09-13

## 发现

文档删除在索引清理成功后删除加密文档对象或普通源文件，随后才写 `document.deleted` 审计。旧实现中审计异常会直接让接口返回 500；源文件和索引实际上已经删除，UI 也会把完成的不可逆操作显示成失败。

## 公开实践对照

- [Restate Durable Execution key concepts](https://docs.restate.dev/foundations/key-concepts)：已完成的副作用步骤在重试中应跳过，后续步骤失败不能重做前置副作用。
- [Restate Request Lifecycle](https://docs.restate.dev/guides/request-lifecycle)：请求完成/恢复需要区分已提交步骤与尚未完成步骤。
- [Nextcloud Trashbin API](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/trashbin.html)：删除是明确的状态转换，客户端需要知道删除操作是否已完成；本机版本仍保持用户确认和永久删除边界。

本轮只借鉴“删除主状态与后置审计分离、状态对客户端可见”的原则，没有引入回收站或改变本机删除策略。

## 实现

1. 文档源与派生索引清理完成后，`document.deleted` 改用 `_append_audit_best_effort()`。
2. 删除成功仍返回 `deleted=true`；审计 sink 失败时附带 bounded `warning`，不把已完成删除伪装成 500。
3. UI 读取并显示 warning，继续刷新文档列表；索引清理异常仍在删除源文件前返回 500，保持原有保护。

## 验收

- 文档服务专项：`12 passed, 1 warning`。
- 前端文档范围静态契约：`22 passed`；生产构建通过。
- 完整 `scripts/check.ps1`：后端 `1291 passed, 1 warning`；前端构建及 8 组测试共 `58 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机删除主步骤完成后审计故障不会误报业务失败，不证明跨存储原子删除、跨进程 exactly-once 或真实 UI/E2E。
- 当前文档删除仍按用户确认和文档名/同名保护执行；响应丢失后的删除回放 receipt 尚未新增，重试可能得到“文档不存在”，不应据此自动重新导入或猜测对象。
- 审计 sink 没有 outbox/自动补偿；warning 仍需人工处理。
