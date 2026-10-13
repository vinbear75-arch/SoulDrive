# SoulDrive 企业策略初始化快照优化

日期：2026-09-15

## 外部实践对照

- [Python `os.replace` / `os.fsync`](https://docs.python.org/3/library/os.html)：配置文件初始化同样应先完成临时文件，再发布可见版本。
- [DBOS recovery](https://docs.dbos.dev/production/workflow-recovery)：恢复所依赖的状态应能区分完整 checkpoint 与尚未完成的写入。
- [SQLite isolation](https://www.sqlite.org/isolation.html)：读取者不应看到配置写入中的中间状态。

## 发现的缺口

`ensure_policy_file()` 在首次创建企业策略时直接写 `enterprise-policy.json`。这是安全默认、请求限制和模型清单策略的初始化入口；写入中断后会留下半写 JSON，而 `load_policy()` 会静默回退到默认策略，掩盖初始化失败。

## 最小实现与验证

- 改为复用 `core.atomic_file.atomic_write_text()`，使用唯一同目录临时文件、`flush/fsync`、`os.replace`。
- fsync 故障回归确认目标文件不存在、临时文件无残留；既有策略读取/边界测试保持通过。
- 策略/诊断定向 `13 passed`；`scripts/check.ps1` 后端 `1603 passed, 1 warning`，前端生产构建成功、UI `62 passed`，Python `compileall` 与锁定 Cargo 通过。

## 边界

这只保证本机策略 JSON 的完整可见性，不证明策略签名校验、跨进程并发更新、远程配置分发、断电目录项持久化或真实 Tauri/WPS UI/E2E。
