# 30 条业务种子：C01–C08 来源证据绑定草案 v0.1

日期：2026-10-10。状态：**待审核，不是 gold，不是评分集，不是冻结集**。

用户已批准采用 C01–C08 的固定版本文本/论文，并允许制作逐题证据草案。本文件只把已经取得的固定版本资料与原有 30 个业务场景做候选绑定；不替换 `eval/fixtures/business_seed_review_v0.1/`，不改变原 `seeds.jsonl`、`manifest.json`、`source_refs`、题目或 `gold`，不运行外部项目代码，不安装框架，不进行模型评分、训练、提示词调参或质量达标判断。

## 1. 审核结论先行

- C01–C08 的指定版本材料均已取得并做了字节哈希；C01–C06 取得固定 commit 的许可证和指定文本/实现文件，C07–C08 取得指定 arXiv PDF。
- 这只证明材料版本与下载完整性可复核，不证明每条业务题都有正文支持。表中 `candidate_support` 是“可进入人工逐句审核”，不是已确认 gold；`partial` 表示只有部分业务命题可由来源支持；`gap` 表示主要依赖 SoulDrive 自己的合同/测试设置，外部来源不能替代。
- 30 条场景全部保留，且仍是原有审核包的场景范围。后续如需替换文献、改题、补标签或冻结，必须另开审核记录并由用户确认。

## 2. 固定版本、许可与获取指纹

来源地址使用固定 commit 或固定论文版本，不使用浮动 `main`/`latest`。许可证核验只覆盖下表所列仓库根部许可证和指定文件；第三方依赖、外链数据、模型权重、示例数据和论文中引用的材料不因此获得许可。

| 候选 | 固定版本与原始地址 | 许可（待法律复核） | 本地材料 | SHA-256 |
| --- | --- | --- | --- | --- |
| C01 Microsoft GraphRAG | [commit `769542f`](https://github.com/microsoft/graphrag/tree/769542fbf1d8e5b4c6a8677fefc34621c87894c5)；[LICENSE](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/LICENSE) | MIT | `build/source-review/2026-10-10-fixed-candidates/microsoft-graphrag/769542f/`；overview/local/global 与 LICENSE | LICENSE `c2cfccb812fe482101a8f04597dfc5a9991a6b2748266c47ac91b6a5aae15383`; overview `ae9f06e8f8c4bd08993a0498af955d49e89ee81b159d8152666596f079ecefda`; local `260b15b16a7f9207e3bddbdeb3efc47a1820666d6ca8b87770c21e5c55976261`; global `21d90708c0d9a1c2b68258e5482bb7c1b94e269a3252a29bbcc0a069aecb8060` |
| C02 Haystack | [commit `6ca8349`](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb)；[LICENSE](https://github.com/deepset-ai/haystack/blob/6ca8349091c09af89ec0090aa13cb22de1d88bfb/LICENSE) | Apache-2.0；指定代码含 SPDX | `build/source-review/2026-10-10-fixed-candidates/haystack/6ca8349/`；README、BM25、Joiner、MetadataRouter、JsonSchemaValidator 与 LICENSE | LICENSE `3f0a3af7e9b0f3b6358aaa3c7501ab522a67dc5483b870e9a8b3db41ea42e249`; README `e016e944fa840265e85e73520522c6152798330720d53d3d64fdffcc448554e6`; BM25 `79c830b6405863e863bcf9786b7611af5984d19188f8653048ddd1bedbd7e465`; Joiner `e47bdc6ac28e096d597982d366559e80ebe9bd81f395100804572595ff2f4af8`; Router `14cd11c6e3ea16b7746fd390884867c5b3948adb6d46a175ba95574e63b08b5e`; Schema `294bdcbc1cf8ce605f794dabdf7fb69859593f557ca7dad4c23a1097fa19c8f9` |
| C03 Graphiti | [commit `17ed4b7`](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259)；[LICENSE](https://github.com/getzep/graphiti/blob/17ed4b707320f092b5e79bdbd9d45130c7f84259/LICENSE) | Apache-2.0 | `build/source-review/2026-10-10-fixed-candidates/graphiti/17ed4b7/`；README 与 LICENSE | LICENSE `2825300b20d7b951209835a4a331f29e24725a39d65168e4b831df53aa372650`; README `72c09aa0f12b7835f8c2919000788bca43306cf847269d4c3a7f35c957218689` |
| C04 LongMemEval 仓库 | [commit `9e0b455`](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc)；[LICENSE](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/LICENSE) | MIT | `build/source-review/2026-10-10-fixed-candidates/longmemeval/9e0b455/`；README 与 LICENSE | LICENSE `d3c4b9aa54759df6ded337978a6f3b55b75615e5e4525c3b82d7e2627d4b9732`; README `c4ff45676683d9e2f7cf7d9099d26426f14635ec110dbb1da818d1019a142573` |
| C05 MLflow | [commit `09bdb2c`](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269)；[LICENSE.txt](https://github.com/mlflow/mlflow/blob/09bdb2cca50a1ab31feefd513ea059bfbd252269/LICENSE.txt) | Apache-2.0 | `build/source-review/2026-10-10-fixed-candidates/mlflow/09bdb2c/`；README 与 LICENSE.txt | LICENSE `6395355de6f391afff35996a30fb41b189b4991a4cb54993ace35ab69a0bfa28`; README `e9b30b8aee35e78fb87bdbbfcf837e8739f596aef5f6def89e735680d7d8b00e` |
| C06 Temporal Python SDK | [commit `b337550`](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472)；[LICENSE](https://github.com/temporalio/sdk-python/blob/b3375504ee29bda57585a5eab7e11a7df39b3472/LICENSE) | MIT | `build/source-review/2026-10-10-fixed-candidates/temporal-python/b337550/`；README 与 LICENSE | LICENSE `d7706f28d144dabf07e4946553a4d117d1dab5cda6bf34a602959b4a2fa86b38`; README `9a841dff876f7421351222243813d739f1a1e722b412e4cd1d81c4c211cb6d7d` |
| C07 LongMemEval 论文 | [arXiv `2410.10813v2`](https://arxiv.org/abs/2410.10813v2)；[PDF](https://arxiv.org/pdf/2410.10813v2) | 版本页指向 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，仍需核对 PDF 第三方材料声明 | `build/source-review/2026-10-10-fixed-candidates/papers/LongMemEval-2410.10813v2.pdf`；28 页 | `05c5d055201466a241a56e082cdd02d39ad566fa04b3804891983e4e069a3fda` |
| C08 Self-RAG 论文 | [arXiv `2310.11511v1`](https://arxiv.org/abs/2310.11511v1)；[PDF](https://arxiv.org/pdf/2310.11511v1) | 版本页指向 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，仍需核对 PDF 第三方材料声明 | `build/source-review/2026-10-10-fixed-candidates/papers/Self-RAG-2310.11511v1.pdf`；30 页 | `d9eaa1398abac0df67a9d0933a5bf8b6d9d83d2e72da2a486073cd842dd52978` |

## 3. 可复核的正文/论文定位

定位是给审核人复查的候选锚点，不是对业务答案的预填。Markdown 行号以本地固定文件为准，PDF 页码为 PDF 页码。

| 来源 | 定位 | 该定位可支持的机制命题 |
| --- | --- | --- |
| C01 | `docs/query/overview.md:3,20,26,32`；`docs/query/local_search.md:5,45`；`docs/query/global_search.md:5,48,50` | 区分基础向量 RAG、面向实体的 local search、面向整体主题的 global search；local 将图结构与原文 chunk 组合，global 以社区报告做 map-reduce，并存在层级/成本权衡。不能由此证明 SoulDrive 的图索引、召回或答案质量。 |
| C02 | `README.md:14,60`；`bm25_retriever.py:44-68,121-155`；`document_joiner.py:46-55,90-115,146-180`；`metadata_router.py:14-16,63-67,121-140`；`json_schema.py:29-34,61-68,115-184` | 把召回过滤、BM25、结果融合、元数据路由和 JSON 结构校验拆成可追踪组件；不能替代 SoulDrive 的项目权限过滤，也不能把 schema 通过当事实正确。 |
| C03 | `README.md:37-50,62-76,119-129,140-152` | 事实有有效时间窗口、episode provenance、增量更新、历史查询和语义/关键词/图混合检索；不能把 Graphiti 的动态 context graph 当作已部署的 SoulDrive 图库，也不能自动改写本地 `as_of` 合同。 |
| C04 | `README.md:21,30,75-88,92-106,186-206,224-232` | 500 个问题覆盖信息抽取、多会话、时间推理、知识更新和 abstention；数据结构有 session/date/evidence 字段，评测输出为 `question_id`/`hypothesis`，并明确 abstention 不应伪造证据位置；不能复制其数据或把 README 当业务 gold。 |
| C05 | `README.md:8-14,110,150-152` | 记录运行、参数、指标、评估结果和产物 lineage，并做系统评测与回归监控；不能替代 SoulDrive 的指标单位、数据版本、可比性和本地时间语义。 |
| C06 | `README.md:7-10,235,721-730,870-948,1094-1104,1930-1955` | durable workflow、activity 副作用隔离、固定 workflow ID、确定性与 replay 检查；不能把本地 journal/receipt 宣称为跨服务 exactly-once，也不能在没有 Temporal server 的情况下宣称已运行其恢复语义。 |
| C07 | PDF p.1-4、p.10 | 论文提出五类长期记忆能力，并描述跨会话、时间、知识更新和未知问题拒答；p.4 明确时间信息既包括显式时间也包括交互时间戳。不能只凭摘要补实验数字或替代本地业务合同。 |
| C08 | PDF p.1-6，尤其 p.1-4 | 论文提出按需检索和对检索/支持性/有用性进行自反思；固定数量、无条件检索可能带来无关上下文；这支持“有界检索与证据校验”的候选机制，不直接支持自由 Agent 循环、SoulDrive 质量提升或训练可复现。 |

## 4. 30 条逐题候选绑定

每一行保留原 seed ID。`支持部分`描述来源可以帮助审核的命题；`不能替代/待核验`是必须保持为空或人工确认的部分。这里不填写 `gold`、标签值、分数或答案。

| Seed | 状态 | 候选来源与定位 | 支持部分 | 不能替代/待核验 |
| --- | --- | --- | --- | --- |
| SEED-RAG-001 | partial | C02 `README:60`、Joiner `46-55`；C08 p.1-4 | 可比较检索、融合与自反思检索的机制边界。 | 两份材料是否对同一事实作共同支持、答案引用是否正确，需逐句绑定。 |
| SEED-RAG-002 | candidate_support | C02 BM25 `44-68,121-155`；Joiner `90-180`；C08 p.1-4 | 可分别核对稀疏召回、过滤、融合和按需检索。 | C02 所列组件不是 SoulDrive 的实测重排器；延迟/Recall/NDCG 未提供。 |
| SEED-RAG-003 | partial | C01 local `5,45`；C03 `62-76,140-146` | 可区分静态图检索与带有效期/episode 的动态事实图。 | 两者不共同定义同一种社区报告；本地图数据、来源证据和图一致性待核验。 |
| SEED-RAG-004 | gap | C01 global `48-50`；C02 `60`；C08 p.1-4 | 可提出跨文档综合的检索/聚合候选。 | 没有绑定到原 30 题所需具体事实，不能先填综合结论或共同观点。 |
| SEED-RAG-005 | partial | C02 Router `14-16,63-140`；BM25 `54-68` | 可审核元数据过滤与未匹配分支。 | “只允许选中文件、不得偷看 C01”是本地测试授权，不由外部源码证明。 |
| SEED-RAG-006 | partial | C02 Router `63-140`；C01 固定文件身份 | 可审核显示标签与元数据身份分离。 | 相同显示名、文件版本和引用归属是 SoulDrive 合成场景，需本地金标准。 |
| SEED-RAG-007 | gap | C02 Router `63-140`；C01 local `45` | 外部资料支持“先路由/过滤再检索”的机制候选。 | 项目 A/B 隔离、权限与串项目率不由这些材料给出。 |
| SEED-RAG-008 | partial | C08 p.1-6；C02 Schema `29-34,61-68` | 可审核按需检索、自反思和结构校验的有界组合。 | 不能据此引入自由 Agentic RAG；循环上限、失败回退和收益需本机 baseline。 |
| SEED-RAG-009 | partial | C01 overview `20-32`；C08 p.1-4 | 可审查不同查询类型是否适用不同检索路径。 | 不能把 GraphRAG 对某类查询的描述外推为所有查询均适用或有收益。 |
| SEED-RAG-010 | gap | C01 global `50`；C08 p.1 | 可记录资源/上下文成本是设计变量。 | RTX 5060 8GB、P50/P95 和本机吞吐必须本地实测，外部资料不提供。 |
| SEED-PAPER-001 | candidate_support | C07 p.1-4、p.10 | 可按研究问题、五类能力、评测设计和局限组织论文精读。 | 论文版本、正文引用、实验数字和缺失信息仍需人工逐段核对。 |
| SEED-PAPER-002 | partial | C08 p.1-6 | 可绑定 Self-RAG 的任务、机制和反思信号。 | 原题中的旧章节/旧论文名称不能自动映射为 C08；模型、数据和结果需正文定位。 |
| SEED-PAPER-003 | partial | C02 README `14,60` 与指定组件文件 | 可识别“实现说明/组件文档”而非同行评审实验论文。 | 不得从 README 补造准确率、显著性或研究结论。 |
| SEED-PAPER-004 | partial | C01 overview/local/global | 可区分原理、查询实现和资源权衡。 | 文档示例不等于本机运行；未列出的配置、演示数据和限制待核验。 |
| SEED-PAPER-005 | gap | C07 p.1；固定 PDF 身份 | 可作为待选论文窗口的稳定资产。 | 未绑定研究窗口/选区前不能默认读取或注入；窗口合同来自本地系统。 |
| SEED-PAPER-006 | partial | C07 p.1-4；C05 `150-152` | 可分别讨论论文证据与一次实验记录的不同作用。 | 切换会话窗口的输入要求、轨迹和输出合同必须由本地 workflow 实现并测试。 |
| SEED-PAPER-007 | gap | C07 p.1-10 | 可检查某个数字是否真的出现在论文并带有定义。 | “12%”只是待核查输入，当前没有绑定页码、指标分母或实验条件。 |
| SEED-PAPER-008 | partial | C08 p.1-6；C02 Schema `29-34`；C05 `110,150-152` | 可把文献结论、结构校验和本机候选实验分层。 | 不产生 baseline、因果结论或质量提升声明；需要本地重复运行。 |
| SEED-EXP-001 | partial | C05 `110,150-152` | 可审核 run、参数、指标、评估结果和产物 lineage 的记录字段。 | 指标单位、数据版本、脚本和环境不由 MLflow README 自动确认。 |
| SEED-EXP-002 | partial | C05 `150-152` | 可要求记录数据/模型/参数/指标以便比较。 | 数据版本不兼容时是否拒绝比较是 SoulDrive 本地合同，不能直接借用外部默认。 |
| SEED-EXP-003 | partial | C05 `110,150-152` | 可区分运行记录与评估结果。 | 不同 config 不是合法重复；统计兼容性和因果边界待本地审核。 |
| SEED-EXP-004 | gap | C05 `110,150-152` | 可记录多次 run 和指标。 | 均值、方差、置信区间、样本量限制没有由这些定位定义，不能预填统计答案。 |
| SEED-EXP-005 | gap | C05 `150-152` | 可记录 `performed_at` 等运行 lineage 候选。 | “上个月”按本地时区和 `performed_at` 解释、同分候选澄清均是 SoulDrive 合同。 |
| SEED-EXP-006 | gap | C05 `150-152` | 可提示运行必须有项目/数据/配置身份。 | 项目 A/B 权限和 baseline 拒绝由本地目录与接口验证，外部 README 不提供。 |
| SEED-MEM-001 | partial | C03 `37-50,62-76`；C04 `75-88`；C07 p.1-4 | 可支持来源 provenance、跨会话证据和时间化事实的审核方向。 | 项目隔离与本地 memory scope 不是外部 benchmark 的结论。 |
| SEED-MEM-002 | partial | C03 `62-76,140-146`；C07 p.1-4 | 可支持不同事实/时间上下文需要不同适用范围。 | 论文窗口/实验窗口的实际 memory namespace、切换清理和权限待本地验证。 |
| SEED-MEM-003 | candidate_support | C03 `62-76,119-129,140-146`；C04 `81-88`；C06 `870-948` | 可审核当前事实、历史版本、来源 episode、知识更新和重放边界的分离。 | C06 只能作为工程机制参考；不证明本地 outbox 的跨存储原子性或 exactly-once。 |
| SEED-MEM-004 | candidate_support | C03 `62-65,140-146`；C07 p.4 | 可绑定有效时间窗口、显式时间和交互时间戳的研究机制。 | `as_of=100/300` 是本地合成时钟，不能直接解释为外部论文的业务日期。 |
| SEED-MEM-005 | partial | C03 `62-76,119-129`；C04 `81-88` | 可审核事实更新、历史保留和来源回指。 | 用户偏好冲突解决、通知授权和不发送副作用必须由本地测试明确。 |
| SEED-MEM-006 | gap | C03 `119-129`；C04 `81-88` | 可支持项目约束需要有来源和适用范围的候选原则。 | 约束优先级、权限、确认级别和跨项目不泄露不由这些来源替代。 |

## 5. 审核顺序与下一道门

1. 先审核每行的来源类型、版本、定位和 `candidate_support/partial/gap` 状态；任何定位无法在本地固定文件或 PDF 页码复查时，降为 `gap`。
2. 再审核业务题是否仍与选定材料同题域；不能因为标题相近就替换旧文献、修改题意或把 C04 与 C07 当作两个独立研究来源。
3. 用户确认后才能另建 gold/标签草案；在此之前不读取封闭测试标签，不生成答案，不做训练或调参，不把本草案写入冻结 fixture。
4. 后续若要验证 Self-RAG/Agentic RAG，只能先做固定数据、固定模型、固定预算的 `B0 -> B1 -> Candidate` 对照；优先验证按需检索、证据校验和有限重试，不直接引入自由循环或重量级框架。

本文件的“来源支持”不是 SoulDrive 的质量结论。当前 30 条场景仍处于待审核状态；真正的召回率、引用正确率、答案事实性、窗口差异化作用、跨项目隔离、记忆更新与耐久执行收益，都必须在本地冻结规则和逐样本日志下单独验证。
