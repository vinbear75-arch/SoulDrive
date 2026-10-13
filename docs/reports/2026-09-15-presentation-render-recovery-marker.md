# 2026-09-15 PPT 渲染恢复 marker 优化

## 结论

本轮修复 PPT durable generation 的一个外部副作用窗口：`render_pptx` 先生成本地 `.pptx`，再写恢复 marker。进程如果在两者之间退出，重试时旧逻辑看不到 marker，会再次执行渲染，可能覆盖已经被用户打开或修改的有效 PPT。

现在渲染前先写入 `status=rendering` 的 marker，渲染完成后再写 `status=completed`。重试只要发现 marker 身份与任务/大纲摘要匹配、且 PPTX 通过结构校验，就直接接管已有文件并把 marker 收敛为 `completed`，不重复调用 renderer。marker 写入仍使用现有临时文件 + flush/fsync/replace 安全路径。

## 外部实践对照

- [DBOS Architecture](https://docs.dbos.dev/architecture) 要求 workflow 从最后一个已完成 step 的 checkpoint 恢复；本轮把 renderer 前的 phase receipt 和有效 PPTX 校验作为本机最小 checkpoint，不引入 DBOS runtime。
- [DBOS Transactional Outbox](https://docs.dbos.dev/python/examples/outbox) 明确外部操作通常是 at-least-once，要求副作用具备幂等或可恢复边界；本轮使用确定的任务/大纲/step 身份和结构校验，避免同一有效产物被无条件重渲染。
- [Restate Key Concepts](https://docs.restate.dev/foundations/key-concepts) 以 journal 记录已完成动作并在重试时跳过；本轮只在 marker、任务身份和 PPTX 校验全部匹配时跳过 renderer，校验失败仍重新生成。
- [DBOS Concurrent Executions](https://docs.dbos.dev/explanations/concurrent-executions) 强调并发执行必须在 checkpoint 边界收敛；本轮没有声称文件系统 exactly-once，仍依赖 TaskJournal lease 和确定性输出路径。

## 失败优先证据

新增 `test_render_reuses_valid_output_when_final_recovery_marker_write_is_lost`：

1. 第一次调用先成功写 `rendering` marker；
2. renderer 生成有效 PPTX；
3. 模拟最终 marker 写入失败；
4. 第二次调用验证文件有效且身份匹配，renderer 不再调用，marker 变为 `completed`。

旧逻辑没有渲染前 marker，无法在第 4 步区分“尚未生成”与“文件已生成但完成收据丢失”。

## 最小实现

- `core/presentation_service.py` 把任务身份、`plan_digest`、step 幂等键和 slide count 组成 marker payload。
- OpenXML 与 WPS/COM 两条渲染路径都在外部 renderer 前写 `rendering`，成功后写 `completed`。
- 恢复条件仍要求任务身份、计划摘要、step key（如有）、页数和 `verify_pptx` 全部匹配；不匹配不会复用文件。
- legacy marker 没有 `status` 时保持兼容；只有明确的 `rendering` 状态才在恢复成功后补写 `completed`。

## 验证证据

- marker/renderer 定向：`4 passed`；PresentationService 全部：`41 passed`。
- TaskJournal + PresentationService + 论文接口组合：`87 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1663 passed, 1 warning in 430.48s`；前端生产构建成功，8 组 UI 脚本合计 `66 passed`；`compileall` 与锁定 Cargo 检查通过。

## 边界

本轮证明的是本机 PPT 文件恢复 marker 在“文件已生成、完成收据写入失败”窗口的行为，不证明 PPT 内容事实准确率、WPS/COM 真实启动、用户在 renderer 期间编辑文件后的语义合并、跨进程/跨主机 exactly-once、真实 Redis/PostgreSQL 或真实 Tauri/WPS UI/E2E。若文件有效但内容已被用户外部修改，结构校验不能证明内容仍符合原计划；因此没有扩大为内容级自动覆盖策略。
