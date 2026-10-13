# TaskJournal 恢复扫描快照 fencing

日期：2026-10-11；第十九轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只修复本机 `TaskJournal` 恢复控制面的旧快照竞争，不增加 Temporal/DBOS 服务、超时接管或后台 worker。业务数据、30 条待审核种子、冻结评测集、gold、提示词、模型和指标均不改变。

验收条件：

1. 扫描看到旧 worker 已退出，但另一连接已经恢复并启动新租约时，旧扫描不得改变新任务、步骤或尝试历史；新 worker 仍能提交结果。
2. 同一旧快照已经被另一个恢复请求处理时，不得重复列入本次 `recovered`。
3. 死进程正常恢复、活进程长步骤保护、旧 worker 写入 fencing、取消和完成步骤复用保持通过；随后执行完整 `scripts/check.ps1`。

## 外部机制与本地证据

- 已采纳 C06 的 [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/blob/b3375504ee29bda57585a5eab7e11a7df39b3472/README.md) 将 workflow 身份、持久化恢复和重放边界分开。这里借鉴耐久状态/执行身份的原则，不复制分布式引擎。
- [DBOS Concurrent Executions](https://docs.dbos.dev/explanations/concurrent-executions) 的 Workflow Ownership 小节在同一数据库事务内核对当前执行所有权；这与本地已有 `lease_epoch` 适配。
- [SQLite Transactions](https://www.sqlite.org/lang_transaction.html) 的 2.1/2.2 小节说明单 writer 和 `BEGIN IMMEDIATE`；本轮用它保证读取当前 epoch 与中断更新处于同一写事务。

真实入口：`PresentationService.run_task()`、`get_task()`、`list_tasks()` 都会调用 `recover_orphaned_tasks()`。旧实现先获取 running 行，再在事务外检测旧 `owner_id`；调用 `interrupt_task()` 时没有绑定当时的 `lease_epoch`。因此两次恢复/重新领取交错时，过时的进程检查会中断当前新租约。

## B0 与实验固定条件

- 数据：临时工作区/加密 SQLite；两条独立 `TaskJournal` 连接；在进程活性检查处确定性交错另一个恢复操作；不读取真实用户任务、不调用模型或外部副作用。
- 环境：本机 Windows，`souldrive` Conda；Python 3.10.20，SQLite 3.53.2；无随机采样/模型参数，随机任务 ID 只用于临时身份，不参与断言。
- B0 生产文件 SHA-256：`core/task_journal.py` = `ced16b0ec353fbba518dc311075ff6b116e22ab8e155621adbc8b17e67671920`。
- B0 原测试文件 SHA-256：`tests/test_task_journal.py` = `348029ab2bfb2df201bb315aac0f96c13ba03075d9e0f32098bf8c4556de5b0c`。
- 新增反例执行：`python -m pytest -q tests/test_task_journal.py -k stale_recovery_scan`，旧生产实现结果为 **2 failed, 29 deselected**，0.33 秒。
- 逐例：新 worker 的任务实际变为 `interrupted`，而非 `running`；第二例把另一恢复已处理的任务错误列入本次 `recovered`。
- 日志：`build/quality-gates/task-recovery-fencing-b0-20261011.log`。

## B1 / Candidate

已实现：

- `recover_orphaned_tasks()` 查询并携带观察到的 `lease_epoch`。
- 恢复中断进入 `BEGIN IMMEDIATE`，在同一事务中复核 `lease_epoch` 与 `running` 状态；旧快照遇到代数变化抛出既有 `TaskLeaseLostError`，扫描跳过且不计入本次恢复结果。
- 无 epoch 的直接 `interrupt_task()` 调用保持兼容，用于明确的人工/控制面中断；恢复路径必须带 epoch。

候选定向结果：`tests/test_task_journal.py` **31 passed**；PPT durable、演示 API、运行时和索引幂等组合 **112 passed, 1 warning**。`compileall` 与 `git diff --check` 通过（仅报告已有 LF/CRLF 转换提示）。

完整 `scripts/check.ps1` 退出码 0：后端 **1736 passed, 1 warning in 458.77s**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；workflow 角色 **6/6**；前端生产构建成功、UI **66 passed**；锁定 Cargo 与编译检查通过。日志：`build/quality-gates/task-recovery-snapshot-fencing-full-check-20261011.log`。

当前文件 SHA-256：`core/task_journal.py`=`b956995d244bda490334db15624136caf978472238beabad6991440751ddf079`；`tests/test_task_journal.py`=`4963b8e5ebd404a5032b8003df4658bbf867ee009d08a8bd5e4a576b8b6769b5`。

B2 不适用：本轮是可证明状态竞争的正确性修复，不是新增检索或模型方案竞赛。

## 验证与边界

以上 B0/Candidate 只证明同进程双连接的确定性交错与本机 SQLite 事务 fencing；完整门禁不证明真实跨进程压力、断电、跨主机、外部副作用 exactly-once、模型答案质量或 Tauri/WPS UI/E2E。长同步步骤仍依赖单机进程存活判断，不引入远程 heartbeat/协调服务。
