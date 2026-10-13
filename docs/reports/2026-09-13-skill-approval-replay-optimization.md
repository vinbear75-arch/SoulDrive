# Skill 预览与审批回放边界优化

日期：2026-09-13

## 发现

Skill 收件箱的适配器预览和显式审批共用同一个编译函数。原实现虽然编译结果可复用，但 `POST /approve` 没有持久的“已准备审核”标记：响应丢失后重试会再次写 `learning.project_skill_adapter_review_ready`。如果把 GET 预览误当成审批，还会让用户看预览就改变审核状态。

## 公开实践对照

- [vercel-labs/skills CLI](https://github.com/vercel-labs/skills/blob/main/src/cli.ts) 将 `add`、`update`、`use` 和从 lock 文件恢复区分开；本轮沿用“动作状态分开、重复动作有明确结果”的思路。
- [AWS CLI Agent Orchestrator 的 Skill 文档](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/skills.md) 对已有 Skill 保持拒绝覆盖并要求显式 remove/re-add；本轮不覆盖待审核原文，只增加本地审核状态。
- [Temporal retry policy](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 将 Activity 重试视为常态；本轮把审批状态写入既有 `install_manifest.json`，使重试可以判断已完成的本地副作用。

## 实现

1. 显式 `POST /learning/skills/inbox/{skill_name}/approve` 首次编译适配器后，在安装清单写入 `review_status=adapter_ready` 和 `reviewed_at`，再次调用返回 `replayed=true`。
2. GET `/adapter` 使用 `mark_review=False`，只生成/读取适配器，不写审核状态；随后用户仍需显式 POST 审批。
3. 审批 API 只在首次写入审核状态时记录 `learning.project_skill_adapter_review_ready`；回放继续返回适配器结果但不重复写审计。
4. 激活仍是独立的下一步，激活后把适配器状态提升为 `verified`；审批 marker 不会自动激活或执行 Skill 脚本。

## 失败优先与验收

- 新增回归覆盖：manifest 审批 marker 的首次/回放、审批 API 审计去重，以及 GET 预览不改变审批状态。
- 安装器、项目 Skill、学习 MCP 路由和完整 Skill E2E 组合：`99 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1283 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- `review_status=adapter_ready` 只代表本地声明式适配器已经准备好供用户审核，不代表 Skill 已激活、脚本可信或来源已签名。
- 没有引入远端供应链验证、自动升级、跨进程 exactly-once 或审核人身份系统；原始 Skill 仍留在本地待审核目录。
- manifest 标记写入和适配器文件写入属于本机两个文件副作用；本轮通过顺序回放保护，不宣称跨文件系统原子提交。
