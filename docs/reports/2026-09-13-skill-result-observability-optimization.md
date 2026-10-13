# Skill 匹配与 PPT 样式结果观测边界优化

日期：2026-09-13

## 对照实践

- [Open WebUI 审计实现](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/audit.py) 将观测写入故障与主请求结果分开记录；本轮沿用这一点，把 Skill 匹配、样式适配和删除结果与本地审计副作用分离。
- [Open WebUI 重复 Knowledge 条目问题](https://github.com/open-webui/open-webui/issues/24954) 说明本地 Skill/Knowledge 的来源、内容和重复状态必须可追踪；本轮不改变 Skill 的 source path、digest、唯一解析和删除安全门。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将业务结果、事件和可重试工作分层；SoulDrive 当前只吸收结果/观测边界，不引入外部队列或远程执行。

## 失败优先发现

- `/learning/skills/match` 匹配候选已经算出后直接写审计，审计故障会把可用候选变成 500。
- `/learning/skills/{skill_id}/presentation-style` 虽然捕获审计异常，但只写本地日志，UI 无法知道样式文件已生成而审计缺失。
- Skill 删除主文件与 LearningStore 记录已经完成后，旧 warning 会把底层审计异常原文返回，存在路径/内部信息泄露风险。

## 本轮修改

- Skill 匹配返回候选集合并在审计失败时附加固定 warning。
- PPT 样式适配返回 `created/style_path/skills` 结果并暴露固定 warning。
- Skill 删除继续保持已完成结果，同时统一使用 `_append_audit_best_effort`，不再回显异常文本。
- 不改变 Skill 来源路径校验、重复匹配拒绝、PPT 目标限制、文件清理失败保护和数据库记录删除语义。

## 验收证据

- 新增匹配、PPT 样式审计故障回归；更新删除回归以验证敏感异常文本不回显。
- Learning/Skill 完整回归 `114 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1324 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是 Skill 质量、自动执行权限、沙箱或供应链签名增强。
- 这不是 Skill 事件 outbox、自动补偿、外部 exactly-once 或跨进程 durable execution。
- 这不证明 PPT 真实渲染、WPS 兼容或 Tauri/UI/E2E；审计 sink 仍可能丢事件，只是不再覆盖 Skill 主结果。
