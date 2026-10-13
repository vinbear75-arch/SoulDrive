# 聊天主链路审计观测故障边界优化

日期：2026-09-13

## 对照实践

- [Open WebUI audit implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/audit.py) 将审计写入放在独立的异常边界中，日志失败不覆盖已完成的请求处理。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将 durable work/event 结果与观测流分离，并要求重试使用稳定身份。
- [Restate durable execution](https://docs.restate.dev/quickstart) 强调已成功步骤在后续重试中不应被倒写成失败；[Graphiti issue #1707](https://github.com/getzep/graphiti/issues/1707) 则展示了“调用方收到成功但后台失败未反馈”的另一种风险。

## 失败优先发现

聊天入口的 `chat.requested` 审计写入发生在返回流式响应之前，审计 sink 故障会阻止请求进入 SSE。工作区锁定时，`require_software_unlock()` 还会直接记录 `security.password_required`；该审计异常会覆盖本应返回给用户的明确锁定状态。

## 本轮修改

- `_append_audit_best_effort` 支持可选 `trace_id`，保留聊天链路的追踪关联。
- `chat.requested`、实验/工作流阻断、锁定拒绝、幂等回放、失败和完成事件统一使用 best-effort 审计。
- `require_software_unlock()` 的拒绝审计也改为 best-effort；锁定判断本身保持 fail-closed，不改变授权条件。
- 审计故障只记录 bounded warning/log，不把聊天流、锁定提示或真实业务错误改写成观测层错误。

## 验收证据

- 新增失败优先回归覆盖：审计失败时聊天仍返回 SSE、锁定流仍返回明确锁定提示；与启动 outbox 回放边界合并定向 `3 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1312 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是聊天传输层的断线重连或模型推理成功率保证；RAG/模型真实错误仍进入既有失败路径。
- 审计日志仍可能丢失，本轮没有建设审计 outbox、自动补偿或跨进程 exactly-once。
- 完整工程门不等于真实 Tauri/UI 点击、SSE 网络中断或真实模型/外部服务 E2E。
