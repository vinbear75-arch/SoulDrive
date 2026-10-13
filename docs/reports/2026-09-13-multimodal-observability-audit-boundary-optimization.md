# 多模态链路审计观测故障边界优化

日期：2026-09-13

## 对照实践

- [Open WebUI audit implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/audit.py) 把审计失败隔离在观测路径，不让它覆盖请求处理结果。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将事件、durable work 和结果状态分开，提醒副作用记录不能成为主处理结果的唯一依据。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 暴露结构化错误响应；其 [silent background ingestion issue](https://github.com/getzep/graphiti/issues/1707) 也说明“后台失败但前台无信号”必须被显式识别。

## 失败优先发现

多模态决策、工作流规划、Playbook 审核、ASR/OCR 预热、VLM 预热、同步推理和上传推理在主结果计算后直接写审计。审计 sink 故障会把澄清决策或本地模型预热误报成 HTTP 500；这会干扰用户判断，也可能诱导重复触发模型加载或视觉推理。

## 本轮修改

- 多模态所有观测事件统一使用 `_append_audit_best_effort`。
- 审计失败不改变决策、工作流 plan、模型 warmup 或 inference 的业务结果；真实模型/输入/范围错误仍保持原错误码和 fail-closed 行为。
- `_append_audit_best_effort` 保留结构化 payload；聊天链路额外保留 `trace_id`。

## 验收证据

- 新增多模态决策审计故障回归和视觉预热审计故障回归；多模态/学习/工作流/Topic5 组合 `170 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1314 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是 ASR/OCR/VLM 质量提升，也不证明 GPU 冷启动、显存调度或模型推理稳定性。
- 这不是多模态事件 outbox 或自动补偿；审计失败仍可能丢失，只是不再伪造业务失败。
- 不扩大屏幕采集、摄像头、U 盘或云端范围；完整工程门不等于真实 Tauri/UI/E2E。
