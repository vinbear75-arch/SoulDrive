# Skill 状态回放与审计告警的 UI 可见性优化

日期：2026-09-13

## 发现

后端已经区分 `replayed` 和 `warning`，但前端 Skill 管理器原先只显示“已下载/已生成/已启用”，没有读取这两个字段。用户无法知道本次是重试回放，也看不到“主动作已完成但审计记录暂时写入失败”的重要告警。

## 公开实践对照

- [Open WebUI 文件处理问题](https://github.com/open-webui/open-webui/issues/27987) 将后台处理失败不可见、同步客户端反复上传列为可靠性问题；本轮沿用“状态必须能被客户端看见”的原则。
- [Open WebUI 文件处理状态讨论](https://github.com/open-webui/open-webui/discussions/26931) 关注 pending/processing/failed 的状态展示与恢复；SoulDrive 本轮只补当前已有 Skill API 状态的显示，不扩展新的后台处理状态机。
- [Inngest Durable Endpoints](https://www.inngest.com/docs/reference/typescript/v3/durable-endpoints) 返回已持久化 step 结果；本轮将 `replayed` 作为用户可见结果标签，不把回放伪装成新动作。

## 实现

1. `SkillInboxItem`、`SkillAdapterReview` 和激活/同步响应类型增加可选 `replayed`、`warning`。
2. 安装、同步、审批、激活提示分别显示“回放已有内容/契约/启用结果”和审计 warning；原有状态流转和审核弹窗不变。
3. GET 适配器预览仍不改变审核状态；UI 只依据 POST 返回的结果显示回放。
4. 学习 UI 静态回归新增字段与中文状态文案检查，并重新通过生产 TypeScript/Vite 构建。

## 验收

- 学习 UI 定向测试：`3 passed`。
- 完整 `scripts/check.ps1`：后端 `1288 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。
- Vite 仍有既有主 chunk 大小警告：约 `582.01 kB`，不影响构建通过。

## 边界

- 这是响应状态的 UI 可见性修复，不证明真实 Tauri 网络断线下的端到端渲染；工程门中的前端测试主要是静态/脚本测试。
- UI 展示 warning 不等于自动补偿审计；当前 Skill 审计仍没有独立 outbox。
- 不引入 Open WebUI 的后台上传状态机，也不改变本机 Skill 需要人工审核、脚本不自动执行的安全边界。
