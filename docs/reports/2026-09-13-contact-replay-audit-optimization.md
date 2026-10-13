# 联系人重复提交与审计回放优化

日期：2026-09-13

## 发现

SoulDrive 的联系人目录已经按邮箱（大小写不敏感）合并重复保存，避免同一邮箱在联系人解析时形成多个同分候选；但 API 只拿到联系人 ID，不知道这次是首次插入还是已有联系人回放，所以网络重试会重复写 `contact.created` 审计事件，并把回放误报成 `created`。

## 公开实践对照

- [Monica Personal CRM](https://github.com/monicahq/monica) 和 [Nextcloud Contacts](https://github.com/nextcloud/contacts) 都把联系人作为可持续维护的本地实体，而不是每次表单提交都生成匿名记录；本轮沿用 SoulDrive 已有的邮箱身份合并，不引入姓名相似度自动合并。
- [Inngest event identity](https://github.com/inngest/inngest/blob/main/docs/SDK_SPEC.md) 把稳定事件 ID 与一次处理结果绑定；本轮将 catalog 的“首次/已有”状态传给 API 和审计层，避免把存储回放写成新建事件。
- [torkbot/sledge](https://github.com/torkbot/sledge) 的 durable event dedupe 强调同一业务 key 的竞争收敛；本地只借鉴结果状态传播，没有新增联系人事件总线。

## 实现

1. `ProjectCatalog.create_contact(..., return_status=True)` 对按邮箱命中的已有联系人返回 `replayed=true`，新插入返回 `replayed=false`；默认旧调用仍返回字符串。
2. `POST /contacts` 根据 catalog 结果返回 `status=created/replayed` 与 `replayed`，只在首次插入写 `contact.created` 审计事件。
3. 保持原有邮箱合并语义：重复邮箱仍合并别名和项目范围，不按姓名、模糊相似或项目强行创建第二条联系人。

## 失败优先与验收

- 旧实现反例：同一邮箱连续提交两次时返回相同联系人 ID，但两次都写 `contact.created`，第二次没有 `replayed` 标记。
- 修复后联系人 API 与 catalog 定向 `22 passed, 1 warning`；回归覆盖相同邮箱重放、联系人 ID 稳定和审计事件只出现一次。
- 完整 `scripts/check.ps1`：后端 `1275 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机联系人邮箱身份与审计回放保护，不是联系人语义实体解析、模糊去重或外部通讯录同步。
- 无幂等请求头的旧调用仍依赖邮箱合并；联系人更换邮箱会形成新的业务身份，系统不猜测两者是否同一人。
- 该证据不覆盖真实 UI 网络断线、跨进程同时更新同一联系人、CardDAV/邮件导入或外部 exactly-once。
