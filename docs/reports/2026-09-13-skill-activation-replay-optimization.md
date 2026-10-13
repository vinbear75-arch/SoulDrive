# Skill 激活移动后的 durable 回放优化

日期：2026-09-13

## 发现

Skill 激活是一个本地文件系统副作用：审核通过后，系统先写入已验证的声明式适配器，再把 `.inbox/<skill_name>` 移到活动目录。若移动已经成功但 HTTP 响应在客户端不可见，用户重试时原实现只看到 `.inbox` 不存在，会把已完成激活误报成失败；路由还会再次写激活审计（或在模拟层重复传播该事实）。

## 公开实践对照

- [vercel-labs/skills CLI](https://github.com/vercel-labs/skills/blob/main/src/cli.ts) 将安装、更新和激活/绑定区分为不同动作，并对已有 Skill 保持明确状态，而不是静默覆盖。
- [Open WebUI 的 durable Knowledge linking RFC](https://github.com/open-webui/open-webui/discussions/24871) 强调由后端持久化逻辑链接意图，并让并发/重试调用复用同一共享操作；本轮把这个原则缩小到本机“已移动的 Skill 目录”。
- [Temporal retry policy](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 将 Activity 重试视为常态，副作用需要由 Activity 自身保持幂等；本轮不引入 Temporal，只把文件移动后的回放状态持久化在现有安装清单和已验证适配器中。

## 实现

1. `activate_skill_from_inbox()` 在 `.inbox` 不存在时检查活动目录是否同时满足本系统安装清单、同名身份和 `compatibility_status=verified` 的适配器条件；满足时返回 `status=active, replayed=true`。
2. 首次激活返回 `replayed=false`；同名但不具备本系统激活凭据的活动目录仍拒绝认领，避免把用户手工目录误当成回放结果。
3. `/learning/skills/inbox/{skill_name}/activate` 只在首次移动时写 `learning.project_skill_activated`，回放仍返回当前 Skill 列表，但不重复写激活审计。
4. 文件移动遇到同名竞态时增加同样的已验证活动目录判定；本轮未宣称跨进程压力下的原子 exactly-once。

## 失败优先与验收

- 新增回归覆盖：移动成功后第二次直接调用不再要求 `.inbox` 存在；API 连续激活返回稳定活动结果且激活审计只出现一次。
- 安装器、项目 Skill、学习 MCP 路由和完整 Skill E2E 组合：`96 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1280 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单工作区的“移动完成后回放”保护，不是远程发布、自动升级或多副本同步协议。
- 活动目录必须有本系统安装清单和已验证适配器才会被认领；旧版本/手工目录仍需要人工处理。
- 未证明跨进程高并发目录移动、跨机器 exactly-once、供应链签名或真实 Tauri/UI/E2E；当前 full check 仍有既有 Vite chunk 大小警告（581.69 kB）。
