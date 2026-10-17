# 自动记忆的真实消息来源与管理器表达

## 基线、参考与边界

B0 为 `08e1f99b6fe8ad6ddd81e112ef734c472f3389d5`，开始时工作区干净。检查真实调用链：前端在聊天流完成后，把原问题、会话和同一请求键交给 `/learning/memories/runtime`；该接口仅把会话/工作流写入候选，未复用已存在的 `find_message_by_request_id` 绑定消息主键。候选的 `source_message_indexes=[0]` 是本次单条输入中的位置，不是归档会话序号；管理器却显示“会话消息 0”。反思审核路径已有真实消息绑定，不能由其单测通过推定自动记忆也已绑定。

参考 [Graphiti 的 episode 引用和有效时间字段](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)、[Graphiti 原始 episode 与 reference_time 示例](https://help.getzep.com/graphiti/getting-started/quick-start)，借鉴来源与有效期分离的表达；参考 [Copilot 记忆与聊天历史的独立管理](https://learn.microsoft.com/en-us/microsoft-365/copilot/copilot-personalization-memory)，不把会话上下文误作已核验原文。本轮不引入其框架、云服务或图数据库，不改变现有时间口径和保存/撤销制度。

## 本轮验收条件

1. 独立虚构会话里存在两条相同偏好时，按请求键绑定正确的用户消息主键与真实序号，不按文本取第一条、不借 assistant 消息或其他会话。
2. 同一已归档请求的正文不一致时，在写入记忆/回执前拒绝；来源读取异常不能被当成“未找到”绕过。
3. 独立直接提交且没有归档原消息的明确偏好，保留现有 API 能力，但不伪造消息主键；管理器明确说明原消息定位未核验，不把候选局部位置标为会话序号。旧记录不批量改写。
4. 来源字段在事实源、加密归档、幂等重放和修订/撤销/重启后保持正确；现有项目/工作流/有效时间门不放宽。
5. 定向失败优先、实际 React 渲染与完整工程检查分别验收；另用隔离假资料实际服务流程检查记忆生命周期，不用工程样例代替长期记忆业务评测。

业务种子、四条标签草案和冻结评测集不改、不评分/训练/调参。本轮不操作桌面，不重新索要标签审核。完整项目仍未完工。

## 失败优先与最小修复

新增两个 API 反例首次均失败：保存记录缺少 `source_trace.message_ids`；与原请求不一致的正文返回 200 而非 409。前端新增三个实际组件检查首次失败（原四个 UI 检查仍通过）。修复只复用既有 `find_message_by_request_id(session_id, request_id, role="user")`：有归档消息时正文必须精确一致，并保存原消息 ID / sequence；读取异常返回固定 503，缺失匹配则保留独立明确偏好提交能力，不按相同文本猜测另一请求。判断在记忆/回执写入前，不新增来源索引或框架。

管理器共用一个来源组件，展示来源会话、工作流、真实主键/序号；无真实消息主键的旧记录或直接输入显示“原消息定位未核验”，局部位置明确为“候选片段第 N 条（非会话序号）”。React 按文本渲染摘录，HTML 不执行。不批量改写旧记录、原内容、作用域或有效时间。

后续新增来源读取故障和同文不同请求的正反例。记忆 API / LearningStore / Runtime / 会话存储组合 **381 passed, 1 warning / 87.21 秒**，日志 SHA-256 `2152f95c438e7c5f8600dbe95d13a473436fa56b7920ae3794bd31f2d4cf77d4`。实际前端来源组件与既有学习入口合计 **7 passed**。

## 真实流程及边界

复用公开 smoke 脚本，增加独立 `--memory-flow` 和明确的 `--source` 入口；输出目录必须不存在，只使用新建虚构工作区。源码模式记录 core 文件指纹，并在结束时核对未变；报告显式区分源码/打包，不改既有项目/实验流程默认行为。

第一次真实旧包流程错误地在无论文/未审核的论文会话发偏好，正确得到输入未就绪的 422；两次前置失败均保留，不算产品来源验证。改用普通聊天记录来源，不关闭论文/实验门。旧 R56 包（SHA-256 `7c63d022...`）实际保存偏好后复现缺失消息主键，报告 `memory-round57-package-b0-source/report.json` SHA-256 `6fee819c35d6c24c028005cef77a907ec9999e85188e03b40f2eaa6cf12f4745`，保留 fail。

首次新源码生命周期测试错误地把检索返回的 `asset_id` 当成管理列表的 `memory_id`，保留 `memory-round57-source/` 的测试启动器失败；修正测试身份读取，不改产品 API，然后另建目录重跑。

`memory-round57-source-identity/report.json` **pass**，SHA-256 `c1ea5128eb81004b8d9a7aa082394e2aa23d29adec72d880d9ed1ad9e9c321ff`：两次真实进程启动 1031 / 1047 ms，均退出 0；两条同文请求绑定第二条的真实用户消息、序号 3；同键保存重放、改写/跨项目拒绝、修改为 v2、修改同键重放、停用/同键重放、当前不再使用、指定历史时间找回 v1 和进程重启后原来源均通过。该偏好按现有规则属于 global，能在另一个研究模式检索是正确行为；`project_only` 项目检索为空，不把全局偏好应有的共享误当串项目。

本机禁用 Redis/PostgreSQL；本次实际检索 keyword_count=1、semantic_count=0、semantic_backend=null，报告 `model_inference_tested=false`、`desktop_or_installer_tested=false` 保持原值。当前 `_semantic_memory_scores` 依赖 Redis 的 `search_vector`，不能由本次本机关键词流程宣称向量记忆或长期业务质量已通过；这是下一项本机检索覆盖缺口。当前完整检查与新包独立验证中，不从源码 pass 推定发行已同步。

追加核对发现上述测试报告的模型标志只统计语义召回，遗漏修改时的实际 embedding。修正测试统计，保存修改响应的 `storage_sync`，不改产品链路；第三个独立源码目录 `memory-round57-source-final/` 仍 pass，报告 SHA-256 `5f84b4ab9869b99b7a48f459098f938d6286fa605ba53c16a40d2fb7ba5d9446`，两次启动 1047 / 1031 ms、均退出 0。修改返回 `vector_requested=true / file_archive=ok`，因此最终 `model_inference_tested=true`，它仅覆盖本机 Qwen embedding 调用，不证明聊天生成质量。检索仍 semantic_count=0 / backend=null；真实问题是“向量已生成保存，却没有本机语义读取分支”，不是“未生成向量”。`desktop_or_installer_tested=false` 不变，前两份记录不覆盖或改写。

可复跑工程命令（在 `souldrive` 环境，新输出目录）：`python scripts/smoke_packaged_sidecar.py --source --output-dir build/quality-gates/memory-independent-run --memory-flow`。验证包时用 `--executable` 指定本机服务 EXE，不能同时指定 `--source`。不把启动时间当完整问答 P95；本轮并行检查/构建，不报性能增益。

实际环境为 RTX 5060 Laptop / 8151 MiB、驱动 616.92。Qwen3.5:9b 摘要 `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`，Qwen3-Embedding:4b 摘要 `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`。普通聊天为空知识库的记忆来源记录流程，不是模型答案质量测试；本轮也不证明在途生成的记忆撤销、复杂多时点业务推理、真实 Redis/PostgreSQL 或实际桌面/安装器验收。

## 完整工程检查

PowerShell 7.6.5 / Conda `souldrive` 的完整 `scripts/check.ps1` 正常退出 0：后端 **1899 passed, 1 warning / 246.13 秒**，前端 **115 passed / 0 failed**，Python 编译、生产前端构建及锁定 Cargo 通过。原四小型工程门另核对，不能作为长期记忆业务准确率。完整日志 SHA-256 `568e0f503ff0cb5d39212d9fcd7a7ec57a3e28f34c0c1ff5f5d02d0c261e3bdd`。既有 Starlette/httpx 弃用、Vite chunk 大小警告保留；没有以并行检查耗时比较宣称性能提升。原 30 条种子、manifest 和四条标签草案再次核对原指纹不变。

之后只将来源的技术标识收进原生 `<details>`，默认显示绑定状态/真实消息序号，避免记忆卡片铺满内部 ID；原生展开具备默认键盘行为，无新依赖。再次生产构建及完整前端 **115 passed / 0 failed**，日志 SHA-256 `5c79c225b0b8685c189484772dde3d13d70d7d541bfe18e1c7b39c060ab2e667`。实际组件渲染另检查展开区默认关闭、原文按文本转义；不是桌面视觉验收。

## 新包实际生命周期

正常构建退出 0，服务 EXE SHA-256 `06c8662927ffafdc087c9fe100b47abbae01c12a6ad846b3a0c5d2dc184c082c`。八个验收相关模块（本轮增加 MCP API 核验）包内字节码与源码一致，核验 JSON SHA-256 `65d11ca629269075249f45579bb993958615e11e174b915fea0054aefee6730a`；构建日志 SHA-256 `f1e3f28136263422368dc87b6ba621bafd091021a44f6d6dc56ec38d4ecc5d10`。正常复用既有自有 work，无 `--clean`，仅更新默认生成包，不清理被拒绝的旧包/缓存目标，不计释放空间。

公开 `--memory-flow` 新包回放 **pass**，报告 `memory-round57-packaged/report.json` SHA-256 `2b50974ce57805bb8ed3731be260a3cb24f8cae2f31ac0b44b58949683bc884c`，绑定上述服务 EXE。两次启动 1562 / 1032 ms、均正常退出 0；冷启动锁定/token、初始化确认、错误口令拒绝、解锁/重锁/退出保持原保护。来源绑定第二条同文请求的真实消息 3、重放/冲突/跨项目保护、修改 v2、修改与停用重放、当前排除/历史 v1/进程重启找回均完成。修改实际得到 embedding 并归档，但检索仍 keyword_count=1 / semantic_count=0 / backend=null，本机语义分支缺口仍在。没有新 PPT/开放式问答质量通过结论；`desktop_or_installer_tested=false` 不变。

最终 Release `--no-bundle` 构建正常退出 0，Rust release 阶段 44.52 秒，EXE SHA-256 `d3f4fbe6bee3fc7e4ebd40831bdddb1b27191e91ce06bd11919109ed482ef97d`，构建日志 SHA-256 `81bb93680b454328ccedd1d7c4973bc10f5b93b8d67346541e39233088d8d40a`。Release Rust **7 passed / 0 failed**，正常退出 0，日志 SHA-256 `1d9da88d5f984957c2fdf0f4de1e8d823e0bd3853910ca570e4626f06f88a734`。本机链接器输出 warning 保留；本轮没有启动新 GUI、操作口令界面或运行安装器。

本轮工程来源闭环完成，完整 goal 保持 active。下一项为本机加密归档已保存向量的检索覆盖，须先证明作用域/版本/有效时间约束和独立反例，再谈语义收益；开放研究、长期记忆业务评测、性能及安装器仍未完成，不能由本轮工程通过推定。
