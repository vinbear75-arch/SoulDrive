# SoulDrive PPT durable 标记原子发布优化

日期：2026-09-15

## 外部实践对照

- [Python `os.replace` / `os.fsync` 文档](https://docs.python.org/3/library/os.html)：先同步临时文件，再以替换操作发布完整目标文件。
- [Paige atomic saves](https://github.com/DQ-Labs/Paige)：把恢复状态文件也当作需要原子发布的本地产物，并对同步/替换失败保留旧文件。
- [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery)：恢复依赖持久化的步骤状态，不能把未完整落盘的状态当作可恢复 checkpoint。

本轮只把同一失败边界落到现有 PPT 标记文件，没有引入新的 durable runtime。

## 发现的缺口

PPTX 主文件已经通过临时文件替换生成，但渲染恢复标记、WPS 渲染标记和 WPS 打开请求标记原来直接调用 `Path.write_text()`。因此可能出现主产物完整、`.souldrive.json` 半写或不可解析；下一次 durable task 恢复会丢失已完成步骤的证据并重复执行。

## 最小实现

- 新增 `_write_json_marker()`：同目录临时 JSON、完整写入、`flush/fsync`、关闭后 `os.replace`。
- 三个标记写入点统一使用该函数；失败时保留旧标记并清理临时文件，异常继续向任务层报告。
- 不改变现有任务身份、步骤顺序、PPT 内容校验和 WPS 启动人工边界。

## 验证证据

- `tests/test_presentation_service.py tests/test_calendar_export.py`：`43 passed`。
- 新增 flush/replace 失败回归：旧标记字节保持不变，临时标记不残留。
- `scripts/check.ps1`：后端 `1586 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机标记文件的原子可见性改进，不证明跨存储（PPTX、标记、SQLite）原子提交，也不证明真实 WPS 进程已经启动或外部应用已打开文件。
- 真实 Tauri/WPS UI、打包启动、跨主机 exactly-once 和长时间崩溃恢复仍需单独环境验收。
