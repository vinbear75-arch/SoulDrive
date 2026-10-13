# 未绑定工作流记忆的 Doctor 诊断优化

日期：2026-09-13

## 问题

上一轮已让未绑定的 `workflow_fact` 在运行时 context-only 读取中 fail-closed，但如果没有诊断入口，事实会变成“安全地不可见、却没人知道为什么”。这不适合本机单用户项目的长期维护和人工审核。

## 公开实践对照

- [Letta memory architecture](https://github.com/letta-ai/skills/blob/main/letta/letta-api-client/memory-architecture.md) 把始终注入的 core memory 与按需检索的 archival memory 分开；本地 doctor 只检查长期记忆元数据，不把被隔离的正文重新输出到诊断响应。
- [Graphiti project configuration/group_id](https://github.com/getzep/graphiti/pull/1209) 强调项目配置是 group_id 的权威来源，并要求写入与读取使用同一来源；本地把缺少 workflow 归属视为需要人工处理的范围不完整，而不是自动猜测。
- [Temporal Workflow ID/Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid) 区分稳定业务 Workflow ID 与可随重试变化的 Run ID；本地诊断只报告 `workflow_fact` 的缺失绑定，不把一次重试/运行标识当成记忆归属。

## 实现

`LearningStore.memory_health()` 现在新增两类只读问题：

- `workflow_memory_unbound`：活动 `memories` 中的 `workflow_fact` 没有 `workflow_id`。
- `workflow_memory_version_unbound`：活动 `memory_versions` 中的 `workflow_fact` 没有 `workflow_id`。

问题只包含 memory/version ID、subtype、范围和 project ID，不包含解密后的标题或正文；既有 project/global 过滤、问题数量上限、敏感内容和指令型内容诊断保持不变。doctor 不自动迁移、不猜测窗口、不修改存储。

## 失败优先与修复过程

先加入回归并按旧实现运行，因没有 `workflow_memory_unbound` 问题而失败；实现活动记录后再扩展历史版本分支，测试暴露出版本查询遗漏 `subtype`，补上查询字段后通过。这一过程证明诊断覆盖了活动 head 和历史版本，而不是只为测试造出一个固定结果。

## 验收

- LearningStore 全量：`71 passed`。
- 完整 `scripts/check.ps1`：后端 `1211 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。
- 警告仍为 Starlette/httpx 弃用提示；前端 chunk 大小提示仍是既有构建建议。

## 边界

本轮只补可观测性，不提供自动归属、批量迁移、跨进程 exactly-once、记忆抽取质量或答案准确率结论。后续若要迁移存量事实，必须由用户或明确的人工审核记录确认目标工作流，再走现有幂等写入和 outbox 链路。

