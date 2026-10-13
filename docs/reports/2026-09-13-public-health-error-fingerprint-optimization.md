# 2026-09-13 公共健康检查错误指纹优化

## 结论

`/health` 属于免令牌可用性端点。此前 `_storage_health()` 在存储后端异常时返回原始 `error` 文本，可能暴露本地路径、Redis/数据库连接串或供应商回显。本轮改为返回 `error_type`、`error_chars` 和 16 位 `error_digest`，保留“存储不可用/降级”的状态判断，不向公共状态面暴露异常正文。

## 失败优先反例

新增 `tests/test_mcp_server_security.py::McpServerSecurityTests::test_storage_health_does_not_expose_raw_backend_exception`：让工作区发现抛出包含 Redis 用户、密码和私有主机的异常；修复后结果仍为 `status=error`、`degraded=true`，但不含原始连接串，且错误类型和固定长度摘要存在。

## 实现范围

- `core/mcp_server.py::_storage_health()` 复用通用 `_error_observation()`。
- 不改变健康检查 HTTP 路由、锁定/降级状态、存储恢复机制或鉴权边界。

## 验证证据

- 健康检查失败优先回归：`1 passed, 1 warning`。
- 修改后的完整 `scripts/check.ps1`：后端 `1356 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。
- Vite 仍提示既有单个 JS chunk 超过 500 kB。

## 边界

- 该状态面只用于可用性判断；需要详细原因时仍应查看受保护的本机诊断/日志，而不是扩大公共健康响应。
- 本轮不声称全部 API 错误都完成统一脱敏、不声称远程 observability backend、真实 Tauri/WPS UI/E2E 或外部 exactly-once 已完成。
