# 新 sidecar + 默认 Ollama：隔离项目真实链路诊断

日期：2026-10-13。轮次：48。初始状态：执行前固定功能合同，尚未运行或宣称通过。

当前结果：新 sidecar 的隔离 HTTP/真实 Ollama 项目链路通过；本轮只修正验证入口，不修改生产代码或模型配置。收尾时 goal 工具返回 `usageLimited`，不是 `complete`；不再启动新一轮优化，已将本轮在途完整回归跑完并留档。下述工程通过不代表全部优化完成。

## 依据与固定条件

按 [Ollama embed API](https://docs.ollama.com/api/embed)、[generation API](https://docs.ollama.com/api/generate) 与[驻留模型清单](https://docs.ollama.com/api/ps)核对当前适配器和模型资源，不新增框架。实际聊天适配器使用 `/api/chat`，不是把 `/api/generate` 当作已调用接口。沿用第四十七轮的新 exe：SHA-256 `a34ebb84503832326561870fc55ce6888cc2248130c42ff9d0deb9a354b1ca64`，尚未替换旧发行资源。

只读前置快照：Ollama `0.40.3`，无驻留模型；GPU RTX 5060 Laptop，491 MiB 已用、7409 MiB 空闲，驱动 616.92。默认聊天 `qwen3.5:9b` digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`，默认嵌入 `qwen3-embedding:4b` digest `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`；均已安装，不下载模型，不主动卸载其他模型。

本轮数据只是在全新输出目录生成的**合成工程 fixture**，沿用既有数值检查用例：项目 A 的 `measurement.md` 为 `-5%`，项目 B 的同名记录为 `+9%`，明确写“仅供链路测试，不代表真实实验”。不把它们包装为业务语料、gold 或质量分数；原冻结集、30 条待审核种子及四条标签草案均不读取用于模型回答、评分或调参。

## 在运行前固定的验收

1. 新 exe 在独立 workspace/端口/随机 token/虚构口令下启动，使用现有默认 Ollama 生成与嵌入，保持原模型、提示词、seed/预算、Top-K 和门控阈值。
2. 通过真实 HTTP 创建两个隔离项目，分别导入同名不同内容资料，再用真实独立 indexer 完成索引；记录 run_id、终态、逐文件错误及实际阶段日志，不将初始 idle 当完成。
3. 项目 A 会话只选精确文档 ID；请求选择 B 的文档必须拒绝。正常回答应由 A 的证据支持且带定位引用，不得把 +9% 当作 A 的事实。同 request_id 回放结果不变。
4. 用相同 A 资料生成实际持久化 PPT 大纲；确认前无 PPTX，确认绑定 digest、重复确认复用同一生成任务；文件结构与证据映射可核对，不自动打开 WPS。
5. 保留原始回答、请求身份、来源 ID、实际索引/PPT输出与日志。任何失败先定位层次，不能换题、改预期、关图谱或修改模型参数来制造通过。
6. 若修复生产代码，补失败优先回归、交叉及全量检查，并重建新 sidecar再跑同一 fixture。源码与冻结包混用不作为修复结果。

B0 为当前新 exe/默认配置在这些固定输入上的首次真实运行；只在出现可复现缺口后设计 B1/Candidate。确定性协议修复不为凑 B2 添加自由 Agent 或新数据库。时间只作阶段诊断，不是 P50/P95；正常回答的一个工程实例不代表开放式 RAG、GraphRAG 或长期记忆准确率。真实 Tauri/WPS、安装包、用户业务 gold 和完整实验比较仍须分别验收。

## 执行记录

统一候选根目录：`build/release-candidates/current-source-20261013-120726/`。运行使用 `souldrive` Conda 环境及 UTF-8；本轮实际 shell 为 PowerShell **7.6.5**，不冒充已验证 5.1。所有工作区、口令、token、项目、索引、审计与产物均为该目录内的隔离实例；未触碰用户真实项目，未调用真实邮件/日历或 WPS。

### B0：保留失败，定位验证层

首次实际运行保留于 `ollama-project-flow-b0/`，进程退出 1，`report.json` 为 `fail/AssertionError`。这不是一轮已通过的主流程。

- 失败前实际已完成两文件索引（2/2、4 chunks、无文件失败），两个项目各自建立图索引；跨项目文档选择返回 409，A 会话回答为 `-5%` 且引用只属于 A。
- 验证入口最初直接比较“首轮 HTTP 流全部字符 = 同键回放全部字符”。首轮原始流有 **6 个 U+200B** 保活前缀；服务端 `event_generator()` 在等待模型时发保活，但不把保活加入消息正文。已完成回执回放的是落盘正文，不重放网络等待过程。
- B0 审计实际记录一次 `rag.completed`、一次 `chat.replayed_idempotent` 及两次 `chat.completed`。结合服务端实现与原始流，失败位于检查脚本的观察对象，不是生产侧重复检索或正文丢失。没有为迁就断言而删除服务保活、改题、关闭图谱或调整模型。
- B0 报告的 `model_inference_tested=false` 是整个项目 flow 未成功返回时的保守总标记；不能据此反推该次没有实际模型调用。

### 最小验证入口修正

沿用 ponytail 的最小改动原则，仅扩展既有标准库入口 `scripts/smoke_packaged_sidecar.py --project-flow`：真实项目创建/导入/独立索引进程/限定文档问答/持久化大纲确认/生成均通过已有 HTTP 接口，不引入 Agent 框架或新依赖。

幂等检查改为通过受项目约束的历史接口读取权威消息，再同时断言：

1. 首轮流以完整落盘回答结尾，额外前缀的每个字符只能是 U+200B；不使用通用 trim 或删正文字符。
2. 同 request_id 回放与**落盘正文逐字符相等**，历史记录在回放前后完全相等。
3. 隔离审计只有一次 `rag.completed` 和一次 `chat.replayed_idempotent`。

这是区分传输保活与业务消息，没有改动冻结业务指标、标签、事实预期或生产行为。新目录保护、禁用 Python `-O` 保护、独立 workspace 校验与自有子进程退出清理继续保留。当前入口 SHA-256：`7541e4f98a57a0a391d2345f3c2d1c117b6418ba1db75e25628f439f802219b2`。

### Candidate1：实际完成的链路

第二次运行保留于 `ollama-project-flow-candidate1/`，实际命令退出 **0**；`report.json` 为 `pass`、`model_inference_tested=true`、`desktop_or_installer_tested=false`。

| 合同 | 实际结果 |
| --- | --- |
| 独立安全与重启 | 新库启动锁定、无 token 401、锁定 423、未确认初始化不建 keystore；显式初始化后错误口令拒绝、正确口令解锁并锁回；已有库重启仍锁定。两个服务进程均正常退出 0。 |
| 真实摄取与索引 | run_id `d7942410-62ca-4fd8-ab2a-f07460964240`，终态 `completed`，2/2 文件成功、4 chunks、失败列表为空。等待的是匹配 run_id 的终态，不是启动前 idle。 |
| 项目/同名资料边界 | A/B 各有一个 `measurement.md`，文档 ID 不同且分别属于各项目。A 会话选择 B 的 ID 被 409 拒绝；正常问答仅选 A 的精确 ID，回答含 `-5%` 与 `[E1]`，不把 B 的 `+9%` 写成 A 的事实。 |
| 回答来源 | E1/E2 均为 `project_id=flow-a`，带版本 `version_daaaee4d105148aaa9196f42630f54a7` 与 `source_locator=lines:1-7`。这是两个 chunk 证据，不是两个独立来源。 |
| 正文幂等 | 首轮有 5 个保活前缀；回放与落盘正文相等，历史不新增，审计只有一次 RAG 完成。这里只证明已完成请求回放，不证明进程崩溃时 exactly-once。 |
| 真实大纲确认 | 计划任务 `task_a0a3d2151014de8e588cec44` 持久化至 `outline_review`；确认前没有 PPTX；确认绑定真实 `plan_digest`，重复确认复用生成任务 `task_0902e43986f0373c444bf8cc`。 |
| 真实文件生成 | OpenXML 生成实际 **3 页** PPTX（请求的 `max_slides=4` 为上限）；ZIP CRC 检查通过且有 `ppt/presentation.xml`。来源覆盖状态 `exact_object_ids`，记录精确文档与资产版本 ID，不再仅靠显示名计覆盖。 |

产物为该隔离 workspace 内 `data/presentations/工程链路验证-34fd456417da573c.pptx`，10,151 bytes，SHA-256 `5fa9c3aba6dcc39ac19685ffdcafb5d23792ff017cb89f4e317e1e3dacee0e2e`；相邻 `.pptx.souldrive.json` 为持久化侧写，不是 WPS 打开证明。`opened_in_wps=false`；未检查中文版式、视觉或编辑行为。

图索引日志显示 A/B 各一份项目索引、各一个 community，all_projects 的 scope manifest 完整、2 documents/4 chunks。这个微型 fixture 的 `target_range_ok=false`，`report_methods=[]`；不能把“图摄取完成”写成 GraphRAG 检索/全局综合质量达标。文档列表的 `index_freshness=unverified` 也没有被当作新鲜度通过证据。

### 时间与身份核对

- B0 索引 81.0948 秒，Candidate1 36.6074 秒；后者复用了已加载模型，前者开始时无驻留模型。**运行条件不等价，不能声称性能提升、消融增益或 P50/P95 达标**。冷启动 1047.0/1031.0 ms 也只是两个隔离服务样本。
- 两次 A 输入均为 158 bytes，SHA-256 `4832282b47b782f09042e882786511bed24d13c062273fe5941490eea8529243`；两次 B 均为 158 bytes，SHA-256 `51b7d129c4b02cab3437733d4cc4050895b2abd7cb0ad4083b9cad8303463fe6`。没有换输入或调整事实预期。
- 重读构建前清单的 86 份生产/入口源码，当前指纹全部相等，新 exe 指纹未变；本轮无生产修复，不需要把旧二进制冒充新修复包。旧 sidecar `17278d4e…0a5dce` 与旧桌面 exe `e22637a1…6a79ad9` 指纹也未变，未覆盖发行资源。
- 结束后自有服务/indexer PID 58836/57948/65272 与 B0 的 52552/40060/47532 均不在运行；未终止其他用户进程，也未主动卸载模型。
- 原 30 条 seeds、manifest、四条独立标签草案 SHA-256 仍分别为 `ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424`、`752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d`、`0052d2576abd896bc358dca130f7ee69f6ed9cc1ac383eb14218f21908ec84a7`。本轮未冻结、评分、训练或调参。

## 完整工程回归

`scripts/check.ps1` 已等到实际终态，命令**退出 0**，不是仅凭日志出现百分比或某阶段完成判断。

- 后端 `1783 passed, 1 warning`，449.64 秒；唯一警告为 FastAPI/Starlette TestClient 的 httpx 弃用提醒，本轮未为消警告更改依赖。
- 既有冻结工程合同门：记忆 7/7 场景、8/8 查询；GraphRAG route 4/4；研究窗口角色 6/6；episodic runtime 路由 5/5。小型固定规则门不是长期记忆/论文评审的开放式质量分数。
- compileall、前端生产构建（6.47 秒）、8 组前端脚本共 `66 passed`、锁定 Cargo 检查（0.94 秒）均完成；前端脚本不是实际 Tauri 点击。
- 完整原始日志 `build/quality-gates/check-round48-20261013-124800.log`：1086 行、36,879 bytes、SHA-256 `15fa14ac51ab7b041ea3ba477fcd0e672b0b9607664671653bfe364915159779`。工具回传虽截断长 JSON，原始日志完整保存，四门逐样本输出可从该日志复核。
- 全部检查结束后再次核对 86 份构建生产/入口源码及验证入口，指纹均未变；30 条原种子仍为 gold 全 null。未把该完整回归写成业务集冻结或发行/UI 验收完成。

## 尚未修复的真实缺口与后续边界

本轮真实数据揭示了**同名文件索引诊断串映射**：索引逐文件记录 A 的 `elapsed_ms=11245`、B 为 `12252`，但两个项目文档列表中的 `index_observation.elapsed_ms` 都为 `12252`。当前 `_indexing_diagnostics_by_source()` 只用公开 `source_filename` 作字典键，后一个同名记录覆盖前一个；这部分显示仍不准确，不能据此比较项目性能或判定某文件的诊断身份。回答/选择边界通过不代表诊断展示边界已修复。

恢复 goal 后优先为该共享投影的跨项目同名场景建立失败优先回归，再把稳定文档/项目身份贯通到诊断生成及读取；不得只给某个 UI 调用临时加名字前缀，或修改本次原始日志。新生产改动后仍须重新构建并跑相同工程 fixture。当前因 goal `usageLimited` 未开始该修复，未将其写成已完成。

真实 Tauri UI/发行继承变量隔离、取消与崩溃恢复、安装包和 WPS 中文布局/编辑仍未验证；真实实验比较、不同研究窗口与长期记忆的开放式业务质量也未由此证明。业务 gold/指标/划分仍按原人工门推进，不把两个合成工程文件扩充为未经批准的业务评测集。

## 可复跑入口与原始证据

```powershell
conda run --no-capture-output -n souldrive python scripts/smoke_packaged_sidecar.py --executable build/release-candidates/current-source-20261013-120726/dist/souldrive-sidecar/souldrive-sidecar.exe --output-dir build/release-candidates/current-source-20261013-120726/ollama-project-flow-new --project-flow
```

复跑必须使用**尚不存在的新输出目录**；本命令会在隔离实例中实际运行两个本地模型，须预留资源，不能把名称 `new` 当作可覆盖目录。

- B0 原始报告：`ollama-project-flow-b0/report.json`，SHA-256 `8c82558c4ea10e45e17927e65f6f2c61636fff6d009165b554801f11bcc763bf`；其失败日志与原始 `answer.txt` 均保留。
- Candidate1 原始报告：`ollama-project-flow-candidate1/report.json`，SHA-256 `e9bc5dfc1d18b3761043f1bfed6e71f0ee35c81e3d64da6e1b04c022fffc98e6`；报告含完整回答、回放、来源、索引与任务结果，不从摘要反造证据。
- 两目录内 `workspace/audit/audit_log.jsonl`、`workspace/runtime/runtime_state.json`、`app/runtime/sidecar.log`、`process-*.log` 和候选根目录的两次 `ollama-project-flow-*.log` 均为实跑留档。
