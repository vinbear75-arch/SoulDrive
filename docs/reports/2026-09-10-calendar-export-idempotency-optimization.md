# SoulDrive ICS 本地产物幂等导出优化

日期：2026-09-10

## 外部规范对照

- [IETF RFC 5545 的 UID 说明](https://datatracker.ietf.org/doc/rfc5545/)要求 VEVENT 使用持久且全局唯一的 UID；SoulDrive 已将 UID 绑定到确认草案的 `artifact_id`。
- RFC 5545 对无 METHOD 的 iCalendar 对象说明 `DTSTAMP` 表示日历存储中的创建/最后修改时间；如果同一确认草案只是因为网络/API 重试重新导出，不应把当前系统时间伪装成一次新的草案修改。

## 本地缺口

`core/calendar_export.py` 原来每次调用都用 `datetime.now()` 生成 `DTSTAMP`。输出路径和 UID 虽稳定，但同一草案的第二次导出字节内容会漂移，无法把重试视为同一份本地产物；这也会让审计 diff 把重试误判为草案变更。

## 最小实现

- `export_ics` 新增可选 `stamp_at`；日程 API 传入 artifact 的 `created_at`，同一确认草案重试使用同一时间戳。
- 直接调用未提供 `stamp_at` 时仍保留当前时间行为，兼容原有调用者；已有稳定 UID、固定输出路径和“只写本地 .ics、不写真实日历”的边界不变。
- 增加函数级和 HTTP 级重复导出回归，验证路径和文件字节均稳定。

## RFC 5545 长内容兼容补丁

RFC 5545 的内容行以 CRLF 分隔，长行按 UTF-8 字节折叠，并用空格开头的续行表示同一属性；当前导出原来只保证换行符，长中文标题/描述会超过 75 octets，原始 `\r` 也可能注入非法物理行。[RFC 5545 Section 3.1](https://datatracker.ietf.org/doc/rfc5545/)给出了该折叠和按字节处理要求。

- `_escape` 将输入的 CRLF/CR 规范化为 iCalendar 文本的 `\\n`，避免用户文本生成额外物理行。
- `_fold_ics_line` 按 UTF-8 octets 折叠，并只在 Python 字符边界切分，不在多字节序列中间切断。
- 新增长中文标题、长描述和 CRLF 输入回归，验证每个物理行不超过 75 octets，展开后仍保留完整原文。

## 验证证据

- `tests/test_calendar_export.py`：`6 passed`；日程草稿/HTTP 定向回归在前一轮为 `20 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1094 passed, 1 warning`；Python compileall 通过；前端构建通过、7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本地 ICS 文件的幂等导出，不是外部日历写入幂等；真实日历仍未接入。
- 如果用户修改并重新确认草案，应产生新的 artifact/时间语义；本补丁不把不同版本错误合并。
