# 文档归全球化后置审计故障边界优化

日期：2026-09-13

## 发现

文档从项目范围归为全局会先提交 catalog 绑定变化，随后写 `document.globalized` 审计并要求重建派生索引。旧实现中审计 sink 异常会让已经成功解除项目绑定的请求返回 500；UI 只处理 `reindex_required`，完全丢失后置告警。

## 公开实践对照

- [Open WebUI Knowledge Base Sync](https://github.com/open-webui/docs/blob/main/docs/ecosystem/knowledge-base-sync/index.md)：源 manifest 变化、差异预览、上传/清理和进度是可观察的分层步骤，不能把一次接口响应当成全部同步完成。
- [Dapr Transactional Outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/)：业务状态与事件意图是不同可靠性边界；没有 outbox 时也必须诚实区分二者。
- [Restate Durable Execution](https://docs.restate.dev/foundations/key-concepts)：完成的状态步骤在恢复中不应重复执行，后置通知失败不能覆盖已完成主状态。

本轮只借鉴状态转换与通知分层，不引入自动目录同步、云端连接器或 outbox。

## 实现

1. `document.globalized` 改用 `_append_audit_best_effort()`；globalize 主状态提交后，审计失败返回 bounded `warning`。
2. UI 读取 warning，在触发索引重建提示或“已是全局”提示中显示告警；原有项目范围保护、实验证据阻断和 `reindex_required` 行为不变。

## 验收

- globalize/项目接口定向：`2 passed, 1 warning`。
- 前端文档范围静态契约：`24 passed`；生产构建通过。
- 完整 `scripts/check.ps1`：后端 `1293 passed, 1 warning`；前端构建及 8 组测试共 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机项目绑定主状态与后置审计失败的分离，不证明索引重建已经完成或 GraphRAG 派生索引自动一致。
- 审计仍没有事务性 outbox/自动补偿；`warning` 只是明确告警。
- 文档归全球仍要求用户确认，未扩大可检索范围之外的权限。
