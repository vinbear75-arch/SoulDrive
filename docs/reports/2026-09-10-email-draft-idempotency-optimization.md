# SoulDrive 邮件草稿幂等键优化记录

日期：2026-09-10

## 本轮结论

日程草稿此前已有幂等键，但邮件草稿入口没有把幂等键传入已有的 `artifacts.idempotency_key` 唯一约束。网络超时后重试会创建另一份邮件草稿，既增加用户清理成本，也会让后续确认对象不明确。

现在邮件草稿 API 接受 `idempotency_key` 并交给 `ProjectCatalog.create_artifact`：同键同输入返回同一 `artifact_id`，同键不同输入返回 `400`，数据库中不产生第二份草稿。前端以项目、收件人、主题、正文和附件引用构成输入指纹；网络失败重试沿用同键，用户改变草稿内容时生成新键。

## 外部实践依据

- Inngest 将事件/函数幂等键绑定到业务输入，并明确区分“重复同一事件”和“新的业务事件”：[Inngest idempotency](https://www.inngest.com/docs/guides/handling-idempotency)。
- Inngest 的 durable execution 将成功步骤结果持久化，重试时跳过已经完成的步骤；SoulDrive 本轮将同一原则映射为本机 artifact 唯一键和可重放草稿记录：[Inngest durable execution](https://www.inngest.com/docs/learn/how-functions-are-executed)。
- LangGraph 的公开文档要求副作用放在可重放任务中，并在重执行时使用幂等键或检查已有结果：[LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)。

本轮没有引入队列或工作流框架，直接复用项目目录已有的 SQLite 唯一索引和输入摘要比较；邮件发送仍是单独确认后的另一条副作用链。

## 实现范围

1. `EmailDraftRequest` 增加可选的 16–128 字符 `idempotency_key`。
2. `/drafts/email` 将该键传入 `ProjectCatalog.create_artifact`，复用同键同输入重放、同键异输入拒绝和并发插入竞争处理。
3. UI 为邮件草稿维护输入指纹和幂等键；草稿内容变化会换键，网络失败且输入不变时保持原键。
4. 不把邮件草稿幂等键用于真实发送；发送仍使用已确认草稿对应的独立 delivery key、项目范围和人工确认边界。

## 验收证据

- 先添加失败回归：旧 API 忽略请求中的 `idempotency_key`，同一键两次创建得到不同 `artifact_id`。
- 修复后 `tests/test_topic5_drafts.py`：`14 passed, 1 warning`；覆盖同键重放、同键换内容 `400` 和目录中仅保留一份 artifact。
- `souldrive-ui/scripts/document-scope.test.mjs`：`14 passed`；覆盖 UI 发送输入绑定幂等键和按输入变化换键的代码接线。
- 随后的仓库级 `scripts/check.ps1`：后端 `1062 passed, 1 warning`；前端生产构建通过，7 组 UI 测试 `48 passed`；`cargo check --locked` 通过。外部发送、真实 SMTP 和日历写入仍未由自动测试触发。

## 边界与后续

- 当前键是客户端请求级幂等键，不是跨机器协调锁；本机 SQLite 唯一索引只覆盖当前工作区。
- API 客户端若主动为每次重试生成新键，仍会得到多份草稿；调用方必须在可能重试的同一输入窗口内保留键。
- 其他已有 artifact 入口仍需逐个核对是否传递输入绑定键；不能因为邮件草稿补齐就宣称所有写入口都具备相同契约。
