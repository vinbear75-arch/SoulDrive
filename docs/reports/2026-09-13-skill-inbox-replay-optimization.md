# Skill 审核收件箱回放与同步审计优化

日期：2026-09-13

## 发现

Skill 下载安装已经采用“先进入 `.inbox`、审核后激活、只运行声明式适配契约”的安全边界，但同一网络请求因超时或用户重复点击时，第二次提交会因为目录已存在而报错；API 也没有区分首次下载和本地回放。与此同时，`POST /learning/skills/sync` 在内容没有变化时仍会重复写同步审计事件。

## 公开实践对照

- [Open WebUI 重复 Knowledge 摄取问题](https://github.com/open-webui/open-webui/issues/24954) 记录了同一文件内容因新记录 ID 被再次摄取、重复启动 embedding 任务的风险，并建议在持久层按内容 hash 去重、将重复提交作为幂等成功处理。
- 本地适配只借鉴“内容身份优先于显示名/新记录 ID”和“回放应复用已有结果”两点；SoulDrive 的 Skill 下载仍保持独立的待审核收件箱，不引入远端脚本执行或自动激活。

## 实现

1. `install_skill_to_inbox()` 在 `.inbox/<skill_name>` 已存在时，按来源、目标、文件清单、`SKILL.md` 摘要和所有已下载文件字节进行匹配；完全一致时返回 `replayed=true`，保留原目录和原安装清单。
2. 临时目录提交遇到同名并发创建时，复用同样的内容匹配；同名但来源或内容不一致仍 fail-closed，不覆盖待审核材料。
3. `/learning/skills/install` 只在首次写入时记录 `learning.project_skill_downloaded`，回放把状态返回给调用方但不重复写创建审计。
4. `/learning/skills/sync` 只有发生导入、更新或重复副本归档时才写 `learning.project_skills_synced`；纯 `unchanged` 同步不制造审计噪声。

## 失败优先与验收

- 新增回归覆盖：同一待审核 Skill 重试返回同一目录且不改写 manifest；安装 API 回放不重复下载审计；无变化同步不重复同步审计。
- Skill 安装器、项目 Skill 发现和学习 MCP 路由组合：`93 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1278 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 当前回放仍会重新下载并校验远端内容；本轮保证的是本地待审核写入与 API/审计回放，不是网络请求级缓存，也不证明远端内容不可变。
- 这是本机单工作区的持久目录保护；没有宣称跨进程/跨机器 exactly-once、分布式锁、签名供应链验证或自动等待同一下载任务。
- 同名不同来源或内容会拒绝覆盖，用户需选择新名称或清理旧待审核项；系统不做语义相似 Skill 合并。
- Skill 仍必须人工审核后激活，脚本不会因为回放而执行。
