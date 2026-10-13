# 记忆派生同步：失败收据故障隔离

日期：2026-10-08。本轮只改记忆同步重放的故障隔离，不改变检索、提示词、记忆有效时间、项目范围、重试上限、租约或任何冻结评测数据。

## 外部机制与本机缺口

[Dapr outbox 官方说明](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/)说明投递或清理失败需要重试，交付语义为至少一次；[Temporal 的幂等与耐久执行说明](https://temporal.io/blog/idempotency-and-durable-execution)区分流程与有副作用的执行，并要求重试不产生意外重复效果。本轮借鉴的是故障边界和待恢复事件，不部署任何新服务，也不把现有实现宣称为 Dapr 的事务性 outbox 或外部 exactly-once。

当前 `_replay_learning_memory_outbox` 被工作区加载、聊天记忆注入、记忆列表和搜索入口共用。外部投影或完成收据异常已进入处理分支，但该分支再次调用 `fail_memory_sync` 时若 SQLite/加密/连接写入也异常，会从处理分支直接抛出，后续已经领取的独立事件不再执行。上层可保留事实源可读，不代表这一批同步已恢复。

现有归档/投影已有版本与状态防回退，现有 claim token 已防过期/失权的完成确认；这两项本轮没有重新实现。也不扩大项目作用域或增加自动重试次数。

## 运行前锁定的验收条件

- 数据：新工程反例只含两个虚构用户偏好，临时工作区、虚构测试口令；SQLite/outbox 为真实本地实现，投影后端是 mock，不读取待审核种子、论文正文、封闭测试标签或真实用户记忆。
- 故障：分别覆盖第一条投影抛异常、投影返回 degraded、完成收据抛异常三种情况，随后失败收据写入也抛异常；第二条投影正常。三条用例各有独立临时工作区，时钟固定，避免依赖睡眠或人工竞争时机。
- 预期：统计为 completed=1/failed=1；两条投影都被尝试；第一条仍 pending 且不得伪造完成/错误收据，第二条已确认完成；固定警告不包含异常正文。
- 恢复：关闭并重开同一临时 LearningStore，原租约过期后只重试第一条并完成，第二条不重复执行。
- 回归：现有完成收据失败、失权确认、项目过滤和版本防回退保持通过；定向后运行完整工程检查。质量门只按原冻结合同解释，不证明开放式记忆问答质量。

基线 HEAD 为 `e9aa88c722c1a6ef61b25c191048caf1c0423da1`，工作树已有大量改动且全部保留。B0 运行前 `core/mcp_server.py` SHA256 为 `dbf0c705ee06aad7313bf244e31d290c89b4eb328e2347f774615afb0348e477`；LearningStore 为 `6916bb7b4392fa7fb206e28a75cb9541967e0a0b1229e88abd3126234870e990`。运行环境为现有 `souldrive` Conda 环境；不公开本机解释器绝对路径。

## B0 / 候选 / 验证记录

新增三个反例后、改生产代码前，B0 为 **3 failed、141 deselected、1 warning，2.29 秒**（退出 1）。三条均因异常处理分支里的失败收据写入再次抛出而终止，不是检索结果或标签断言失败。此前单条投影异常反例也先失败；两次运行没有改冻结数据。

候选仅在共用重放器的异常分支为失败收据写入加一层故障隔离，保留固定不含异常正文的 warning，并把当前事件计入 failed 后继续后续事件。没有伪造失败收据写入成功、清除 pending、跳过租约确认或改变对外摘要字段。SQLite 暂不可写时该事件沿用原租约过期恢复路径。

候选三个反例为 **3 passed、141 deselected、1 warning，0.82 秒**（退出 0）。三条分别验证坏事件 pending、正常事件已完成、固定警告不含异常正文，以及关闭/重开后的单事件恢复。

首次扩大范围测试的会话句柄丢失，随后系统查询未发现对应进程，未取得终态结果，因此不计为通过。已用相同范围重跑并增加 JUnit 结果文件；以下只记录重跑实际取得的终态，不以部分输出替代结果。

扩大范围重跑已结束，退出 0：`tests/test_mcp_server_learning.py`、`tests/test_learning_store.py`、`tests/test_conversation_store.py`、`tests/test_memory_runtime.py`、`tests/test_task_journal.py` 合计 **386 passed、1 warning，295.51 秒**。持久化结果为 `build/quality-gates/memory-outbox-receipt-targeted-20261008.xml`。警告为既有 Starlette/httpx 弃用提示，不在本轮调整依赖。

第十二轮完整工程检查已结束，执行会话 `40704` 终态退出 0：后端 **1718 passed、1 warning，444.50 秒**；Python 编译检查、三个冻结质量门、前端构建、8 组前端脚本 **66 项**及锁定 Cargo 检查通过。三个结果文件复查分别为记忆 7/7 场景（8/8 查询）、图路由 4/4、研究角色 6/6。这些是原冻结规则合同的结果，不是新的开放式回答质量分数。

本轮实际聚合执行宿主为 PowerShell 7.6.5，Python 使用现有 `souldrive` Conda 环境（3.10.20）；不能据此声称完成 PowerShell 5.1 专项兼容验证。`build/quality-gates/memory-outbox-receipt-full-check-20261008.log` 只记录 transcript 的宿主、起止与会话元数据，没有收录原生命令的完整标准输出；完整检查终态和上述计数来自执行工具输出，不把该文件冒称为完整测试日志。定向 JUnit 文件与三个质量门 JSON 可独立复核。前端构建仍有既有大分块提示，未作为本轮优化对象。

候选最终 SHA256：`core/mcp_server.py` 为 `77a742239f67a5c5428bcfa3c15d66334f637eeae5be1afb29c3d264ad80a684`；`tests/test_mcp_server_learning.py` 为 `8f09ec21da96eb9b6c5839f00376cbdd8fd516a251a5eecb1f027a928c5b48d9`。LearningStore 与投影后端源码未改变。30 条待审核种子、其 manifest、原 RAG 开发集、三个冻结规则集和种子引用的 `tests/test_experiment_ledger.py` 均复查哈希保持不变。

## 下一项已复现、尚未修复

只读核查研究窗口后，用全新临时工作区、虚构项目和一条合成实验运行，通过真实 `_load_explicit_experiment_comparison` 入口将同一 `run_id` 同时指定为基线与当前。结果仍为 `status=comparable`、1 条变化、1 条 M 证据、差值 0；没有调用模型或使用待审核业务语料。问题位于共享的两运行可比性检查缺少运行身份区分，不能据此宣称研究对比已经全部可靠。

后续需先增加工程反例，要求同一次运行不能伪装成两次实验，且不同运行的相同数值仍允许正常比较；项目范围检查与现有指标兼容门保持不变。该修复尚未实施，尚无候选通过结果。当前 goal 工具返回 `usageLimited`，本轮仅归档已完成验证，不自行解除限制或启动下一轮优化；这不是项目完成标记。

## 尚未证明

不证明真实 Redis/PostgreSQL、真正断电、跨进程/跨主机 exactly-once、文件系统与 SQLite 原子事务、长时间批量租约调度或真实 Tauri UI。新语料候选采纳仍待用户审核，原 30 个场景、gold 和冻结集不动；GraphRAG 开放式内容质量与完整发行流程也不在此单点修复中宣称完成。
