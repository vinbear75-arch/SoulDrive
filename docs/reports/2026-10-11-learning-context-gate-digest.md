# 学习上下文准入收据与最终 prompt digest

日期：2026-10-11；第三十二轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮检查长期记忆 gate 的 `decision_digest` 是否真正约束最终 prompt。现有 `decision_digest` 主要用于读取决策审计，但最终 prompt 入口此前没有校验上下文是否仍与 gate 生成时一致。目标是为 gate 生成的记忆集合和胶囊文本增加 prompt binding digest，并在最终构建时复算；上下文被改写、替换或收据失效时跳过长期记忆。

本轮不引入 Temporal/MLflow 等运行时，不修改记忆存储 schema、冻结评测集、gold、模型、索引或外部后端，也不把本机 digest 写成分布式事务证明。

## 外部机制与本地缺口

- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 的 lineage 机制强调运行记录要能回指实际消费的对象；本轮新增 `memory_prompt_binding_v1` digest，将项目、窗口、胶囊文本和结构化 memory items 绑定起来。
- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 的状态/重放身份作为 durable execution 参考；本轮只借鉴“重放前核对输入身份”的边界，没有宣称本地 SQLite 具备跨主机 durable execution。
- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 provenance/group 约束继续用于限定 memory identity；digest 只绑定本地上下文输入，不替代来源事实或时间有效性校验。

## B0 与 Candidate

### B0 反例

构造 gate 携带 `prompt_binding_digest=sha256:stale-binding`，但 capsule 仍包含同项目的记忆文本。旧实现完全忽略该收据，照常把内容放入 prompt；失败优先结果为：**1 failed, 124 deselected**。

### Candidate 实现

- `gate_memory_context()` 新增 `prompt_binding_digest`，采用 `memory_prompt_binding_v1`，覆盖项目、是否包含全局记忆、workflow、胶囊文本和结构化 memory items。
- `build_learning_context()` 在显式 gate 提供该字段时，按当前最终 capsule items、补充 memories 和文本重新计算，并用固定时间比较；缺失/错误 digest 会清空长期记忆注入并写出 bounded 状态提示。
- 旧的 `decision_digest` 保持不变，继续承担读取决策审计；新增 digest 专门承担 prompt 消费边界，不混淆两种用途。
- 兼容没有该字段的旧/直接调用上下文，继续使用已存在的 scope、accepted IDs、内容安全和文本 provenance 门；生产 `gate_memory_context()` 路径现在总会提供新字段。

## 验证证据

- 失败优先反例：**1 failed, 124 deselected**；修复后 digest 定向回归：**1 passed, 124 deselected**。
- memory gate、memory runtime、RAG prompt 组合：**12 passed, 134 deselected**；扩大组合：**174 passed in 1.90s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1758 passed, 1 warning，415.79 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前 SHA-256：
  - `core/memory_runtime.py` = `303F34ECA90132D02E6BE79C9853AEBC49361CA132F203BDE4D0447CB345DCB7`
  - `core/rag_engine.py` = `F06AE62F9B1A9A5504C1E2847B1FC3E8BD16487808548C2501D49E8CFC4FD8AF`
  - `tests/test_memory_runtime.py` = `0B8F4497C2560B13F2644035E96EB8B59C8205C7BF24541C967E056B203B8B97`
  - `tests/test_rag_prompting.py` = `51659B8D4ACC12653DBB4CB54E912F371EC62019A33824ABBE6345BCA3987137`

## 边界与下一轮

本轮只证明本机 gate 收据与最终 prompt 的输入集合可做内容 digest 闭合；不证明跨进程/跨主机 receipt、外部 Redis/PostgreSQL 事务同步、历史上下文迁移、密码学签名、长期记忆开放式答案质量或真实 Tauri/UI/E2E。Starlette/httpx 弃用和 Vite 单 chunk 体积仍为非阻断警告。

下一轮继续审计 durable receipt 的 schema/version 与 prompt binding digest 是否在失败恢复、重放和旧事件兼容路径中都保留；优先构造“memory sync/恢复成功但上下文收据被旧版本覆盖”的反例。
