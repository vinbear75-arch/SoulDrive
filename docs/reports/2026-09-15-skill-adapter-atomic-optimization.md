# SoulDrive Skill 审核与适配器原子发布优化

日期：2026-09-15

## 外部实践对照

- [Vercel Skills installer](https://github.com/vercel-labs/skills/blob/main/src/installer.ts)：技能目录名需要经过路径安全校验，安装结果区分 canonical 位置与 agent 侧入口。
- [skills.sh](https://github.com/isiahhill/skills.sh)：支持先解析/临时使用，再显式安装；远端 Skill 不能直接等同于本地可执行能力。
- [agent-skill-installer 的 manifest 与卸载约束](https://github.com/omry/agent-skill-installer/blob/main/docs/installing-skills.md)：安装清单记录来源、版本、文件和所有权，供后续卸载、重试和恢复使用。
- [Python `os.replace` / `os.fsync`](https://docs.python.org/3/library/os.html)：派生 JSON 应先完整写入同目录临时文件并同步，再切换可见路径。

## 发现的缺口

SoulDrive 已有“远端文件进入隔离 `.inbox`、人工审核后激活、绝不执行原 Skill 脚本”的边界，但审核标记、激活时生成的 `souldrive_skill.json` 和演示风格适配器分别使用不同的写入方式。进程中断或磁盘写入失败时，可能留下半写 JSON；缓存适配器也使用固定 `.tmp` 路径，缺少统一的 flush/fsync 保障。

## 最小实现

- 新增 `core/atomic_file.py`，提供同目录临时文件、`flush/fsync`、`os.replace` 和失败清理。
- 审核标记、Skill 激活适配器、显式/自动演示风格适配器、Skill 缓存适配器统一使用该原子文本写入。
- 不改变远端 Skill 的下载、人工审核、脚本忽略、项目/会话 workflow 作用域或运行时路由。

## 验证证据

- Skill 安装/适配/运行时/学习/端到端组合：`269 passed, 1 warning`。
- 新增失败优先回归覆盖：审核标记 fsync 失败保留旧 manifest；激活适配器失败仍留在 inbox；演示风格适配器替换失败不产生半成品；缓存适配器替换失败保留旧文件且临时文件清理。
- `scripts/check.ps1`：后端 `1599 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机派生 Skill JSON 的原子可见性与恢复改进，不等于远端供应链可信，也不执行或沙箱化第三方脚本。
- 不证明跨进程/跨机器 exactly-once、真实 Skill 仓库同步、真实 Redis/PostgreSQL、模型答案质量、Tauri/WPS UI/E2E 或断电目录项持久化。
