# 日程 ICS 导出回放与审计边界优化

日期：2026-09-13

## 发现

日程导出使用稳定的 `artifact_id.ics` 路径，重复导出已经能得到相同字节，但接口每次都会重写本地文件并追加 `schedule.ics_exported` 审计事件，UI 也无法区分首次导出与重复请求。网络响应丢失后再次点击会造成重复执行和错误的用户语义。

## 公开实践对照

- [Restate HTTP invocation](https://docs.restate.dev/services/invocation/http)：相同幂等身份在保留期内返回原结果，不重新执行服务。
- [Restate Architecture](https://docs.restate.dev/references/architecture)：完成的步骤从 journal 恢复，迟到尝试不能重新提交已完成结果。
- [Prefect caching](https://docs.prefect.io/v3/concepts/caching)：缓存键应绑定输入，输入变化不能误用旧结果。

本轮只借鉴本地“输入绑定结果回执”原则，不引入 Restate/Prefect 运行时。

## 实现

1. 以规范化日程计划的 SHA-256 作为导出回执的输入摘要，并将回执写入 artifact 的 `confirmation_record.calendar_export`。
2. 同一 artifact、同一计划摘要且目标 `.ics` 文件仍存在时，直接返回 `replayed=true`，不重写文件、不重复写导出审计。
3. 计划摘要变化或目标文件缺失时重新导出；不把旧回执误用于新计划。
4. 导出成功后审计写入采用已有 best-effort 边界：主文件和回执成功时仍返回成功，同时返回 bounded warning。
5. UI 消费 `replayed/warning`，明确显示“未重复写入本地 .ics 文件”。

## 验收

- 日程/ICS 定向回归：`9 passed, 1 warning`。
- 前端日程静态契约测试：`5 passed`；生产构建通过。
- 完整 `scripts/check.ps1`：后端 `1290 passed, 1 warning`；前端构建及 8 组测试共 `56 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机单 sidecar 的本地文件导出回执和审计语义，不是外部日历写入 exactly-once。
- 目标文件被用户删除后会重新导出；文件系统删除/替换不是本模块可证明的事务。
- artifact 回执与文件写入仍是两个本地存储边界，未宣称跨存储原子提交、跨进程 exactly-once 或真实 Tauri/UI E2E。

## 2026-09-14 后续校验：输出内容指纹边界

补测发现仅凭“计划摘要相同 + 目标文件存在”仍不足以判定重试可以安全回放：用户或其他本地程序可能已经改写同一路径的 `.ics` 文件。现已在 `confirmation_record.calendar_export` 保存输出文件的 `content_sha256`，重试时同时校验计划摘要、目标路径和文件内容指纹。

- 指纹一致：返回 `replayed=true`，不重写文件、不重复追加成功审计。
- 指纹缺失或不一致：返回 `409 calendar_export_output_conflict`，保留用户当前文件，不静默覆盖；需要重新确认后再导出。
- 新增回归：`tests/test_topic5_drafts.py` 的 ICS 场景与 `tests/test_calendar_export.py` 合计 `38 passed, 1 warning`。
- 本轮最终完整 `scripts/check.ps1`：后端 `1501 passed, 1 warning`；前端生产构建及 8 组脚本通过；`compileall` 与 `cargo check --locked` 通过。

旧版本没有输出指纹的回执按“无法证明完整性”处理，不直接把现有文件当作可信回放结果；这保证了升级后的安全边界，但不会替历史文件自动背书。
