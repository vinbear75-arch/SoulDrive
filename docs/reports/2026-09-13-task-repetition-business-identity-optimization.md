# TaskJournal 业务身份与执行身份分离优化

日期：2026-09-13

## 发现

上一轮的重复失败守卫只按完整任务输入摘要匹配。PPT 计划把 `request_id`、`task_id` 和产物标识保存在任务输入中，因此用户仅因网络重试或重新打开流程而换了这些执行标识时，同一份项目证据可能无法触发既有失败提醒。

## 公开实践对照

- [Temporal Workflow ID 与 Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid) 将具有业务含义的 Workflow ID 与每次执行变化的 Run ID 分开；文档明确不应依赖当前 Run ID 做逻辑判断。
- 本地只借鉴身份分层：幂等键继续绑定完整执行输入，避免错误复用；重复失败提醒另存一个只含业务输入的摘要，仅由 PPT 调用方显式提供，不对所有任务擅自剥离字段。

## 实现

1. `runtime_tasks` 增加 `repetition_digest` 与 `repetition_basis`，旧 SQLite 表自动迁移；完整 `input_digest` 仍负责幂等键冲突校验。
2. `TaskJournal.create_task(..., repetition_inputs=...)` 对可重试业务输入生成持久化摘要；`get_task()` 和 `public_task()` 使用持久化摘要构建重复失败守卫，跨关闭/重开仍可告警。
3. PPT 计划投影项目、来源、版本、标题、受众、页数、WPS 开关和 Skill 信息，排除 request/task/artifact 等执行标识；PPT 生成阶段再绑定已确认的 `plan_digest`。
4. 对无新摘要的历史任务，仅在 `full_inputs` 模式下回退旧 `input_digest`；业务投影不会把旧任务猜成同一业务，继续保持 fail-closed。

## 失败优先与验收

- 旧实现先因 `TaskJournal.create_task()` 不接受 `repetition_inputs` 而失败；接入摘要后又由 SQL 查询仍查旧 `input_digest` 的反例暴露，修正为 `repetition_digest` 后通过。
- TaskJournal/PPT 定向回归 `46 passed`，覆盖换 request/task/artifact 标识、跨重启、不同来源放行、旧任务摘要回退和不泄露失败正文。
- 完整 `scripts/check.ps1` 通过：后端 `1249 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 业务投影是 PPT 的显式白名单，不是通用语义重复检测；未来新增业务字段若未加入投影，仍可能漏报或因保守策略产生不同摘要。
- `warn` 只提醒，不阻断创建、不自动复用旧任务、不自动重试，也不证明下一次执行一定成功。
- 这项改动不提供跨进程/跨机器 exactly-once；真实邮件、日历、WPS 等外部副作用仍须各自的确认、幂等键和不确定状态处理。
