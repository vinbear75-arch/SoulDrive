# durable task 重复失败守卫优化

日期：2026-09-13

## 发现

TaskJournal 已保存任务输入摘要、失败状态、step 尝试历史和幂等键，但调用方在更换幂等键后重新提交完全相同的任务时，只能再次执行，无法看到“这个规范化输入以前失败过”。这会让 durable execution 只解决“同一次请求的重放”，却不能帮助用户避免重复走同一条失败路径。

## 公开实践对照

- [projectmem](https://github.com/riponcm/projectmem) 把 issue、attempt、fix、decision 等事件用于 Memory-as-Governance，并在动作提交前提示 agent 不要重复已经失败的方法。本地只借鉴“失败经历在动作前可见”的边界，不引入其事件目录或 Git hook。
- [projectmem README 的 pre-commit warning 说明](https://github.com/riponcm/projectmem#what-is-coding-agent-memory) 强调提示应来自既有失败记录，而不是让模型凭空猜测。本地因此只按 durable task 的结构化输入摘要匹配，不做语义相似度推断。

## 实现

1. `TaskJournal.repetition_guard(kind, inputs, steps)` 按现有任务输入摘要查找同 `kind`、同输入和同步骤集合的 `failed/interrupted` 任务。
2. 返回 `souldrive_task_repetition_guard_v0.1`、`allow/warn`、匹配 task ID、状态、当前步骤、尝试次数和错误摘要哈希；不返回失败正文。
3. `get_task()`/`public_task()` 暴露该只读派生结果，PPT 状态面板在 `warn` 时提示用户先检查执行轨迹；守卫不改变任务状态、不阻断创建、不重复调用模型。

## 失败优先与验收

- 旧实现先以 `AttributeError: 'TaskJournal' object has no attribute 'repetition_guard'` 失败。
- 实现后 TaskJournal/PPT/项目组合 `63 passed, 1 warning`；回归确认失败正文不出现在守卫结果，输入变化返回 `allow`，公共任务状态返回 `warn`。
- 完整 `scripts/check.ps1` 通过：后端 `1245 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是结构化输入的精确重复提醒，不是语义重复检测；如果调用方把新的 request/id/artifact 标识纳入输入摘要，新的请求可能不会命中，这属于当前保守边界，不应伪装成“已经识别所有重复尝试”。
- `warn` 不等于拒绝；不可逆动作、真实邮件/日历等仍需各自的确认与幂等边界。
- 失败正文仍留在受保护的 TaskJournal/执行轨迹中，不自动转成长期记忆、不进入 RAG/GraphRAG，也不证明失败原因或下一次执行一定正确。
