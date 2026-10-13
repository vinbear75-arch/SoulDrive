# 工作区备份与隔离恢复预检优化

日期：2026-09-13

## 发现

工作区已经同时保存原始资料、项目目录、会话/记忆、任务日志、产物、加密密钥和可重建索引，但此前没有一个可核验的停写门。直接复制多个 SQLite 文件或只保存 `index/` 都不能证明恢复后仍能解锁、找回项目和重建检索链路。

本轮没有把“备份平台”或“一键恢复”误装进首轮范围，而是先实现计划要求的最小前置：明确保存范围、密钥依赖、停写顺序和测试工作区 SQLite 恢复演练。

## 公开实践对照

- [SQLite Online Backup API](https://www.sqlite.org/backup.html) 说明在线备份可以得到一致快照，源数据库只在短暂读取期间加读锁；目标数据库在备份完成后成为源数据库的快照。SoulDrive 只复用这一点，不宣称多个数据库之间天然原子。
- [restic repository check](https://restic.readthedocs.io/en/stable/045_working_with_repos.html) 将结构一致性检查与完整数据读取检查区分开；SoulDrive 对应地把“工作区范围预检”和“单 SQLite integrity_check”分开，不把文件存在当成可恢复证明。
- [Kopia snapshot verify](https://kopia.io/docs/reference/command-line/common/snapshot-verify/) 体现了恢复前验证内容的边界；本轮只落地本机、无外部仓库依赖的最小验证原语。

## 实现

1. 新增 `core/workspace_backup.py` 的 `build_backup_preflight()`，返回 `souldrive_workspace_backup_preflight_v0.1`：
   - `read_only=true`，不停止进程、不写 runtime state、不打开密钥、不创建目标目录；
   - 检查 `runtime_state.indexing` 是否处于 `queued/scanning/indexing`；
   - 以只读 SQLite 连接检查任务日志，`pending/running/interrupted` 任一存在即阻塞；任务日志存在但不可读时 fail-closed；
   - 将 `config`、资料、目录、会话/记忆、任务日志、产物、技能和审计列为 `authoritative`，`config/keystore.json` 单独列为 `key_dependency`；
   - 将 `index` 标为 `derived_rebuildable`，`models` 标为 `optional_cache`，避免把派生索引或模型缓存冒充原始资料；
   - 输出不含文件名列表、正文或密钥内容，只给路径类别、文件数、大小、敏感标记和状态。
2. 新增 `GET /diagnostics/backup`，复用本机挂载/软件解锁门，供 UI/人工操作读取预检，不自动执行备份或恢复；预检会复核持久化的索引 worker 身份，避免 sidecar 指针丢失时误判可备份。
3. 新增 `backup_sqlite_snapshot()`：使用 SQLite Backup API 复制单个数据库；目标已存在、源目标相同、源不存在或目标完整性检查失败时拒绝/失败，不静默覆盖目标。
4. 固化停写顺序：停止索引器和任务 worker → 等待运行步骤进入终态 → 重新读取状态/任务日志/预检 → 保存权威范围和 SQLite 快照 → 恢复时先恢复配置/密钥与权威范围，最后重建派生索引。

## 失败优先与验收

- 先加入四个反例；实现前测试因 `core.workspace_backup` 不存在而失败。
- 实现后 `tests/test_workspace_backup.py` 通过 `6 passed`：覆盖索引/任务活跃阻塞、活 worker 阻塞、敏感密钥不出现在诊断、任务日志损坏 fail-closed、SQLite 隔离快照与拒绝覆盖、项目目录/来源/keystore 隔离恢复演练；索引专项另覆盖 sidecar 指针丢失后的 PID/启动时间复核。
- 备份/索引服务组合定向回归通过：`26 passed, 1 warning`。
- 完整 `scripts/check.ps1` 通过：后端 `1267 passed, 1 warning`；前端生产构建通过，7 组测试合计 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是备份/恢复预检和单库快照原语，不是工作区全量复制器、远程仓库、在线协调快照或一键恢复。
- 当前没有自动停止索引器/worker，也没有覆盖多个 SQLite 与普通文件的跨存储原子提交；调用者必须按照清单人工停写。持久化 PID/启动时间只提供本机 owner 复核，不是跨进程 lease fencing。
- 测试验证的是临时工作区中的单库恢复，不等于真实用户项目已完成恢复演练；下一步应在隔离测试工作区复制一个项目的权威资料、目录、任务和加密资产，再做解锁、索引预览和项目证据回读。
- `models` 和 `index` 不属于恢复正确性的最低集合；恢复后若索引未重建，系统必须继续显示未验证/需重建状态，不能把旧索引当作当前证据。
- 完整检查仍不覆盖真实 Tauri UI、WPS 视觉检查、安装包冷启动或真实用户资料恢复。
