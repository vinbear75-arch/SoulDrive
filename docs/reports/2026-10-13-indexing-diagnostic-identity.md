# 第四十九轮：索引诊断的文档身份闭合

日期：2026-10-13。执行前合同：先记录 B0，再做候选；本文件不把计划或历史通过当成完成证明。

当前结果：本轮身份修复、完整工程检查、新包内嵌代码核对及真实默认模型链路均通过，goal 保持 active。不是整项目、桌面安装或开放式业务质量已经完成。

## 当前缺口与外部机制

第四十八轮真实隔离运行中，两个项目各导入一个同名 `measurement.md`。逐文件索引耗时为 A=11245 ms、B=12252 ms，资料列表却都显示 B 的 12252 ms。当前索引观测只带显示名，`_indexing_diagnostics_by_source()` 以该名覆盖，失败与处理中分支也使用相同关联方式。

参考 [OpenLineage 1.53 命名规范](https://openlineage.io/docs/spec/naming/)的资源命名空间/唯一身份，以及 [MLflow get_run](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.html#mlflow.get_run)按唯一 run_id 读取的机制。本项目只复用现有稳定 document_id、project_id、catalog 绑定和本地 runtime 文件，不安装这些框架或新增数据库。

## 固定条件和验收

- 当前 HEAD `e9aa88c722c1a6ef61b25c191048caf1c0423da1`，保留原 dirty tree；不回退、不清理、不提交。
- B0 源码 SHA-256：indexer `c5681bb33f335f4072501c9b3d06e841b313f966a7ba9a015510d2196579050e`；MCP `331064d6ef1018ade6a64add3f3891180863985ac40b074b191754e5dde4fac5`；runtime_state `e2dbc6407c53b87835716b30631593c61e04767e00c61f49125eaaf4b6ab91db`。旧 smoke 入口 `7541e4f98a57a0a391d2345f3c2d1c117b6418ba1db75e25628f439f802219b2`。
- 使用 `souldrive` Conda + UTF-8；新增输入只是在临时库内创建的同名工程文件和虚构项目，不是业务 gold、训练集或新评分指标。业务种子、四条独立标签草案、冻结集和模型/检索参数均不改。
- 成功观测、解析失败、处理中、worker 异常退出都必须保留稳定文档/项目身份；匹配已知文档 ID，不能因显示名相同回退到其他对象。
- A/B 同名安全对象的观测分别读取；B 失败/处理中不得改变 A 的状态。嵌套明文同名文件及 catalog asset 身份也应准确；不是仅为加密入口打补丁。
- 旧无 ID 记录只在来源名无歧义时兼容；存在同名歧义或多个无身份观测时明确保持诊断未知，不猜第一条/最后一条。已存在外部文档 ID 或矛盾项目身份时不能降级按名字附会。
- 公开 runtime 保留新增的非敏感身份，但继续隐藏路径、原始异常、连接串与 token；不靠把错误正文公开来关联。
- 先跑新增反例记录失败，再跑同条件候选、相关回归、完整 `scripts/check.ps1`。生产修改后在全新目录重建 sidecar，复跑原 A=-5%/B=+9% 的真实模型工程链路，并核对每个列表诊断与相同 ID 的观测相等；不把耗时必须不同作为断言。

## 运行记录

- B0 新增 10 个反例实际 `10 failed in 1.34s`，退出 1，完整日志 `build/quality-gates/indexing-identity-round49-b0.log` 保留。失败包括 A 显示 B 耗时、B 失败/处理中同时污染 A、旧无身份歧义被附会、明文嵌套文件范围缺失、索引/worker 失败丢失 ID。
- 第一候选仅针对这些身份分支修正，实际 `10 passed in 1.06s`，退出 0；日志 `build/quality-gates/indexing-identity-round49-candidate1.log`。尚不能据此交付。
- 扩大来源身份审查时，发现新鲜度仍按显示名汇总哈希，且 catalog 的 basename fallback 可能把“已登记路径 A 的新内容”误绑到 B。现追加两个同名明文工程反例（A 改成 B 的字节 / A 改成全新字节）与旧式 progress 更新清理回归；先验证候选行为，再决定最小修正。不修改任何业务数据或评分标准。
- 第一候选扩大安全检查实际 `2 failed, 11 passed`，再核实 catalog 与入库身份后为 `3 failed, 11 deselected`：确实把 A 的明确路径误绑到 asset-b，并给两份同名同内容、不同资产的输入生成相同 ingestion key。两个失败日志分别为 `indexing-identity-round49-candidate1-safety.log`、`indexing-identity-round49-candidate1-safety2.log`，均保留；不能把此前 10 passed 当最终通过。
- 第二候选复用一个共享的来源身份解析：稳定 asset/document/project 与已验证内容版本分开；已登记精确路径不再因哈希变化借用别的 basename。明文新鲜度按文档 ID 取哈希，入库键也绑定稳定来源 ID。未伪造当前版本、未升级 schema/模型或增设框架。
- 第二候选反例实际 `14 passed in 1.44s`；索引、MCP 项目/状态、catalog、父子索引与安全检索交叉 `194 passed, 1 warning in 63.01s`，退出 0，日志 `indexing-identity-round49-cross2.log`。随后只把诊断查找写法展开便于审阅，并加强同一反例的“稳定归属不冒充旧版本已验证”检查；待最终全量覆盖。
- 将沿用既有 collect/metadata/hidden-import 清单在全新 `build/release-candidates/indexing-identity-20261013-r49/` 构建，不使用 --clean/--noconfirm，不裁剪依赖或覆盖旧资源。构建前后核对生产源码与验证入口指纹；更新 smoke 只新增逐文档诊断与相同 document_id/project_id 观测的对应断言，原 A=-5%/B=+9% 输入、请求与模型不改。

## 最终改动范围

沿用 ponytail 的最小实现原则，没有新增框架、数据库、模型请求策略或业务评分。四个生产文件的改动均围绕同一个来源身份因素：

- `core/indexer.py`：共享解析稳定文档/资产/项目归属，与已验证内容版本分开；成功、解析失败和处理中携带相同身份。入库 key 使用稳定来源 ID，避免同名同内容的不同资产相撞；已知项目或同名歧义时增量检查/清理也带 ID。来源解析缓存复用，目录查询异常仍进入逐文档失败边界，不串用上一文件身份。
- `core/runtime_state.py`：补两个可空的当前身份字段；清空 current_file 或旧式更新下一文件但不提供身份时，清理旧 ID，避免继承到下一文件。现有 run_id/终态门保持。
- `core/mcp_server.py`：成功、失败、processing 按文档 ID + 项目关联；明确身份不匹配时不按名称附会。收集完整资料清单后再做无歧义的旧记录兼容。明文诊断复用相同来源解析规则，新鲜度按文档 ID 的内容哈希检查；私有辅助字段不进入公开列表。worker 异常退出保留当时已知身份，公开错误继续脱敏。
- `core/project_catalog.py`：已登记的精确路径找不到匹配内容哈希时返回未验证，不回退到另一同名资产。没有精确路径的原有唯一 basename 兼容仍保留。源文件更新后可以识别稳定资产归属，但不能说旧 asset_version 已验证新字节；新增反例明确检查两者分离。

新增 `tests/test_indexing_diagnostic_identity.py`，并给既有 `scripts/smoke_packaged_sidecar.py` 增加 ID/项目与真实观测字段逐项对应的检查。没有以“两个耗时必须不相等”替代身份验证；合法情况下两份资料耗时可以相同。

## 完整工程检查：已等到终态

最终 `scripts/check.ps1` **退出 0**：

| 检查 | 当前实跑结果 |
| --- | --- |
| 后端全部测试 | `1797 passed, 1 warning`，460.75 秒；包含最终 14 个新增反例及稳定归属/版本分离断言 |
| 记忆规则门 | 7/7 场景、8/8 查询 |
| GraphRAG route 门 | 4/4 |
| 研究窗口角色门 | 6/6 |
| episodic runtime 路由门 | 5/5 |
| 其余工程入口 | compileall、前端生产构建、8 组共 66 项前端脚本、锁定 Cargo 均通过 |

完整原始日志 `build/quality-gates/check-round49-20261013-r49.log`：1086 行、36,875 bytes，SHA-256 `788e86c69e013f3a6d49733f0bd739d98face3128dfebf7c3a800653d0136ac8`。长工具回传虽截断，原始日志完整保存，四门逐样本可核对。保留 TestClient/httpx 弃用和 Vite 大块警告，未以升级依赖消警告。固定规则门不等于开放式长期记忆/论文评审质量，前端脚本不等于 Tauri 点击验收。

## 同一真实工程输入：旧包失败，新包通过

本轮候选根目录为 `build/release-candidates/indexing-identity-20261013-r49/`，以下路径均以该目录为根。真实测试继续使用第四十八轮的两份合成文件（A=-5%、B=+9%），没有换题、改事实预期、关闭图谱或调整模型/检索配置。

### 构建和内嵌代码

- 新目录沿用原 onedir 清单，构建命令退出 0，388.18 秒。PyInstaller 6.20.0、hooks 2026.6、Python 3.10.20；实际 PowerShell 7.6.5，不称已验证 5.1。
- 新 exe SHA-256 `02e1d282b6788cf57ec18077cda1c8d8ad51210d6d999df7b4549dc76c51b000`，67,683,549 bytes；完整目录 9811 文件、3,843,589,411 bytes，未裁剪依赖或宣称解决 NSIS 大小限制。
- 构建前后 86 份生产/入口/打包/渲染文件及两份验证入口指纹相同。真实流程结束后再次核对同一清单，仍无差异。
- 只读打开新 exe 的实际 PYZ（13358 modules），11/11 关键模块与当前源码编译代码对象相等：既有 answer_quality/RAG/MCP/runtime/PPT/receipt 等 8 项，加 indexer、runtime_state、project_catalog。本次不是仅检查函数名或编译源码。
- 可选 TensorRT、onnx、server/CLI、llama_cpp native 导入和 bitsandbytes 多平台 DLL 等构建警告原样保留。默认 Ollama 通过不证明 GGUF/CUDA 旁路或所有可选后端可用。

### 真实旧包负例

新验证入口先运行第四十七轮 exe（`a34ebb84…1ca64`），输出 `old-exe-b0/`。实际退出 1，停在 `index_diagnostic_identity`；索引本身 2/2 成功、无文件失败，但观测缺少文档/项目身份，且列表再次复现错绑。

| exe / 项目 | 逐文件真实观测 elapsed_ms | 该项目列表 elapsed_ms | 结论 |
| --- | --- | --- | --- |
| 旧候选 A | 17528 | 14836 | 错用了 B 的记录 |
| 旧候选 B | 14836 | 14836 | 不能因 B 恰好正确就说整体通过 |
| 新候选 A | 11852 | 11852 | 按 A 的实际 document_id/project_id 逐字段一致 |
| 新候选 B | 19880 | 19880 | 按 B 的实际 document_id/project_id 逐字段一致 |

旧包失败是实际产品缺口的负例，不是新包回归通过；其报告的 flow 为未完成，未执行聊天/PPT。失败后验证入口终止了自有服务，服务退出码 1 不单独解释为应用崩溃。原始报告、索引、审计和日志全部保留。

这些毫秒数仅用于核对观测归属。模型冷热、处理顺序和并行构建条件不等价，**不作性能提升、P50/P95 或配对消融结论**。

### 真实新包完整通过

`new-exe-candidate/` 实际命令退出 **0**，报告 `status=pass`、`model_inference_tested=true`、`desktop_or_installer_tested=false`。

1. 用独立 workspace/随机 token/虚构口令真实启动两次；新库待初始化、已有库重启仍锁定，缺 token 401、锁定 423、未明确确认不建 keystore，错误口令拒绝、正确口令解锁并锁回。两个服务正常退出 0。
2. index run_id `197c1bba-e569-4cbe-b105-d6d8b68c8990`，终态 completed，2/2 文件、4 chunks、失败为空。A/B 观测均有自己的 ID 与项目，列表字段逐项匹配，current_file 与两个 current ID 终态均为空。
3. A 会话选 B 的文档 ID 返回 409；正常 A 问答含 -5%，E1/E2 只属于 A，版本为 `version_4701f9e5534d466f98ee88e1cd0f217c`，定位 `lines:1-7`。这两个证据是同一来源的两个 chunk，不是跨文档综合成绩。
4. 首轮 5 个 U+200B 保活之外正文与落盘内容一致，同 request_id 回放逐字符等于落盘正文，历史不新增；实际审计为一次 rag.completed、一次 chat.replayed_idempotent、两次 chat.completed。不是断电或跨系统 exactly-once 证明。
5. 真实计划保存为 outline_review，确认前无 PPTX；确认绑定 digest，重复确认复用同一生成任务。OpenXML 实际输出 3 页（max_slides=4 为上限），CRC/结构检查通过，来源覆盖为 exact_object_ids。没有打开 WPS，步骤 open_in_wps 的 completed 在这里是显式不打开的空操作。

产物为 `new-exe-candidate/workspace/data/presentations/工程链路验证-1ef47a4e1b114a78.pptx`，10,151 bytes，SHA-256 `25b7f345c8553e9e19733350294f6e965d8aacec56723c2c75007b9792ecc5d7`；相邻 `.pptx.souldrive.json` 为持久化侧写，未作为视觉或编辑验收。

运行前模型清单无驻留模型；本机 RTX 5060 Laptop 8151 MiB，驱动 616.92，瞬时已用 702 MiB。默认聊天 qwen3.5:9b digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`，嵌入 qwen3-embedding:4b digest `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`，与上轮相同；未下载、调参或主动卸载模型。结束后只读查询 Ollama 版本为 0.40.3。上述不是逐调用权重校验或稳定显存/延迟测量。

两次输入逐字节相同：A SHA-256 `4832282b47b782f09042e882786511bed24d13c062273fe5941490eea8529243`；B `51b7d129c4b02cab3437733d4cc4050895b2abd7cb0ad4083b9cad8303463fe6`。这些工程文件不是真实实验，也没有被加入业务 gold。

## 指纹与交付边界

| 当前证据 | SHA-256 |
| --- | --- |
| build.log | `18d1e6ffc3753dde33c197fd4b9940931a0777278fb82035a4f3d88c25fe51d7` |
| embedded-code-check.log | `908ab2427c29c929226f397535e8f8ab2cb0dbc2477e7ab82c5e6311e08efa5b` |
| old-exe-b0/report.json | `d871966c46802d130324c2c9fbf6559681e0db56d088a9c647f0ea72a463e241` |
| new-exe-candidate/report.json | `663c24259c4937a980f78f63cc6e9577aa705f85573ba0517357157bff9869a9` |
| core/indexer.py | `8d3db397df2ccf83fa7d61b9c3955d265211e28db83a6859f1eff630311ba105` |
| core/project_catalog.py | `305dcfdb662e22dc2b6b0efc09853178fc217d51910cc18bb0f107ee51f25f65` |
| core/mcp_server.py | `09112fd51cc0cdf7565115ba2a5858c66121e0603b4474b966301ebc3ff26280` |
| core/runtime_state.py | `1b61e6893b57165a37d5b72fee46f8c02d9f0deff6954405784364680497835b` |
| scripts/smoke_packaged_sidecar.py | `16531ce23b57d605f680b0297955348a91c60ea6b50a3fd15918e6646855d170` |
| tests/test_indexing_diagnostic_identity.py | `41fd80d14cc8f79efa5c79be3f11eb6dfbad7f65125b715ab8073f8da4149f49` |

原旧 sidecar、旧 Tauri release 和第四十七轮候选 exe 指纹仍分别为 `17278d4e…a5dce`、`e22637a1…79ad9`、`a34ebb84…1ca64`，未覆盖或激活。自有服务/indexer PID 43480/38528/40336/70152/43516 结束后均不存在，没有停止其他用户进程。磁盘保留所有候选、反例与日志；完成时约 30.3 GB 可用，没有删除旧包腾空间。

原 30 条 seeds、manifest、四条独立标签草案仍分别为 `ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424`、`752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d`、`0052d2576abd896bc358dca130f7ee69f6ed9cc1ac383eb14218f21908ec84a7`；30 条 gold 全 null。未冻结、评分、训练或调参。

本轮只在临时库验证旧记录兼容，没有演练用户旧索引全量升级；解析/异常退出反例含 fake KB/受控进程句柄，不冒充 OS 崩溃恢复。微型图摄取仍有 target_range_ok=false，不是 GraphRAG 全局综合质量达标。实际 Tauri 发行继承变量隔离、安装、真实 UI 交互、WPS 中文布局/可编辑性、完整实验比较主线、研究窗口/长期记忆开放式质量仍须分别闭合。

goal 当前实际状态为 active；工具目标文字为 `resume`，按用户此前完整 SoulDrive 优化范围继续，不以恢复状态或本轮通过代替全项目完成。下一轮优先将真实实验比较纳入新包的项目流程，再补研究窗口、记忆与交付验收；业务数据人工门继续遵守。
