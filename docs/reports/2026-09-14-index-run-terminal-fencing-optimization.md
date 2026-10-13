# 索引运行回执状态单调与终态 fencing 优化

日期：2026-09-14

## 发现

`IndexRunJournal.update()` 过去对同一个幂等键无条件更新状态。运行时 JSON 已经按 `run_id` 拒绝陈旧索引 worker，但持久回执仍可能被迟到的 `scanning`/`indexing` 回调回退，或在 `completed` 后被迟到 `failed` 改写。这样客户端重试会看到与事实运行状态不一致的终态。

## 公开实践对照

- [Temporal workflow definition](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-definition.mdx)：可恢复工作流依赖可重放、确定性的事件历史，已产生的历史不能由迟到执行路径静默改写。
- [Temporal architecture](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/architecture/temporal-architecture.mdx)：事件历史用于重建工作流状态，持久状态应能在失败后恢复，而不是接受任意顺序的状态覆盖。
- [Restate v1.7 safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：重试需要稳定身份，终态结果和入口重试要能区分，不能让重试把不同执行结果静默合并。

本轮只给现有 SQLite 索引回执增加状态机约束，不引入 Temporal/Restate 等重量级运行时。

## 实现

- 固化 `starting → queued → scanning → indexing → completed/failed` 的单调顺序。
- `completed` 与 `failed` 都视为终态；终态一旦落库，任何迟到的非终态或另一终态更新均保持原结果。
- 将允许的当前状态直接编码进 SQL `UPDATE ... WHERE status IN (...)`，让跨进程竞争时由 SQLite 的原子更新条件裁决，而不是只在 Python 读取后判断。
- 保留原有失败观测脱敏、幂等键输入摘要冲突和运行时 `run_id` fencing；本轮不改变索引 worker 的启动/重试策略。

## 验证

- 失败优先：新增回归在修复前确认 `scanning` 会覆盖 `indexing`，修复后通过。
- 索引幂等回归与运行时审计：`13 passed, 1 warning`。
- 完整工程门禁：后端 `1509 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。该回归仍是本机 SQLite 级证据，不等于多机 worker、真实 sidecar 崩溃恢复或跨存储 exactly-once。

## 边界

这保证的是单个索引回执的状态不回退和终态不被迟到投影覆盖；它不证明索引内容正确率、worker 外部副作用 exactly-once、真实 Tauri/WPS UI/E2E 或跨设备调度能力。
