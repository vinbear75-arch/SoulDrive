# SoulDrive 审计链跨进程并发追加优化

日期：2026-09-15

## 外部实践对照

- [Python `msvcrt.locking`](https://docs.python.org/3/library/msvcrt.html)：Windows 可对共享文件区域加阻塞锁；锁区域从当前文件位置开始，无法获取时 `LK_LOCK` 会等待并重试。
- [Python `fcntl`](https://docs.python.org/3/library/fcntl.html)：Unix 使用文件描述符锁；本项目保留该分支以便测试工具和未来非 Windows 环境复用同一语义。
- [OpenParallax audit chain recovery](https://docs.openparallax.dev/audit/) 与 [pametan/audit-log](https://github.com/pametan/audit-log)：审计链的前驱 hash 与事件顺序是可验证约束，不能让并发追加产生第二条有效分支。

## 发现的缺口

上一轮已修复“checkpoint 写失败后从旧 state 恢复”的崩溃恢复问题，但追加过程仍是：读取 JSONL 尾 hash、生成事件、写 JSONL、写 `audit_state.json`。这些步骤没有跨实例锁。

失败优先验证用两个线程在读取尾 hash 后设置 barrier，稳定复现了两个事件都以同一个 `previous_hash` 追加，`verify_chain()` 返回 `previous_hash_mismatch`；在真实并发状态快照替换中还观察到 Windows `WinError 5`。因此仅有原子替换和尾部恢复不足以覆盖 sidecar/worker 并发。

## 最小实现

- 在审计 JSONL 旁建立持久的 `.lock` 协调文件。
- 同一进程先用路径级 `threading.RLock` 防止本进程线程重复进入，再用 Windows `msvcrt.locking` 或 Unix `fcntl.flock` 防止独立进程竞争。
- 将读取尾 hash、追加并 `flush/fsync`、更新原子 checkpoint 放入同一个临界区。
- 不把 `.lock` 或 `audit_state.json` 当作审计事实源；事实源仍是 JSONL，尾部损坏继续 fail-closed。
- 不引入第三方锁库，也不把本地审计锁扩展为业务事务锁。

## 验证证据

- 失败优先反例：无锁版本在 barrier 下产生 2 条记录但链 `ready=false`，并出现一次 `WinError 5` 状态替换冲突。
- 审计定向：`11 passed`，包含 4 线程并发追加和 3 个独立 spawn 进程共 15 条事件的跨进程追加。
- 审计/诊断/工作区/运行时组合：`36 passed`。
- `scripts/check.ps1`：后端 `1605 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机文件级并发串行化，不是跨主机锁、分布式事务或外部副作用 exactly-once；真实 Redis/PostgreSQL/SMTP 联调仍未完成。
- 锁文件只能保护遵守同一锁协议的 SoulDrive 进程；手工编辑 JSONL、其他程序直接追加或文件系统损坏仍由链校验发现并拒绝扩展。
- 该锁不保证目录项在断电后的持久化，也不把审计日志提升为授权、业务状态提交或可抵赖存证机制。
- 真实 Tauri/WPS UI/E2E、长时间进程崩溃和多机部署仍未验证。
