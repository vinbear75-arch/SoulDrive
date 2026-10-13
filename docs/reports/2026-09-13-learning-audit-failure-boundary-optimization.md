# 学习动作主状态与审计旁路故障边界优化

日期：2026-09-13

## 发现

Skill 安装、同步、审批、激活和运行计划都先写本地状态，再写审计。原实现中，审计 sink 的异常会沿 HTTP 路由冒泡：本地动作实际已经完成，但客户端收到 5xx，重试后容易重复动作或误以为状态未保存。Skill 删除接口已有“删除完成后审计失败只返回 warning”的边界，其他学习动作没有统一遵循。

## 公开实践对照

- [Inngest 错误处理与重试](https://www.inngest.com/docs/guides/error-handling) 把成功 step 的结果持久化并从失败 step 继续，要求重试代码保持幂等；本轮将审计视为完成本地业务状态后的旁路。
- [Sledge](https://github.com/torkbot/sledge) 将事件结果与事件持久化，并通过 durable dedupe 复用原结果；SoulDrive 本轮没有引入事件引擎，只沿用现有审计记录接口并把主结果先返回。
- [Inngest 幂等处理](https://www.inngest.com/docs/guides/handling-idempotency) 强调稳定业务状态和重复调用不产生不一致副作用；warning 让调用方知道审计缺口，同时不把已完成动作错误重试。

## 实现

1. 新增 `_append_audit_best_effort()`：审计写入失败记录服务端日志并生成 bounded warning，不抛出覆盖主操作结果。
2. `/learning/skills/install`、`/learning/skills/sync`、`/learning/skills/inbox/{name}/approve`、`/learning/skills/inbox/{name}/activate` 使用该边界；回放请求仍不写重复创建审计。
3. Skill 计划和显式执行的本地运行记录先落库，审计失败时返回已保存的 run 状态和 warning。
4. 真实业务错误仍保持原错误码；best-effort 只包住审计调用，不包住下载、文件移动、数据库写入或脚本策略判断。

## 失败优先与验收

- 新增 5 个回归：install、sync、approve、activate、plan 的审计 sink 抛错时，主请求仍返回 200、warning 和已完成/已保存状态。
- 安装器、项目 Skill、学习 MCP 路由和完整 Skill E2E 组合：`104 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1288 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- warning 不代表审计事件已补偿；当前没有为 Skill 审计建立独立 durable outbox，因此审计 sink 长时间不可用时需要通过后续诊断/人工处理发现缺口。
- 本轮不改变只读查询的审计策略、不引入分布式日志、不证明跨进程 exactly-once 或跨文件原子提交。
- 既有 Vite 生产构建提示主 JS chunk 581.69 kB 超过 500 kB；该提示与本轮后端学习路由改动无关。
