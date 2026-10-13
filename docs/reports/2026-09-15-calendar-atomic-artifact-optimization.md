# SoulDrive 本地 ICS 产物原子发布优化

日期：2026-09-15

## 外部实践对照

- [Python `os.replace` / `os.fsync` 文档](https://docs.python.org/3/library/os.html)：先刷新并同步文件描述符，再用替换操作发布完整文件；同目录临时文件避免跨文件系统移动。
- [Paige 的原子保存实现说明](https://github.com/DQ-Labs/Paige)：同目录临时文件、`fsync`、`os.replace`，并对替换/同步失败保留原文件做回归。
- [atomicwrites 文档](https://python-atomicwrites.readthedocs.io/)：把临时文件写入、同步和最终替换作为一个清晰的本地产物发布边界。

本轮只借鉴文件可见性与失败保留原则，没有引入第三方依赖，也没有把本地文件发布误称为外部日历 exactly-once。

## 发现的缺口

`core/calendar_export.py` 原来直接对最终 `.ics` 路径执行 `Path.write_text()`。如果进程在写入过程中被中断，或磁盘/权限错误发生在写入阶段，目标文件可能已经被截断；这会与 artifact 中保存的 `content_sha256` 回执形成“文件已出现但不可验证”的状态。

## 最小实现

- 先在 `calendars_path` 同目录创建随机临时文件，写入 UTF-8 内容后执行 `flush()` 和 `os.fsync()`。
- 只有临时文件完整关闭后才调用 `os.replace()` 发布到 `artifact_id.ics`；异常时清理临时文件并保留原目标文件。
- 保留现有稳定 UID、固定路径、内容指纹、确认回执和修改冲突门；没有扩大到真实日历写入。

## 验证证据

- `tests/test_calendar_export.py tests/test_topic5_drafts.py`：`41 passed, 1 warning`。
- 新增失败优先回归：`fsync` 失败和 `replace` 失败时，旧文件字节不变，临时文件无残留，异常仍返回调用方。
- `scripts/check.ps1`：后端 `1584 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机单文件的原子可见性与失败保留改进，不证明断电后的目录项持久化、不证明跨文件系统替换，也不证明 artifact 回执与文件系统跨存储原子提交。
- 真实日历服务、SMTP、Redis/PostgreSQL、Tauri/WPS UI 和端到端打包启动仍未在本轮门禁中验证。
