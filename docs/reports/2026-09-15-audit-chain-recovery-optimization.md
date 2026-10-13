# SoulDrive 审计链 checkpoint 与崩溃恢复优化

日期：2026-09-15

## 外部实践对照

- [OpenParallax audit chain recovery](https://docs.openparallax.dev/audit/)：append-only JSONL 是链头事实源，启动时从最后一条记录恢复 hash；辅助状态不能成为唯一真相。
- [pametan/audit-log](https://github.com/pametan/audit-log)：采用 SHA-256 hash chain，事件顺序和前驱 hash 是可验证约束。
- [daryl crash-safe WAL](https://github.com/daryl-labs-ai/daryl)：崩溃安全写入要让未完成意图可被重放或显式发现，而不是静默生成另一条链。

## 发现的缺口

SoulDrive 审计事件先追加到 JSONL，再更新 `audit_state.json`。旧实现若第二步失败，下一次 append 会信任过期 state 的 `last_event_hash`，导致新事件绕过刚刚已经写入的事件，`verify_chain()` 出现 `previous_hash_mismatch`。审计 JSONL 尾部损坏时也会被当作 `GENESIS` 继续追加，扩大损坏范围。

## 最小实现

- JSONL append 后显式 `flush/fsync`。
- 链头恢复只读取审计 JSONL 的最后一条有效事件；`audit_state.json` 仅作为可重建 checkpoint。
- checkpoint 使用统一的同目录临时文件、`flush/fsync`、`os.replace`。
- 无效 JSONL 尾部直接 fail-closed，不继续追加，保留原始证据供诊断/修复。

## 验证证据

- 审计定向：`9 passed`；审计/工作区/运行状态/API/索引诊断组合：`77 passed, 1 warning`。
- 故障注入覆盖：state 写入失败后重试不分叉、checkpoint fsync 失败保留旧文件、无效尾部拒绝继续追加。
- `scripts/check.ps1`：后端 `1602 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机 hash-chain 审计的事实源/恢复边界改进，不提供远程不可抵赖存证、签名、时间戳服务或跨主机 exactly-once。
- 追加日志本身若已经包含损坏尾部不会自动截断或删除；系统选择保留证据并拒绝扩展，人工修复仍需独立审核。
- 真实断电、文件系统目录项持久化、Tauri/WPS UI/E2E、Redis/PostgreSQL 联调仍未验证。
