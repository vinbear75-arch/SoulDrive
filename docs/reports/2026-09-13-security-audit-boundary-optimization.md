# 安全初始化与授权审计边界优化

日期：2026-09-13

## 对照实践与取舍

- [Vault Audit Devices](https://developer.hashicorp.com/vault/docs/audit) 把审计设备视为服务可用性的一部分：无法写入至少一个设备时拒绝服务，并建议配置冗余审计设备。
- [Vault 审计设备故障恢复实践](https://developer.hashicorp.com/vault/tutorials/monitoring/blocked-audit-devices) 展示了审计设备阻塞时请求可能暂停，说明强合规场景不能把“已审计”与“业务成功”混为普通日志副作用。
- [Open WebUI audit implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/audit.py) 对本地应用审计写入采用异常捕获和运维日志，适合作为轻量本地产品的观测参考。

SoulDrive 当前是 Windows 本机单用户模式，`AuditLogger` 是工作区本地链式日志，没有 Vault 式冗余设备，也没有把审计写入作为权限判定条件。因此本轮采用明确分层：错误密码和删除授权仍 fail-closed；本地 keystore 已成功落盘后，审计 sink 故障不得把已完成状态伪造成 500。

## 失败优先发现

`/security/init` 先完成 keystore 初始化并设置密钥，随后直接写 `security.keystore_initialized`；审计失败会在 `mark_software_unlocked()` 之前抛出，导致 keystore 已存在但接口返回 500，下一次重试只能得到 409。错误密码的 `/security/unlock` 和 `/documents/delete-authorize` 也会因失败审计覆盖原本应返回的 403。

## 本轮修改

- `/security/init` 先提交本地解锁状态，再以 best-effort 写初始化审计；返回 200 和固定 warning，重试不会把已完成初始化误判为失败。
- 错误密码仍然不解锁、不创建删除授权，保持 403；审计失败只附加固定 warning。
- 审计异常详细文本只进本机日志，不回显口令或异常中的敏感文本。
- 不改变“未初始化返回 409”“未挂载/未解锁返回原状态”“正确口令解锁”这些授权契约。

## 验收证据

- 新增失败优先回归覆盖：初始化后审计失败、错误密码解锁审计失败、删除授权审计失败；旧实现会产生错误的 500/不确定状态。
- 安全/文档授权筛选 `14 passed, 1 warning`；安全与文档文件完整回归 `26 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1320 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是 Vault 式合规审计保证，也没有新增第二审计设备、审计 outbox、重试队列或告警指标。
- “审计 best-effort”只适用于当前本地观测副作用；授权条件、口令校验、工作区锁定和删除确认仍 fail-closed。
- 这不证明跨存储 exactly-once、真实 Tauri/UI/E2E 或密钥轮换质量。
