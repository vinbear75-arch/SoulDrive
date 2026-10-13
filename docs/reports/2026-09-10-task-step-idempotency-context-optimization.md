# TaskJournal 步骤幂等上下文优化记录

日期：2026-09-10  
范围：`TaskJournal.run_steps` 与 PPT 生成/打开步骤的 durable side-effect identity

## 外部实现对照

- [LangGraph durable execution 文档](https://github.com/miraland-labs/langchain-ai-docs/blob/main/src/oss/langgraph/durable-execution.mdx)要求把文件写入、API 调用等副作用包在可持久化步骤中，并在重执行时使用幂等键或已有结果检查；持久化状态还要绑定明确的执行线程标识。
- [Hatchet durable tasks 文档](https://github.com/hatchet-dev/hatchet/blob/main/frontend/docs/pages/v1/durable-tasks.mdx)强调恢复时重放事件日志、跳过已完成操作，并要求副作用放在可重试的子任务中，不能把不可重复的外部调用埋在会被重放的控制逻辑里。
- [Temporal retry policy 文档](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)明确 Activity 可能再次执行，因此 Activity 代码必须按可重试、可幂等的边界设计。

本项目已经持久化了 `runtime_task_steps.idempotency_key`，但旧的 `run_steps` 只把 `inputs` 和已完成输出传给 handler，handler 无法直接取得该稳定 key。PPT 处理器虽然已有 task/plan marker，却没有统一接入步骤级 durable identity。

## 失败回归与根因

新增 `TaskJournalTests.test_step_context_exposes_stable_idempotency_key_across_retry`，要求一个步骤第一次失败、第二次恢复时：

1. handler 能看到 `task_id`、`step_name`、`idempotency_key` 和 `attempt`；
2. 两次执行使用同一个步骤幂等键；
3. attempt 从 1 增加到 2。

修复前测试因 `run_steps()` 不接受 `include_step_context` 直接失败，证明 durable key 只存在数据库记录中，没有进入副作用处理契约。

## 最小修复

- `run_steps(..., include_step_context=True)` 开启兼容扩展；默认仍使用原有两参数 handler，不影响旧调用方。
- 启用扩展时传入只读上下文：`task_id`、`step_name`、稳定 `idempotency_key`、当前 attempt。
- PPT 渲染步骤把 step key 写入 `.souldrive.json` marker，并在恢复复用前校验 task、plan digest、step key 和页数；没有上下文的旧直接调用仍保持兼容。
- WPS 请求 marker 改为结构化 JSON，记录请求时间、目标文件和 step key。已有 marker 仍阻止重复启动。
- 没有宣称外部 exactly-once：WPS 进程启动与 journal checkpoint 之间仍是 at-most-once/结果不确定边界，真实外部系统必须继续使用业务幂等键、结果查询和人工确认。

## 验证

- 修复前新增 TaskJournal 回归失败：`unexpected keyword argument 'include_step_context'`。
- 修复后 `tests/test_task_journal.py tests/test_presentation_service.py`：`33 passed`。
- `git diff --check` 通过；本轮仓库全量 `scripts/check.ps1`：后端 `1075 passed, 1 warning`，前端生产构建与 7 组 UI 测试 `48 passed`，Tauri `cargo check --locked` 通过，退出码为 0。

## 边界

该改动只让本机 durable step identity 可被业务处理器稳定使用，不引入 LangGraph、Temporal 或 Hatchet，也不改变现有单机 SQLite 运行边界。若未来接入真实邮件、日历或 WPS 控制，应为每个外部适配器定义自己的请求/结果状态机，不能只把本地 step key 当作跨系统 exactly-once 证明。
