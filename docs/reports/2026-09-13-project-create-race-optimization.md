# 项目创建唯一键竞争与回放优化

日期：2026-09-13

## 发现

项目创建已经有持久化唯一 `idempotency_key`，但路由先查询、再插入，只用查询结果判断 `created/replayed`。两个请求在查询与插入之间竞争时，数据库虽然能把第二个请求收敛到同一个项目，路由却可能仍返回 `created` 并重复写 `project.created` 审计；若没有显式 `project_id`，竞争双方还可能先算出不同的默认目录。

## 公开实践对照

- [SQLite UPSERT](https://www2.sqlite.org/lang_UPSERT.html) 的核心是让唯一约束决定 insert/update/no-op，而不是把“先查再写”的窗口当作唯一正确性来源；本轮保留现有唯一约束和竞争后回读，并把结果状态传回路由。
- [torkbot/sledge](https://github.com/torkbot/sledge) 使用 producer `dedupeKey` 作为 durable event 身份，并在并发 worker 下维持同 key 的收敛；本地只借鉴 stable key 到结果/审计层的传递。
- [Inngest event identity](https://github.com/inngest/inngest/blob/main/docs/SDK_SPEC.md) 把事件 ID 作为一次处理的稳定身份；本轮将带 key 的默认项目 ID 派生为 key 的 SHA-256 前缀，避免并发请求各自生成随机目录。

## 实现

1. `ProjectCatalog.create_project(..., return_status=True)` 在既有记录、首次插入和唯一键竞争回读三条路径分别返回 `replayed=true/false`，默认旧调用仍返回字符串。
2. `POST /projects` 使用 catalog 的真实结果状态，而不是只看前置查询；竞争回放不重复写 `project.created`，响应返回稳定项目 ID和 `status=replayed`。
3. 当请求没有显式 `project_id` 但带有幂等键时，项目 ID 由规范化 key 派生；无幂等键的旧随机 ID 行为保持不变。

## 失败优先与验收

- 旧实现反例：模拟“前置查询为空、catalog 唯一键竞争后回读既有项目”时，响应把 dict 结果误当项目 ID并标记 `created`。
- 修复后覆盖同键同输入回放、异输入 409、竞争回读返回 replayed 且不写审计；项目/API 与 catalog 定向 `23 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1274 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机 SQLite 唯一键竞争和目录身份稳定化，不是跨机器分布式锁或跨文件系统原子创建。
- 定向测试验证了竞争结果处理契约，但未把长时间多进程压力测试作为本轮门禁；SQLite 锁等待/进程崩溃仍需独立恢复演练。
- 该证据不证明外部副作用 exactly-once、真实 UI 网络断线注入或项目内容/RAG 质量。
