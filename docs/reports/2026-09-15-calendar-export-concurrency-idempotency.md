# SoulDrive 日程 ICS 导出并发幂等优化

日期：2026-09-15

## 结论

本轮修复了确认后日程导出的一个并发竞态。原链路虽然使用原子文件替换，并能在顺序重试时根据 `plan_digest` 与文件哈希回放，但两个本机请求可同时通过“尚未导出”的检查，分别写入同一个 `.ics` 并各自产生导出审计事件。

现在每个确认草稿在本机运行目录拥有稳定哈希命名的 sidecar 锁；重新读取草稿后，回执检查、ICS 发布、产物 provenance 更新和审计事件处于同一临界区。并发请求中只有一个真正导出，另一个回放同一结果。锁文件不放在日历产物目录，避免被当作用户日历文件或随日历删除。

## 外部实践对照

- [Restate HTTP invocation](https://docs.restate.dev/services/invocation/http) 将调用幂等键与已持久化响应作为安全重试边界；本地 ICS 没有外部服务响应可缓存，因此沿用 SoulDrive 的 artifact provenance 回执，并补上本机并发 claim。
- [Restate workflows](https://docs.restate.dev/tour/workflows) 强调稳定工作流身份和可恢复步骤；本轮把稳定 artifact 身份映射为锁键，不引入运行时或分布式编排器。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与现有 `local_file_lock` 的本机跨进程协调语义共同作为边界；ICS 内容继续使用同目录临时文件、`fsync` 和 `os.replace`，不把文件原子发布误称为数据库与文件跨存储事务。

## 失败优先验证

新增 `test_confirmed_schedule_ics_export_concurrent_requests_publish_one_receipt`。测试让第一个导出停在文件发布前，第二个请求在没有锁的旧实现中也进入导出路径；修复前稳定观察到 `export_calls == 2`，并发回归因此确认了真实竞态。

## 实现与验证

- `core/mcp_server.py`：在确认草稿导出路径使用运行目录下按 artifact ID 哈希命名的 `local_file_lock`；获取锁后重新读取 artifact，保持旧的内容哈希冲突保护和顺序回放语义。
- `tests/test_topic5_drafts.py`：覆盖两个并发请求只有一次实际导出、一个 fresh 一个 replay、审计事件只写一次，并保留原有顺序回放/用户修改冲突/审计故障回归。
- 日程/Topic 5 定向回归：`42 passed, 1 warning`；其中并发导出失败优先回归确认两个并发请求最终只产生一个真实导出与一个可回放回执。
- 完整 `scripts/check.ps1`：后端 `1630 passed, 1 warning`；前端生产构建通过，8 组 UI 脚本合计 `62 passed`；Python compileall 与锁定 Cargo 检查通过。Vite 保留既有单 bundle 体积警告（约 `585.53 kB`）。
- 冻结记忆质量、GraphRAG 路由、episodic workflow routing 与研究窗口角色评测分别为 `7/7`、`4/4`、`5/5`、`6/6`；真实本地 GraphRAG acceptance 的关键路由、索引、引用和来源多样性验收项全部为 `true`。全局查询仍是受控多来源回退，不能据此宣称模型跨文档综合质量已达标。原始结果：[graphrag_acceptance_after_presentation_scope_guard_20260915.json](../../eval/results/graphrag_acceptance_after_presentation_scope_guard_20260915.json)。

## 边界

- 这是本机多线程/多进程的同一 artifact 导出竞态保护，不是跨主机锁，也不是外部日历服务的 exactly-once 写入。
- 如果用户在导出后修改 `.ics`，仍返回内容冲突并要求重新确认；不会静默覆盖用户修改。
- 日程 API 仍是本地文件导出，不会自动写入真实日历；真实外部日历连接器需要独立的 provider receipt、verify 查询和人工确认。
