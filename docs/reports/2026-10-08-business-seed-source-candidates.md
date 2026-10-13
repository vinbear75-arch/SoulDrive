# 30 个业务场景的明确许可来源候选

日期：2026-10-08。状态：**候选提案，尚未采纳或冻结**。

用户已批准另拟明确许可的官方开源资料候选，并保留全部 30 个业务场景。本文件只记录来源、固定版本、许可声明、建议用途和仍需审核的缺口。没有替换任何原始文件、冻结集、`source_refs`、提问或 gold；没有下载论文 PDF、数据集或模型，没有安装这些框架，也没有执行新的问答、评分、训练或调参。

## 1. 推荐的 8 个来源

选择依据是对本机单用户业务的任务覆盖和可核查许可，不是排名、星数或项目自报的效果。C01–C06 是官方仓库的特定文本/实现资料，C07–C08 是作者发布的固定版本研究论文；不能把二者混成同一类实验依据。

| 候选 | 固定版本及拟审核材料 | 明确许可依据 | 对 SoulDrive 的用途与限制 |
| --- | --- | --- | --- |
| C01 Microsoft GraphRAG | commit `769542fbf1d8e5b4c6a8677fefc34621c87894c5`；[查询概览](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/docs/query/overview.md)、[local](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/docs/query/local_search.md)、[global](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/docs/query/global_search.md) | 根部 [MIT LICENSE](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/LICENSE)；所选 Markdown 未发现单独许可覆盖声明 | 区分向量查找、实体邻域检索、社区报告综合。资料用于解释路由和派生证据，不证明本项目的图索引或回答质量；不取外链示例数据、图片或 notebook 语料 |
| C02 Haystack | commit `6ca8349091c09af89ec0090aa13cb22de1d88bfb`；[README](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/README.md)、下列四个组件文件 | 根部 [Apache-2.0 LICENSE](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/LICENSE)，四个所选代码文件均有 `SPDX-License-Identifier: Apache-2.0` | 分开讨论召回、融合、元数据过滤和校验回路。只作机制/实现证据，不安装框架，不把网站文档或外链数据自动纳入此许可 |
| C03 Graphiti | commit `17ed4b707320f092b5e79bdbd9d45130c7f84259`；[README](https://github.com/getzep/graphiti/blob/17ed4b707320f092b5e79bdbd9d45130c7f84259/README.md) | 根部 [Apache-2.0 LICENSE](https://github.com/getzep/graphiti/blob/17ed4b707320f092b5e79bdbd9d45130c7f84259/LICENSE) | 借鉴事件来源、事实有效时间和历史读取；动态记忆图不等于静态社区式 GraphRAG。不引入图数据库服务，现有本地时间合同也不自动改成它的语义 |
| C04 LongMemEval 仓库 | commit `9e0b455f4ef0e2ab8f2e582289761153549043fc`；[README](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/README.md) | 根部 [MIT LICENSE](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/LICENSE) | 参考跨会话、知识更新、时间推理及应拒答任务的评测接口说明；不复制其 benchmark 样本，不沿用成绩，外链数据许可另核 |
| C05 MLflow | commit `09bdb2cca50a1ab31feefd513ea059bfbd252269`；[README](https://github.com/mlflow/mlflow/blob/09bdb2cca50a1ab31feefd513ea059bfbd252269/README.md) | 根部 [Apache-2.0 LICENSE.txt](https://github.com/mlflow/mlflow/blob/09bdb2cca50a1ab31feefd513ea059bfbd252269/LICENSE.txt) | 参考实验运行、配置/参数、指标与产物的记录边界；不安装 tracking server。README 不替代业务指标单位、统计公式或实验可比性标签 |
| C06 Temporal Python SDK | commit `b3375504ee29bda57585a5eab7e11a7df39b3472`；[README](https://github.com/temporalio/sdk-python/blob/b3375504ee29bda57585a5eab7e11a7df39b3472/README.md) | 根部 [MIT LICENSE](https://github.com/temporalio/sdk-python/blob/b3375504ee29bda57585a5eab7e11a7df39b3472/LICENSE) | 工程参考：区分可重放流程与有副作用的执行步骤；不增加 Temporal 服务，不把本机 SQLite 收据宣称为跨系统 exactly-once。它不是这 30 题的通用 gold 来源 |
| C07 LongMemEval 论文 | Di Wu 等，*LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory*；[arXiv:2410.10813v2](https://arxiv.org/abs/2410.10813v2)，2025-03-04 | 该版本摘要页的 `view license` 指向 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 为真正的论文精读提供研究问题、评测设计和局限候选；与 C04 分开标识。论文许可不等于其数据集、模型权重或所有第三方材料许可；尚未逐题读正文定位 |
| C08 Self-RAG 论文 | Akari Asai 等，*Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection*；[arXiv:2310.11511v1](https://arxiv.org/abs/2310.11511v1)，2023-10-17 | 该版本摘要页的 `view license` 指向 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 为按需检索、自反思与实验设计提供论文候选。其训练反思 token 的方法不等于现有有界校验重试，也不直接证明应该上 Agentic RAG；不导入模型/训练数据，不宣称是会议终稿 |

Haystack 拟审核的四个文件均在上述固定 commit；链接与文件级 SPDX 已核对：

- [BM25 检索器](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/haystack/components/retrievers/in_memory/bm25_retriever.py)：召回与过滤接口，不据此改写本项目强制项目过滤。
- [DocumentJoiner](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/haystack/components/joiners/document_joiner.py)：融合策略，不把融合等同于语义重排。
- [MetadataRouter](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/haystack/components/routers/metadata_router.py)：路由与元数据约束，不意味着可以扩大授权文档范围。
- [JsonSchemaValidator](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/haystack/components/validators/json_schema.py)：结构校验/反馈回路，不把 schema 通过当作事实正确。

### 许可核验与可复现记录

GitHub 官方 API 按完整 commit 读取上述根部许可和所选文本；仅在内存中解码许可文件计算 SHA256，没有 clone 仓库或下载数据包。以下是**许可文件实际字节的 SHA256**，不是源码、论文或数据集的内容哈希，也不是 Git blob SHA。

| 候选 | 许可文件 SHA256 |
| --- | --- |
| C01 | `c2cfccb812fe482101a8f04597dfc5a9991a6b2748266c47ac91b6a5aae15383` |
| C02 | `3f0a3af7e9b0f3b6358aaa3c7501ab522a67dc5483b870e9a8b3db41ea42e249` |
| C03 | `2825300b20d7b951209835a4a331f29e24725a39d65168e4b831df53aa372650` |
| C04 | `d3c4b9aa54759df6ded337978a6f3b55b75615e5e4525c3b82d7e2627d4b9732` |
| C05 | `6395355de6f391afff35996a30fb41b189b4991a4cb54993ace35ab69a0bfa28` |
| C06 | `d7706f28d144dabf07e4946553a4d117d1dab5cda6bf34a602959b4a2fa86b38` |

Haystack 的自动 SPDX 元数据曾返回 `NOASSERTION`；这里的 Apache-2.0 判断来自实际 LICENSE 文本与所选代码的 SPDX，不依赖自动徽章。其余根部许可也不是第三方依赖、网站内容、外链模型或数据的总授权。

两篇论文的版本页及其许可链接已核实；OpenReview 页面要求人工验证，未绕过，也未把未核实的会议版本列为已确认来源。后续使用论文需保留作者、标题、原版本地址、许可链接和改动说明；[CC BY 4.0 的条件](https://creativecommons.org/licenses/by/4.0/)还要求适当署名并标识修改。下载后仍需检查第三方内容声明及内容哈希，本轮不提供法律意见或完整版权清理结论。

## 2. 保留 30 个场景的建议对应表

**下面是用途提案，不是已完成的正文支持核验、证据绑定或答案。** 现有提问仍指向原四份资料；如果采纳候选，需要另行审核文献名称、选中文档和干扰文档的替换方案，不能直接把同一题换引用后当作旧题已验证。保留的是已批准的业务能力范围，不保证新材料与旧材料逐句等价。

跨文档任务必须区分稳定资产身份、真正的独立来源与共同支持的具体事实；同一项目的三个 Markdown 文件是三份文件，**不是三项独立研究验证**。C04 与 C07 也不能因一个是 README、一个是论文就当作独立研究的两次验证。

| 场景 ID | 建议参考 | 保留的业务目标 / 仍需人工核对 |
| --- | --- | --- |
| SEED-RAG-001 | C02、C08 | 两份资料的检索/生成机制比较；共同支持、单来源观点和差异要逐条定位，不能凭标题填答案 |
| SEED-RAG-002 | C02 的检索/融合组件、C08 | 区分混合召回、融合、重排与生成；候选文件不保证覆盖全部重排术语，缺少部分应补来源或明确未知 |
| SEED-RAG-003 | C01、C03 | 保留原始证据与派生图摘要区分；静态社区图与动态记忆图目的不同，不强求两者共同定义社区报告 |
| SEED-RAG-004 | C02、C08 | 仅在两份已选资料内做综合；逐来源摘录不能算综合结论，新共同结论待正文审核 |
| SEED-RAG-005 | 已选 C02 的特定文件；未选 C01 作干扰 | 保留选中范围不足的场景；先核查选中正文是否支持，不能提前赋拒答标签，也不能偷偷引用 C01 |
| SEED-RAG-006 | C02、C08 | 只模拟相同显示名；来源 ID、版本、引用位置保持独立，不改真实文件名 |
| SEED-RAG-007 | 项目 A 的 C02；项目 B 的 C01 | 合成项目归属仅作待审设置；召回前项目约束和缺证据处理需在本地接口验证 |
| SEED-RAG-008 | C08、C02 | 讨论按需检索与有界校验；论文机制不直接等于自由 Agent 循环，是否增加复杂度必须经过本机 baseline |
| SEED-RAG-009 | C01、C08 | 保留对“所有查询均适用”断言的证据审查；两份材料未必共同支持 GraphRAG 的结论，不靠常识预填否定标签 |
| SEED-RAG-010 | C01、C02、C08 | 8GB 约束来自本机业务，不来自这些材料的实验；候选只能帮助设计验证路线，显存/延迟需另行实测 |
| SEED-PAPER-001 | C07 | 用真正的研究论文检查五部分精读输出；替换旧综述名称需审核，正文和缺失信息不由摘要补齐 |
| SEED-PAPER-002 | C08 | 保留任务、数据、指标、结果与运行条件核查；原“BIMSA 章节”不能自动映射为新论文章节，需逐段定位 |
| SEED-PAPER-003 | C02 的 README/组件说明 | 保留资料类型辨认：实现说明不是实测论文，不补造准确率或同行评审信息 |
| SEED-PAPER-004 | C01 | 原理/实现/应用/限制分开；文档示例不是本机运行，未提供的演示或配置应留缺口 |
| SEED-PAPER-005 | C07 仅作未选干扰 | 论文窗口未绑定文档时先要求选择，候选存在不代表已授权本轮读取 |
| SEED-PAPER-006 | C07、C05；现有窗口合同 | 同一句输入分别要求论文证据或两次运行；切换窗口要改变输入要求和轨迹，不伪造缺失的实验 |
| SEED-PAPER-007 | C07 | 保留未经核实的 12% 断言场景；数字是测试输入，不代表论文结果，正文及数值定义待核查 |
| SEED-PAPER-008 | C08、C02、C05 | 文献结论、本机候选假设、验证实验三者分开；不产生虚假 baseline 或因果证明 |
| SEED-EXP-001 | C05；既有合成运行输入 | 运行配置/数据/脚本与数值记录；单位及可比性标签仍需审核，参数不同不是因果证据 |
| SEED-EXP-002 | C05；既有合成运行输入 | 数据版本不同时的比较门；不拿 MLflow 文档替代业务可比性规则 |
| SEED-EXP-003 | C05；既有合成运行输入 | 对照配置与重复实验分开；不合并不同条件，也不从外部项目复制统计标签 |
| SEED-EXP-004 | C05；既有合成运行输入 | 保留重复次数与小样本限制；均值、方差/置信区间的定义和 `%`/比例解释仍需单独批准 |
| SEED-EXP-005 | C05；既有本地时间合同 | 保留“上个月”与同分候选澄清；不把 tracking 时间戳自动替代本地 `performed_at` 语义 |
| SEED-EXP-006 | C05；既有项目隔离合同 | 另一项目的 baseline 不能自动使用；候选只支持运行记录思路，授权仍由本地项目绑定决定 |
| SEED-MEM-001 | C03、C04、C07；既有合成输入 | 同类事实的项目隔离与来源定位；外部评测不替代本机 scope 合同 |
| SEED-MEM-002 | C03、C07；既有窗口合同 | 论文/实验窗口的适用记忆不同；窗口与项目组合范围仍待审，不静默改变原冻结语义 |
| SEED-MEM-003 | C03、C04、C07；C06 作工程参考 | 更新后的当前事实、历史记录与派生缓存分开；更新重试可借鉴副作用隔离，但不得据此宣称跨存储原子性 |
| SEED-MEM-004 | C03、C07；既有合成时钟 | 按有效区间读取历史；测试数字不是业务日期，外部双时间设计不自动改变本机 `as_of` 合同 |
| SEED-MEM-005 | C03、C04、C07；既有合成输入 | 保留明确偏好变更及冲突来源；只读场景，不创建日程、自动化或通知 |
| SEED-MEM-006 | C03、C07；既有合成输入 | 项目约束的适用范围与确认来源；外部记忆不是权限或指令优先级的替代品 |

实验/记忆的合成值及项目、窗口身份仍来自本仓库内部待审核设置，不变成这些外部项目的公开数据或论文实验。当前仓库未核实根部开源许可，故本提案也不授权合成输入外发。

## 3. 对齐业务后的优先级，而不是新增框架清单

1. **先补可审核证据**：C01/C02/C07/C08 覆盖图检索边界、检索/融合差异、真实论文与技术说明的区别。当前 GraphRAG 三路径回放仅 1/3 完成生成，全局/局部仍回退；候选清单本身没有修复这个质量缺口。
2. **再评记忆与窗口的真实用途**：C03/C04/C07 帮助明确知识更新、历史读取、拒答和跨会话能力；论文窗口绑定资料，实验窗口绑定可比运行，不凭窗口名称宣称业务闭环完成。
3. **按故障证据补耐久执行**：C05 用于记录真实运行条件，C06 用于审视副作用和重放边界；优先复用既有本机 journal/receipt，不增加服务器或重量级依赖。

这些是基于外部机制对本项目的候选判断，尚不是质量收益结论。每次候选优化仍需同数据、同运行条件的 B0/B1 对照、逐样本记录和工程回归；没有收益或未达到 baseline 时进入人工审核，不把通过安全闸门等同于完成内容任务。

## 4. 下一道审核门及当前检查

下一步需用户确认**采纳哪些候选、是否允许获取所列特定文件并制作新的逐题证据绑定草案**。此批准仍不等于 gold、指标、划分、冻结、调参、训练或质量达标授权；新草案应独立版本化，保留当前审核包及冻结集不动。

逐题证据草案至少记录：来源类型、固定版本、原地址/许可、获取后的内容 SHA256、原文页码或文件/段落定位、能支持与不能支持的部分、合成输入说明、仍需确认的单位/时间/统计定义。跨文档还需保留事实到不同原始来源的映射。随后才分别审核行为/答案标签、指标和数据划分，不自动扩成 480 条。

本轮交付检查已完成：8 个候选、30 个唯一场景 ID 与既有审核包逐项一致；无浮动 `blob/main` 链接，3 个内部相对链接有效，文档 UTF-8 与差异空白检查通过。真实结构预检退出 0，仍为 `structure_only`，7 个原始来源指纹匹配、`scoring_performed=false`、`ready_for_freeze=false`；审核包保护单测 11 项通过（0.35 秒）。

审核包的 `seeds.jsonl` SHA256 仍为 `ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424`，`manifest.json` 仍为 `752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d`。原 DEV 16 条、冻结记忆、图路由、研究窗口角色四个输入文件的哈希也逐项与本轮前记录一致。检查没有读取封闭测试标签或产生新答案。

这些只证明提案覆盖、文件完整性与草案保护，不是回答准确率评测。最近一次代码完整检查为第十一轮：后端 1715 项通过、1 项既有警告，前端构建/66 项脚本、三个冻结质量门和锁定构建检查通过；本轮仅新增审核文档，评测脚本及其测试哈希与该轮一致，不复用这个数字宣称来源或新问题质量达标。
