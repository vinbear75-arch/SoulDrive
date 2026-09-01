# SoulDrive 竞赛价值重构与执行路线

状态：执行主计划  
版本：v1.0  
日期：2026-08-29  
适用项目：SoulDrive-PC  
目标赛事：第八届中国研究生人工智能创新大赛，华为赛题五“智能终端多模态输入感知与识别及个性化应答”

## 1. 文档用途

本文是 SoulDrive 后续竞赛开发、实验、演示和材料撰写的统一执行依据。后续新增功能、模型训练、评测和界面调整，必须能够追溯到本文中的目标、阶段和验收条件。

本文不授权真实邮件发送、真实日历写入、扩大屏幕采集范围、启用摄像头、覆盖或删除用户文件。遇到这些动作，必须暂停并取得人工确认。

## 2. 最终产品定位

### 2.1 一句话定位

> 用户指着屏幕中的实验结果说“为什么比上个月差”，SoulDrive 在当前项目内对齐语音、授权选区、实验版本和历史偏好，在 5 秒内给出可验证差异、原因证据等级和最小验证实验，并生成带引用的汇报草稿；全程端侧处理，歧义时先澄清，未经确认不执行外部动作。

### 2.2 核心用户

- 研究生和课题组成员；
- 算法、软件和产品研发人员；
- 需要管理实验材料、比较版本并形成汇报的本地知识工作者。

### 2.3 核心价值

SoulDrive 不再以“功能很多的本地 AI 助手”为主要卖点，而以“端侧、多模态、证据式科研与研发实验诊断”为唯一主线：

1. 语音负责表达任务和关注点；
2. 屏幕选区负责绑定“这个指标、这张图、这个实验”；
3. 项目目录负责限定项目、时间、版本和指标语义；
4. RAG 负责找到可定位的文档证据；
5. 实验账本负责完成严格数值比较；
6. 个性化画像负责改变表达和决策权重，但不得改变事实；
7. 确认门负责阻止歧义目标和不可逆操作。

## 3. 竞赛硬约束

依据 2026 年大赛官网及华为赛题附件，执行时必须满足：

- 支持至少两种模态输入，并完成时间维度上的对齐与融合；
- 结合当前界面、历史对话等上下文进行意图理解和场景建模；
- 基于用户画像生成个性化应答；
- 支持 PC 或其他端侧部署；
- 主模型参数量不超过 14B；
- 最多额外使用三个 3B 小模型进行数据处理；
- 单轮对话响应延迟小于 5 秒；
- 少量人工标注数据不超过 500 条，并明确数据来源和标注策略；
- 总分由公开测试自动评测 50% 和专家评审 50% 构成；
- 专家评审关注新颖性、合理性、端侧部署可行性，并可能使用华为内部多模态数据评估泛化。

参考：

- [第八届中国研究生人工智能创新大赛赛题](https://cpipc.acge.org.cn/cw/contestNews/detail/2c9088a5696cbf370169a3f8101510bd/2c9080179e403028019e496a8feb14b9?page=0)
- [华为赛题附件](https://cpipc.acge.org.cn/sysFile/downFile.do?fileId=4bff16a402754033bb8ec9fca03cee0b)

## 4. 当前状态与主要缺口

以下数据是当前仓库快照，不得直接当作最终竞赛成绩。每次提交前必须重新冻结代码、数据和结果。

| 能力 | 当前证据 | 结论 |
| --- | --- | --- |
| RAG 检索 | 360 条冻结集上 all-hit@6 为 87.0%，MRR@6 为 0.862 | 基础可用，但只是检索指标，不等于回答准确率 |
| 项目查询 | 10 条固定样本中 Version Hit@1 为 100%，错误项目率为 0% | 样本过少，需要真实、多项目和歧义场景 |
| 真实多模态快路径 | 10 条真实授权样本中，目标绑定由 text-only 的 0 提升到 1.0；意图准确率仍为 0.9，存在 1 条失败 | 只证明目标绑定收益，尚未证明完整任务收益 |
| VLM 慢路径 | 单条真实授权样本总耗时约 24.7 秒 | 不满足官方小于 5 秒要求，是最高优先级阻塞 |
| 公开多模态基线 | MIntRec2.0 当前 text-only Macro-F1 约 0.383，早期音视频融合约 0.326 | 当前融合有负收益，不得宣称多模态提升 |
| GUI grounding | ScreenSpot 当前总准确率约 39.5%，图标目标约 1.9% | OCR 文字快路径可用，非文字目标明显不足 |
| 个性化 | 已有显式偏好记忆、项目记忆和模板 | 缺少结构化画像、响应级个性化策略和评测 |
| 实验目录 | 已有 experiment、run、metric、measurement 数据表 | 数据骨架存在，但缺少完整摄取、比较和原因验证闭环 |
| 完整端到端 | 当前 release gate 为 MVP ready、competition ready=false | 缺少真实业务完整链路和官方/隐藏集验证 |

当前证据文件：

- `eval/results/rag_rebuild_baseline_v0.1.json`
- `eval/results/competition_scorecard_v0.1.json`
- `eval/results/competition_release_gate_current.json`
- `eval/results/multimodal_real_pilot_retest_unleaked_20260825/summary.md`
- `eval/results/multimodal_real_pilot_vlm_retest_unleaked_20260825/summary.md`
- `core/project_catalog.py`
- `core/multimodal_workflows.py`

## 5. 三项核心创新

### 5.1 SoulAlign-T：可靠性感知的时序对齐与冲突决策

SoulAlign-T 不采用简单的文本、语音和图像特征拼接。系统把每次交互表示为带时间、空间、来源和置信度的事件图。

节点至少包括：

- 语音片段及 ASR 置信度；
- UI Automation 控件或系统可读选区；
- 鼠标框选和截图区域；
- OCR 文本区域；
- VLM 视觉描述或候选目标；
- 当前项目、资产、实验和版本；
- 用户确认、取消和澄清操作。

边至少包括：

- `temporal_overlap`：事件在时间窗口内重叠；
- `points_to`：语音指代或鼠标动作指向目标；
- `belongs_to_project`：目标属于当前项目；
- `supports`：一个模态支持候选目标或意图；
- `contradicts`：不同模态或项目上下文冲突；
- `requires_confirmation`：继续执行需要人工确认。

每次推理必须输出：

- 意图及来源；
- 目标候选列表；
- 最终目标及置信度；
- 时间对齐结果；
- 冲突原因；
- `respond / clarify / refuse / confirm / cancel` 动作；
- 快路径、慢路径或拒绝路径的选择原因。

### 5.2 EvidenceGraph：类型化证据与因果防火墙

回答中的证据必须区分来源类型：

- `E#`：论文、报告、配置、日志等文档证据；
- `V#`：本轮授权截图中可直接观察到的视觉证据；
- `A#`：用户确认过的语音或会议转写；
- `M#`：结构化实验测量和统计结果；
- `P#`：用户确认、可编辑、可撤销的偏好。

系统不得用 `V#` 证明外部事实，不得用 `P#` 改写实验数值，也不得把 `A#` 伪装成项目文档。

原因分析必须分为：

1. 已证实原因：存在直接实验、消融、日志或决策证据；
2. 高可信候选：存在相关变化和机制解释，但缺少对照实验；
3. 待验证假设：只有推断，必须给出验证方案；
4. 无法确定：缺少必要证据或材料不可比。

对未证实原因，系统必须生成最小区分实验，而不是继续补充看似合理的解释。

### 5.3 可验证个性化

用户画像采用结构化、显式确认、可撤销设计，至少包括：

- 输出长度和表达风格；
- 先结论、先证据或先风险；
- 对精度、延迟、显存、成本、稳定性等指标的关注权重；
- 常用项目、实验别名和指标别名；
- 常用报告、PPT 和组会模板；
- 每项偏好的来源、适用范围、确认时间和最后使用时间。

个性化只改变表达和排序，不改变事实、数值、证据引用和安全边界。

## 6. 总体执行顺序

```text
S0 冻结定位与评测契约
  -> S1 补齐真实数据和端到端基线
  -> S2 实验账本与严格比较
  -> S3 SoulAlign-T 与 GUI 目标验证
  -> S4 可验证个性化
  -> S5 证据式生成与项目 Playbook
  -> S6 竞赛回归、材料和演示冻结
```

每次只改变一个核心因素。没有同数据、同模型、同配置、同指标的 baseline，不得宣称提升。

## 7. 分阶段任务与验收

### S0：冻结定位与评测契约

目标：所有开发和材料围绕同一个问题展开。

任务：

- [ ] 将本文一句话定位写入演示脚本、项目文档和 PPT 首页；
- [ ] 确认主场景为“语音 + 授权屏幕目标 + 项目实验历史的证据式诊断”；
- [ ] 冻结端到端 episode schema、指标定义和数据划分；
- [ ] 冻结 text-only、B1 simple、Candidate 三条路径；
- [ ] 明确模型数量、参数量和硬件计数口径；
- [ ] 记录当前 Git 提交、未提交差异哈希、配置和模型哈希。

验收：

- 一句话定位在 20 秒内可解释清楚；
- 所有核心指标有唯一名称、计算公式和方向；
- sealed-test 在最终评测前不可用于调参、提示词修改或错误驱动修正。

### S1：真实数据与完整端到端基线

目标：把“接口可运行”升级为“真实任务可度量”。

任务：

- [ ] 在现有 10 条真实授权样本基础上，补足至少 30 个独立 episode；
- [ ] 增加不同说话人、不同屏幕分辨率、不同项目和不同指代方式；
- [ ] 覆盖文字目标、图标目标、图表目标、时间错位、项目冲突、低置信度和 OOS；
- [ ] 每条 episode 记录 ASR、OCR、VLM、对齐、检索、首 token 和完整回答延迟；
- [ ] 冻结 text-only 与完整模态逐样本输出；
- [ ] 保存失败样本、错误类型和是否触发澄清。

核心指标：

- Intent Accuracy、Macro-F1；
- Target Binding Accuracy；
- Conflict Detection Accuracy；
- Clarification Precision、Recall、F1；
- OOS F1；
- Evidence Coverage 和 Citation Validity；
- Wrong-project Rate；
- P50/P95 首 token 和完整回答延迟；
- 冷启动时间、峰值显存、失败率。

验收门：

- 完整模态在目标绑定、冲突识别或澄清上稳定超过 text-only；
- 完整回答 P95 小于 5 秒；
- 错误项目率为 0；
- 不可逆动作拦截率为 100%；
- 多模态无稳定增益时，停止宣传创新并回到错误分析。

### S2：实验账本与严格比较

目标：让“和上个月实验比较”成为可运行功能，而不是提示词演示。

任务：

- [x] 增加 CSV/XLSX 指标、YAML/JSON 配置、日志和实验报告摄取；
- [x] 对接 TensorBoard、MLflow 或 W&B 的本地导出格式，首版只选一种最小实现；
- [ ] 为实验、run、metric 和 measurement 补齐创建、查询和版本化接口；
- [ ] 自动抽取候选字段，但对日期、单位、数据集版本和 split 显示确认卡；
- [ ] 实现按本地时区解析“上个月”；
- [ ] 实现指标定义、单位、数据集、split 和脚本版本的可比性门控；
- [ ] 输出差异表、变化因素、证据和缺失字段。

验收门：

- 低置信度关键字段不会被静默写入；
- 不可比材料不会计算百分比变化；
- 每个数值能定位到源文件、sheet/表格/行或日志片段；
- 重复运行优先报告均值、方差和置信区间；
- 原因结论严格区分已证实、候选和待验证。

### S3：SoulAlign-T 与 GUI 目标验证

目标：提升非文字目标、时间对齐和冲突场景泛化。

任务：

- [x] Accessibility/UI Automation 作为第一来源；
- [x] OCR 作为文字快路径和结构化控件缺失时的降级；
- [x] VLM 只处理用户授权裁剪区域；
- [x] VLM 一次返回多个候选区域，而不是直接给唯一坐标；
- [x] 增加独立 verifier，结合时间、空间、窗口焦点、项目和模态可靠度选择候选；
- [x] 目标不唯一或 verifier 低置信度时主动澄清；
- [ ] 优化模型预热、裁剪尺寸、缓存和路由，消除 24.7 秒慢路径；
- [x] 建立并运行 ScreenSpot 分项评测：按文字/图标、移动/桌面/Web 输出分组指标、延迟和
  与配对基线的差异；模型独立重跑和完整端到端指标仍需另行完成。

必要消融：

- text-only；
- 去掉时间对齐；
- 去掉空间选区；
- 去掉项目元数据；
- 去掉可靠度门控；
- 去掉 verifier；
- 去掉冲突澄清。

验收门：

- 图标目标准确率必须显著高于当前约 1.9% 的基线；
- 候选 verifier 相对单坐标预测有稳定提升；
- 无关截图不能绕过普通 RAG 证据门；
- VLM 慢路径完整回答 P95 小于 5 秒；
- 低置信度目标不得自动执行动作。

### S4：可验证个性化

目标：证明个性化有效且不会造成事实漂移。

任务：

- [x] 建立结构化偏好记录及来源追踪；
- [x] 提供用户查看、修改、停用和删除入口；
- [x] 区分全局偏好和项目偏好；
- [x] 为长期未确认或长期未使用偏好降低权重；
- [x] 设计成对画像评测：同一证据、不同画像；
- [x] 加入不相关偏好、冲突偏好和项目切换测试。

核心指标：

- Preference Adherence；
- Factual Invariance；
- Citation Invariance；
- Irrelevant-preference Rejection；
- Cross-project Leakage Rate；
- 用户撤销后残留率。

验收门：

- 个性化显著改变结构、排序或表达；
- 事实、数值和引用保持一致；
- 跨项目泄露率为 0；
- 用户删除或撤销后不再使用对应偏好。

### S5：证据式生成与项目 Playbook

目标：把重复成功的工作流沉淀为可审计能力。

任务：

- [x] 为检索候选增加问题相关摘要和支持度评分；
- [ ] 对长论文和标准增加层级文档树路由；
- [x] 在 UI 中提供分块检查、关键词修正和检索测试；
- [x] 将论文精读、实验复盘和组会汇报定义为版本化 Playbook；
- [x] 每个 Playbook 声明输入、输出、证据范围、副作用和确认点；
- [x] 只允许用户审核通过的 Playbook 在当前项目运行；
- [x] 产物记录来源版本、生成计划、确认记录和变更摘要。

验收门：

- Playbook 在相同输入上产生结构一致、证据可追溯的结果；
- 未满足输入契约时不启动生成；
- 产物中的事实均能回到 `E#/V#/A#/M#`；
- Skill 或 Playbook 不得绕过权限和确认门。

### S6：竞赛冻结与回归

目标：生成可复现、可解释、不过度声明的提交包。

任务：

- [ ] 冻结代码、模型、配置、数据和随机种子；
- [ ] 运行全量测试、前端构建、Tauri 检查和端到端真实任务；
- [ ] 保存逐样本结果、聚合指标、错误分析和延迟分布；
- [ ] 单独报告公开集、合成集、真实业务集和华为隐藏集；
- [ ] 录制统一演示：目标绑定、项目比较、证据分级、个性化和确认门；
- [ ] 输出架构图、数据流图、消融表和失败案例；
- [ ] 检查模型参数量、依赖许可证和数据许可证；
- [ ] 生成竞赛声明清单和限制清单。

最终验收：

- 公开测试脚本能够从干净环境复现；
- 完整回答 P95 小于 5 秒；
- 多模态相对 text-only 的增益通过同条件实验验证；
- 专家能够看到真实失败案例和系统如何安全降级；
- 不把本地合成结果称为华为成绩；
- 不把 targeted tests 称为整体准确率。

## 8. 当前提交冲刺

大赛官网显示报名已于 2026-08-25 结束，作品提交截止 2026-09-01。以下清单以已经完成报名为前提。

### 8.1 提交前必须完成

- [ ] 冻结一句话定位和唯一演示故事；
- [ ] 说明当前多模态真实收益只在目标绑定得到验证；
- [ ] 明确披露 VLM 慢路径当前不达标；
- [ ] 优先尝试模型预热、裁剪输入、缩短输出和路由降级；
- [ ] 补足真实 episode 或明确披露仍不足 30 条；
- [ ] 输出 text-only、完整模态、去时间和去冲突门控对比；
- [ ] 完成项目文档、运行说明、可解释性文档和公开集报告；
- [ ] 录制一条完整、真实、无剪辑隐藏失败的演示链路。

### 8.2 提交前不做

- 不重写整个前端；
- 不增加新的邮件服务商；
- 不增加真实日历写入；
- 不建设通用桌面自动化；
- 不增加 24 小时后台录屏；
- 不训练新的大模型；
- 不为了功能数量增加更多 PPT 风格或 Skills；
- 不在没有同条件评测时继续调 RAG 权重。

## 9. 开源项目借鉴清单

借鉴只提取机制和接口思想，不直接复制代码；引入任何实现前必须检查许可证、依赖体积和端侧资源。

| 项目 | 值得借鉴 | SoulDrive 最小适配 | 不借鉴 |
| --- | --- | --- | --- |
| [OmniParser](https://github.com/microsoft/omniparser) | 把截图解析成结构化 UI 元素 | 为 VLM 提供有限候选区域 | 不构建通用自动点击代理 |
| [GUI-Actor](https://github.com/microsoft/GUI-Actor) | 多候选区域和 grounding verifier | 在现有 3B VLM 后增加候选验证 | 不直接引入 7B 常驻模型 |
| [UI-TARS](https://github.com/bytedance/UI-TARS) | GUI agent 评测与坐标处理 | 学习标准化坐标和 ScreenSpot 协议 | 不竞争完整桌面自动化 |
| [Screenpipe](https://github.com/screenpipe/screenpipe) | Accessibility-first、OCR fallback、本地时间轴 | 用户触发的 10–30 秒短时事件缓冲 | 不做 24 小时录屏 |
| [Windows UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-obtainingelements) | 通过焦点元素/坐标获取结构化控件，而不是先猜像素 | 只读 `GetFocusedElement` 快路径，保留控件类型、焦点和有限元数据 | 不读取控件值，不执行点击或输入 |
| [PaperQA2](https://github.com/future-house/paper-qa) | 问题相关摘要、证据重排、带引用回答 | 为候选片段增加 query-dependent summary | 不默认依赖云端模型和外部元数据 API |
| [PageIndex](https://github.com/VectifyAI/PageIndex) | 长文档层级树和推理式导航 | 只用于长论文和标准的复杂路由 | 不直接替换现有混合检索 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 分块检查和检索测试界面 | 增加可解释的摄取与检索调试面板 | 不引入其完整服务栈 |
| [Mem0](https://github.com/mem0ai/mem0) | user/project/run 作用域和实体关联 | 明确偏好、项目事实和运行记忆边界 | 不引入外部图数据库作为首选 |
| [Letta](https://github.com/letta-ai/letta) | 分层、可检查、可演进记忆 | 给偏好和 Playbook 增加版本与来源 | 不把 Agent 自主改写记忆设为默认 |
| [Claude Code permissions/hooks](https://code.claude.com/docs/en/permissions) | 规则与强制权限分离 | Playbook 执行前后检查和强制确认 | 不依赖提示词保证安全 |
| [OpenAI KPI 根因分析用例](https://learn.chatgpt.com/use-cases/kpi-root-cause-analysis) | 区分确认驱动因素与假设 | 固化为实验诊断输出契约 | 不照搬通用业务模板 |

## 10. 评测数据设计

### 10.1 Episode 最低字段

每条真实或合成 episode 至少保存：

- `episode_id`、数据版本和 split；
- 说话人匿名 ID；
- 音频文件、转写和词/片段时间戳；
- 授权截图或 UI 树；
- 授权区域和目标 bbox；
- 当前窗口和当前项目；
- 历史上下文摘要；
- 用户画像版本；
- gold intent、target、conflict、action 和 clarification；
- 允许使用的证据引用；
- 预期回答契约；
- 每阶段延迟和资源；
- 失败原因和人工复核结论。

### 10.2 数据划分

- 先用 30 条种子检查 schema 和标注一致性；
- 总量不超过 480 条时，保持 320/80/80 的 train/validation/sealed-test；
- 场景模板、说话人、界面布局和关键措辞尽量跨 split 隔离；
- sealed-test 冻结后禁止用于提示词、阈值、模型或规则调优；
- 合成数据、公开数据和真实授权数据必须分开报告。

### 10.3 必测困难样本

- “这个、那个、刚才的”指代；
- 语音和目标在时间窗口外；
- 同屏多个相似目标；
- 文字目标与图标目标；
- 当前项目与屏幕文档项目冲突；
- ASR 错词、OCR 低置信度、模态缺失；
- 画像与本轮显式要求冲突；
- 请求真实发送、删除、覆盖或越权捕获；
- 无证据原因分析；
- 指标同名但定义、单位或 split 不同。

## 11. 竞赛演示脚本

统一演示只讲一个故事：

1. 用户打开当前项目的实验结果图；
2. 用户框选一个下降指标并说：“这个为什么比上个月差？”；
3. SoulDrive 展示本轮捕获的语音、目标、项目和时间范围；
4. 系统发现两个历史候选时主动澄清，不默认选择；
5. 用户确认后，系统完成指标可比性检查；
6. 系统展示差异、配置变化、日志证据和会议决策；
7. 输出“已证实原因、高可信候选、待验证假设”；
8. 对待验证假设生成最小实验；
9. 根据用户偏好生成简洁组会版回答，但事实和引用不变；
10. 用户确认后生成带证据的 PPT 或邮件草稿；
11. 系统明确显示草稿未发送，并保留审计记录。

演示必须同时展示一次失败或歧义安全降级，避免只展示理想路径。

## 12. 论文与答辩中的声明边界

可以声明：

- 系统完成本地项目隔离、证据追踪和确认式执行；
- 在指定数据集、模型、配置和指标下观察到的结果；
- 多模态在目标绑定、冲突识别或澄清上的同条件增益；
- 端侧运行的真实 P50/P95、显存和失败率；
- 哪些开源机制被重新设计并适配到 SoulDrive。

不可以声明：

- targeted tests 等于整体准确率；
- 合成数据结果等于真实用户效果；
- 公开集成绩等于华为隐藏集成绩；
- 检索命中率等于回答准确率；
- 相关变化等于性能下降原因；
- 单条 VLM smoke 等于 P95；
- 多模态没有超过 text-only 时仍宣称创新有效；
- 未核验许可证的数据或权重可用于比赛和发布。

## 13. 决策门与暂停条件

以下情况必须暂停并请求人工审核：

- 改变 sealed-test、指标定义、时间语义或主评测口径；
- 公开数据许可证、下载完整性或权重再发布权限不明；
- 请求真实发送邮件、写入日历、删除或覆盖文件；
- 请求扩大屏幕捕获范围、启用摄像头或后台持续录制；
- 需要增加云端依赖或把本地资料上传外部服务；
- 模型数量、参数量或延迟可能超过赛题限制；
- 候选方案没有超过 baseline 或引入明显回归；
- 无法证明原因、证据冲突或人工标注存在分歧。

## 14. 完成定义

只有同时满足以下条件，SoulDrive 才能被标记为“竞赛版本完成”：

- [ ] 唯一主场景完整可运行；
- [ ] 至少 30 个独立真实授权 episode 完成端到端评测；
- [ ] 完整模态相对 text-only 有稳定、可复现增益；
- [ ] VLM 慢路径完整回答 P95 小于 5 秒；
- [ ] 个性化通过事实和引用不变性测试；
- [ ] 实验比较通过可比性门控；
- [ ] 所有事实性回答有可定位引用；
- [ ] 不可逆动作拦截率为 100%；
- [ ] 公开、合成、真实业务和隐藏集结果分开报告；
- [ ] 代码、模型、配置、数据、逐样本结果和演示材料均已冻结；
- [ ] 限制、失败案例和未完成项已如实披露。

## 15. 本轮执行记录（2026-08-29）

本轮只将路线图中能够在当前本机工作区实现并验证的增量落地，未把未完成的竞赛门槛
标记为完成。

已落地并有回归证据：

- S2 实验台账的本地解析、确认门、项目/证据版本绑定、来源定位、可比性检查、差异因素、
  单因素验证建议，以及显式 run 的重复统计汇总接口；未确认导入不产生目录写入。新增
  MLflow FileStore 单 run 本地压缩导入适配，按官方 `meta.yaml/metrics/params/tags` 结构读取，
  不引入 MLflow 运行时或云端依赖。
- S3 目标复核契约：授权裁剪、VLM 结构化多候选解析、时间/项目/来源可靠度/焦点融合
  verifier、空间区域一致性门、目标缺失或接近时澄清、有限本地缓存；前端提供“目标复核”入口。
  同时修复目标候选在最终问答决策中的事件类型和 `target_grounding` 权限丢失问题；感知层
  返回 `clarify/refuse` 时，前端在进入 RAG 前硬停。
- S3 延迟预算增量：将 JSON 与 multipart 两个多模态入口的默认 `max_new_tokens` 与运行时
  的短输出预算统一为 24，避免前端未显式传参时意外使用 48 token 的慢路径预算；保留用户
  显式参数覆盖，并用接口转发回归锁定。该改动尚未构成 P95 改善证据，真实 VLM 热路径
  仍需在冻结 episode 上进行同条件测量。
- S3 证据传播增量：补齐多模态事件从 runtime 到 `/multimodal/decide` 再到 `/chat` 的来源、
  窗口、显式授权、焦点点位、区域和候选来源字段；按字段白名单和归一化边界传播，不把原始
  事件提升为系统指令，并保持视觉证据在前、结构化 UI 事件在后的既有顺序。
- S3 ScreenSpot 分项评测增量：新增只读取已保存 `scored_predictions.jsonl` 的 fail-closed
  评测器，校验样本键和平台/目标类型标签，输出文字/图标、移动/桌面/Web 及交叉分组的
  准确率、缺失预测、P50/P95 延迟和配对基线差异；现有 1272 条 RapidOCR 候选结果为总体
  39.544%、文字 70.588%、图标 1.913%、P95 2745.593 ms，图标相对基线差异为 0；未将
  该结果当作业务端到端指标。
- S5 证据可解释性增量：最终证据增加抽取式问题相关摘录、词项支持列表和启发式支持度，
  明确标注其不是概率或准确率；前端证据面板显示该字段，且不改变当前排序权重和证据门。
- 验证结果：后端完整回归 `695 passed`；本轮新增台账、目标对齐、运行时、证据支持和
  ScreenSpot 分组评测定向测试通过；前端构建与现有前端测试通过；Python 编译检查通过。

仍未达标、不得提前宣称完成：

- 真实独立 30 条 episode、ScreenSpot 分项模型独立重跑和完整链路 5 秒 P95；
- 真实 VLM 热路径的稳定增益、图标目标超过既有基线、模型预热优化和同条件消融；本轮
  仅统一了默认输出预算并补齐证据传播，尚无同条件 P95 改善结论；
- TensorBoard/W&B 导出格式及 MLflow 多 run/服务端适配、S4 个性化成对评测、S5 Playbook
  版本化、长文档层级路由和检索测试 UI，以及 S6 冻结提交包；
- 竞赛完成定义中的“完整模态稳定超过 text-only”“所有事实引用有效”等总体验收。

本轮没有修改 sealed-test 的主评测口径，也没有把跨数据集结果混算；后续评测必须继续
使用与结果文件匹配的数据集，并保留逐样本证据和延迟记录。

## 16. 本轮继续执行记录（2026-08-30）

本轮继续严格按 S3 的未完成项推进，只落地当前本机可以复现和回归的部分，未把真实
跨应用指标或竞赛完成门槛提前标记为通过。

已落地并有回归证据：

- S3 Accessibility/UI Automation 第一来源：新增 Windows 原生只读 UIA 快路径，使用
  `GetFocusedElement` 获取当前控件的有限结构化元数据；提供 `Ctrl+Alt+Shift+U` 显式
  快捷键和工具栏降级入口，不注册焦点变化监听，不读取控件值，不执行外部动作。密码控件
  或无法确认密码属性时返回脱敏结构，文本经过长度和控制字符边界处理。
- S3 多模态证据接入：UIA 事件带来源、控件类型、焦点、显式授权、窗口句柄和候选 ID，
  通过 runtime -> `/multimodal/decide` -> `/chat` 进入现有链路；UIA 保持为高可靠
  `ui_automation` 模态，30 秒后自动失效，避免把旧焦点误用于新问题。结构化事件名称
  和控件类型会进入交互上下文，目标仍由现有 SoulAlign verifier 决定。
- S3 UIA-first 路由增量：当用户明确读取到可信的当前焦点 UIA 控件并执行目标复核时，
  允许 UIA 作为目标证据，跳过重复的 VLM 慢路径；用户明确选择深度视觉时仍保留 VLM。
  同一图片/提示词的受控对照中，当前无 UIA 的 VLM 阶段约 `17,784 ms`，UIA-first
  场景 runtime 约 `39.3 ms`，目标 verifier 选中 `button-save`、分数 `0.9755`；该对照
  不是 P95，也不代表所有图表/图标都可跳过 VLM。
- S3 有界缓存增量：OCR 结果按图像内容摘要复用，缓存上限为 8 条并采用 LRU；命中返回
  隔离副本，避免下游修改污染缓存；VLM 的 4 条缓存同步改为 LRU。阶段日志新增 OCR
  `cache_hit`，使命中与真实推理可审计。对同一张真实产品截图、同一运行时、5 个不同
  问题的回归中，首条 OCR `1524.264 ms`，后 4 条均命中，OCR `0.091–0.115 ms`、
  中位数 `0.0925 ms`，每条均保留 4 个文字区域；逐次结果见
  `eval/results/multimodal_ocr_cache_recheck_20260830.json`。该数据只证明重复截图
  的局部收益，不外推到完整链路 P50/P95。
- S3 显式模型预热增量：新增受 token 和解锁保护的本地 `/multimodal/runtime/warmup`
  接口，并在已有截图且用户选择深度视觉或目标复核时显示“预热视觉”入口；默认自动路径、
  UIA-first 快路径和无截图场景不自动加载 VLM。相同图片、问题、24 token 和 `slow` 路径的
  单样本对照中，未预热请求总耗时 `13234.171 ms`、VLM `13194.502 ms`；显式预热耗时
  `13203.462 ms` 后，实际视觉请求总耗时 `2661.200 ms`、VLM `2646.962 ms`。逐次结果见
  `eval/results/multimodal_vlm_warmup_recheck_20260830.json`；该增量是前移加载成本，
  不是消除总计算量，也不是 P95 结论。
- S5 RAG 层级摄取增量：借鉴 RAGFlow 的标题式 parent-child 语义补全和 PageIndex 的长文档
  结构导航思路，在现有父子索引之前为普通 Markdown/TXT/JSON/YAML/Office 文本保留确定性的
  多级 `section_path`；同段标题不再丢失，标题独立成文档也不再静默丢弃。PDF 解析器扩展到
  六级标题，明显的标点、残缺编号和短中文姓名样式不再成为章节节点，但原文仍保留为正文。
  重排、引用和支持度回退字段同步识别 Header 4–6。六份当前 RAG 中文 PDF 的一次本地解析
  检查中，残缺章节计数为 0；逐文件结果见 `eval/results/rag_hierarchy_ingestion_recheck_20260830.json`。
- S5 多主题结构覆盖增量：针对“只命中一个引号主题却获得满结构分”的排序缺陷，结构匹配改为
  `matched_topics / quoted_topics` 覆盖率；单主题问题仍保持命中即满分，多主题问题只有覆盖全部
  主题才获得满分。该修复不改变项目过滤、召回池或评测口径。在六份原始 RAG 中文 PDF 的隔离
  索引上（637 个子块），同一 `rag_cn_raw_v0.1/dev` 的 16 条样本前后均为 all-hit@6、首条命中率、
  MRR 和来源覆盖率 100%；3 条样本的证据顺序发生变化，平均检索耗时 1737 ms -> 1748 ms（+11 ms）。
  逐样本前后结果和限制见 `eval/results/rag_hierarchy_coverage_recheck_20260830.json`。
- S5 最小章节树路由增量：将 `section_path` 用作检索时的章节节点，优先精确节点并覆盖该节点
  的后代子块；多主题查询先为每个主题保留一个分支。候选合并不再因关键词分更高而丢失路由元数据，
  引用额外返回 `section_path`、路由模式和覆盖率。六文档隔离 dev 的 16 条样本中，15 条实际带有
  章节路由证据，all-hit@6、首条命中率、MRR 和来源覆盖率均保持 100%，6 条样本的候选顺序发生
  变化，平均检索耗时 1748 ms -> 1784 ms（+36 ms）。逐样本结果见
  `eval/results/rag_section_tree_route_recheck_20260830.json`。
- S5 持久化章节树增量：参考 PageIndex 的长文档节点导航和 RAGFlow 的父子分块元数据，新增
  `section_tree_v1`。节点 ID 由文档 ID与完整章节路径稳定生成，普通/加密父子索引均保存节点标题、
  深度、父子关系、祖先链、完整路径和确定性抽取摘要；检索可按节点标题或完整路径沿祖先链命中后代
  子块，旧索引缺少树字段时自动回退到既有 `section_path` 路由。该增量不改变向量、关键词、图谱、
  重排或证据门，逐项记录见 `eval/results/rag_section_tree_persistence_recheck_20260830.json`。
- S5 证据纠错增量：参考 CRAG 的“先评估检索质量、再纠错重检索”链路，将原有仅扩大
  `Top-K` 的一次重试改为有界确定性纠错：普通问题使用现有查询变体，比较问题使用现有子查询
  分解；保持原始问题作为证据门判断依据，保持当前项目/全局过滤，并审计 `correction_mode`、
  `query_changed` 和重试查询长度，不记录重试原文。受控弱证据场景验证二轮使用不同查询并选择更强
  证据；逐项结果见 `eval/results/rag_retrieval_correction_recheck_20260830.json`。
- S5 生成质量门增量：针对本地 3B 在引用合法但内容重复时反复输出同一段和 `## 1` 的真实 dev
  症状，增加保守的重复编号标题/长答案块检测；命中后复用已有抽取式证据 fallback，不引入额外模型
  调用，也不放宽引用门。16 条同条件 dev 重跑中，重复输出样本由 1 条降为 0 条，最长答案由
  1522 字符降为 467 字符，引用合法率保持 100%；由于本地生成未固定随机种子，答案字符串有 11 条
  变化，关键词和延迟差异只作观察，不宣称因果精度提升。逐项结果见
  `eval/results/rag_answer_quality_gate_recheck_20260830.json`。
- 本增量的验证结果：定向 `85 passed`，后端完整回归 `715 passed, 1 warning`，Python 编译和
  diff 检查通过。该结果证明纠错契约和无回归，不代表真实业务或 sealed-test 的回答准确率提升。
- S5 检索测试与分块诊断增量：参考 RAGFlow 的可检查分块/检索面板思路，在项目文档库增加只读
  “检索测试”入口。`/diagnostics/retrieval` 复用当前项目优先、全局回退的检索链路，返回有上限的
  分块正文、`section_path`、排序分数、问题相关支持度、路由和充分性信号；同时展示现有确定性
  查询变体供用户选择重测。接口要求工作区解锁，索引更新时拒绝测试，未知项目返回 404，且只加载
  知识库、不加载聊天模型；失败审计只保留查询长度和异常类型，不记录查询原文。逐项契约见
  `eval/results/rag_retrieval_test_ui_recheck_20260830.json`。
- S5 检索测试增量验证：后端诊断定向 `3 passed`，前端 `npm test`、`npm run build`、Python
  编译和 diff 检查通过；随后后端完整回归为 `715 passed, 1 warning`。该结果证明接口、字段边界和
  UI 构建无回归，不代表检索或回答准确率已经提升。
- S5 Playbook 增量：参考 Claude Code 权限规则与现有确认门，将论文精读、实验对比和会议行动项
  三条业务工作流定义为 `1.0` 版本 Playbook，显式声明输入契约、输出、证据范围、副作用和确认点；
  在项目目录新增 `playbook_approvals`，`/chat` 与业务计划接口在模型/RAG执行前强制检查当前项目
  审核状态，版本过期返回冲突，审核可撤销。前端展示版本、审核状态、输入契约和副作用边界，并提供
  审核/撤销入口。逐项证据见 `eval/results/rag_playbook_contract_recheck_20260830.json`。
- S5 Playbook 增量验证：后端工作流、项目目录和学习服务定向 `42 passed, 1 warning`，前端
  `npm test` 与 `npm run build` 通过；随后后端全量回归为 `724 passed, 1 warning`，Python
  编译和 diff 检查通过。该结果证明契约和权限边界，无端到端业务质量或回答准确率结论。
- S5 产物 provenance 增量：参考 Claude Code 将权限和审核状态作为模型之外的强制边界，
  为项目目录 `artifacts` 增加兼容迁移后的结构化确认记录与变更摘要；邮件/日程草稿保存
  生成计划、项目内可解析的来源版本和未解析来源标记；PPT 生成在当前项目下创建并更新
  `presentation_draft` 产物，绑定任务 ID、生成结果、证据页/来源覆盖、来源版本和每个来源的
  解析状态。未选择项目时 UI 不启动 PPT 生成，未能映射到项目版本的全局文档明确保留为
  `unresolved`，不伪造 `V#`。逐项证据见 `eval/results/rag_artifact_provenance_recheck_20260830.json`。
- S5 产物 provenance 增量验证：项目目录/草稿/PPT 定向 `30 passed, 1 warning`，前端
  `npm test`、`npm run build`、Python 编译和 diff 检查通过；后端全量回归为 `726 passed, 1 warning`。
  该结果证明字段迁移、状态记录和现有三类产物链路无回归，不代表所有事实已经完成
  `E#/V#/A#/M#` 级语义校验。
- S5 项目/全局 GraphRAG 一致性增量：参考 Microsoft GraphRAG 的查询范围分路和 RAGFlow 的
  原始分块可追溯思路，项目级 GraphRAG 不再遮蔽 `global_shared`。存在项目 ID 时同时读取
  `project:<id>` 与 `global_shared`，回读原始 chunk 时分别施加项目域和 `global_only` 过滤；社区
  摘要仍只做导航，事实仍必须来自原始证据。定向 GraphRAG 回归 `18 passed`，后端完整回归
  `728 passed, 1 warning`。逐项契约和限制见
  `eval/results/rag_project_global_graph_recheck_20260830.json`；该结果证明链路一致性和无回归，
  不代表真实业务回答准确率或 sealed-test 提升。
- S3 慢视觉预热增量：依据现有单样本冷启动 `13234.199 ms`、显式预热后请求 `2661.241 ms`
  的差异，将 VLM 预热绑定到用户明确选择“深度理解图片”或“目标复核”后的后台生命周期；进一步
  复用后端已有的图表/图标/布局等自动慢路由提示，以及已审核工作流的 slow 路由，在用户提交前
  提前加载。相同图片去重，失败可重试，预热期间阻止并发感知以避免模型加载竞态。前端 `npm test`
  和 `npm run build` 通过。该增量只改变等待时机，不证明完整链路 P95<5 秒；OCR 低置信度等运行时
  才能确定的分支仍可能冷启动，逐项限制见 `eval/results/multimodal_auto_warmup_recheck_20260830.json`。
- S5 claim-to-citation 支持门增量：参考 RAGChecker 的检索/生成分开评估和 CRAG 的质量不足
  后受控重试，为生成后的每条带引用结论增加本地可解释支持检查；支持判定只使用章节、章节路径、
  摘录和原始上下文，标题/文件名只用于来源定位，不单独支撑事实。引用内容主题覆盖不足，或
  数字/百分比/规格事实不在被引用证据中时，进入现有重试、引用修复或抽取式回退；`rag.completed`
  同步记录检查模式、检查断言数和不支持断言数。该门不增加模型调用，也不把词法代理称为 NLI。
  答案质量与生成定向 `76 passed`，后端全量回归 `736 passed, 1 warning`；逐项边界见
  `eval/results/rag_claim_support_recheck_20260830.json`。
- S5 答案级离线评测增量：在 `scripts/eval_rag.py` 中复用生产 `semantic=true` 支持门，分开
  报告引用 ID 合法率、引用内容支持率、已检查 claim 数和不支持 claim 数；固定的来源/章节/证据
  定位脚手架不进入事实 claim 分母，但无引用的实际结论仍失败。对已保存的
  `rag_cn_raw_v0.1/dev` 16 条答案只读重算：引用合法率、引用内容支持率和可回答题达标率均为
  `100%`，检查 39 个 claim、0 个不支持；这不是新模型或实时准确率提升。逐项口径和限制见
  `eval/results/rag_answer_quality_eval_recheck_20260830.json`。
- S2 实验比较严格门增量：参考 MLflow 将指标、参数、数据集和产物绑定到同一次可复现 run，
  比较器现在要求项目、数据集版本、评测脚本和完整指标集合（名称/单位/定义版本/split/方向）
  同时通过；只要存在部分匹配、缺失或不兼容指标，就保留缺失项与匹配计数但返回
  `change_computation=blocked`、`changes=[]`，不再泄露可能被误读的绝对/百分比变化。新增
  部分指标匹配和数据集不一致回归；实验账本/API 定向 `19 passed`，后端全量 `737 passed, 1 warning`。
  逐项契约和限制见 `eval/results/experiment_comparability_gate_recheck_20260830.json`；该结果证明
  比较门控无回归，不代表真实实验结论或 RAG 准确率提升。
- S5 GraphRAG 证据身份增量：参考 Microsoft GraphRAG 将图谱结果用于查询范围导航、再回读原始
  证据的设计，合并 GraphRAG 原始切片与普通混合检索时按候选身份去重，保留更高证据分数，
  并在排序后统一重编号 `E1..En`；这样中间路由、诊断和上下文门不会因两路各自从 `E1` 开始而
  产生引用歧义，且 documents/metadatas/evidence 三列保持同步。GraphRAG/RAG 相关定向
  `72 passed`，后端全量 `738 passed, 1 warning`；逐项契约和限制见
  `eval/results/rag_graph_evidence_identity_recheck_20260830.json`。该结果证明证据身份无回归，
  不代表真实业务 RAG 准确率或 sealed-test 提升。
- S5 有界证据门增量：在不扩大上下文预算的前提下，将检索后“证据是否回答问题”的检查范围
  从前 3 条扩展到既有最终 Top-6；对已保存的主检索结果审计发现 324 条有目标来源的样本中
  有 8 条首个目标来源落在第 4–6 位，说明原范围可能漏掉已召回的全局证据。新增 E4 命中回归，
  RAG/项目隔离/答案链定向 `96 passed`，后端全量 `739 passed, 1 warning`。逐项口径和限制见
  `eval/results/rag_bounded_evidence_gate_recheck_20260830.json`；这是误拒答风险修复，不能解释为
  实时回答准确率提升。
- S2/S5 实验测量入对话增量：参考 MLflow 的 run 级指标、参数和产物绑定原则，将实验比较器中
  通过项目、数据集版本、评测脚本和完整指标集合门控的结果转换为独立 `M#` 证据；证据保留基线/当前
  数值、变化、split、指标定义和两侧源文件/行定位，且明示配置或环境差异只能作为候选因素。主 `/chat`
  仅接受用户显式选择的 `baseline_run_id` 与 `current_run_id`，不自动猜测“上个月”；计划接口与聊天接口
  均在模型生成前阻断缺失、跨项目或不可比运行。实验对比 UI 增加基线/当前运行选择器，生成引用校验、
  回退、思维导图和离线评测同步支持 `M#`；证据面板将 M# 的基线/当前数值、变化、split、定义版本和两侧
  源定位单独呈现，并明确“可比性通过”不等于因果已证实。定向链路 `120 passed`，后端全量 `746 passed, 1 warning`，
  前端测试/构建、Python 编译和差异检查通过；逐项证据见
  `eval/results/rag_experiment_measurement_bridge_recheck_20260830.json`。该结果证明结构化测量证据链和
  阻断契约无回归，不代表真实模型回答准确率、因果归因或 sealed-test 提升。
- S3/S5 多模态证据桥增量：参考 RAGFlow 对多模态文档结构化处理和 UniDoc-Bench 对文本、表格、
  图像证据联结的设计，修复语音 `A#` 未强制用户确认的边界：未显式确认的 ASR 不进入证据，录音
  发送前由 UI 展示转写并要求确认。PDF 中的图/示意图元数据现在以稳定 figure ID、页码、caption、
  locator 和视觉状态挂到同页首个文本父块，进入受限 `[VISUAL_ASSETS]` 检索上下文；视觉问题的
  结构偏好、`E#` 图像资产定位、支持度检查和证据面板同步接入。定向回归 `157 passed`，后端全量
  `750 passed, 1 warning`，前端测试/构建、Python 编译和差异检查通过；逐项证据见
  `eval/results/rag_multimodal_evidence_bridge_recheck_20260830.json`。该增量只证明确认边界和
  图像资产可追溯链路，无真实多模态准确率、图表数值理解或完整链路 P95 提升结论。
- S5 Playbook 输出契约增量：参考成熟 Agent 的显式任务契约思路，为论文精读、实验对比和会议行动项
  分别声明必需输出章节、允许使用的 `E#/V#/A#/M#` 证据类型和统一的“待确认”缺证据标记。契约只在
  生成提示阶段注入，不把格式词加入检索查询；任务面板在执行前显示预期结构，完成事件记录确定性的
  章节完整度检查。新增回归 `65 passed, 1 warning`，后端全量 `753 passed, 1 warning`，前端测试/构建、
  Python 编译和差异检查通过；逐项证据见 `eval/results/rag_playbook_output_contract_recheck_20260830.json`。
  该增量只提升业务结果的可预期性和可审计性，不宣称事实准确率、引用正确率或多模态基准提升。
- 验证结果：Tauri `cargo fmt --check`、`cargo check`、`cargo test`（2 passed）；前端
  `npm test` 和 `npm run build` 通过；UIA/运行时/交互上下文定向回归 48 passed；后端
  完整回归 `715 passed, 1 warning`；解析、重排、引用和普通文档层级定向回归 `58 passed`。构建期间仅有现存的前端 bundle 体积提示和 Rust linker stdout
  警告。

仍未达标、不得提前宣称完成：

- 尚未完成至少 30 条独立真实跨应用 episode，也没有据此计算 UIA 目标准确率、完整链路
  P50/P95 或多模态相对 text-only 的稳定增益；本轮编译和单元测试不替代真实业务测量。
- UIA 真实生产者的跨应用覆盖、焦点竞态、权限受限窗口和与 OCR/VLM 的成对消融仍需在
  冻结 episode 上验证；S3 的跨应用模型预热收益、裁剪和跨场景缓存评测仍未整体完成，图标目标超过 1.9%
  基线和完整链路 P95<5 秒门槛仍保持未完成。UIA-first 的单次对照不能替代多样本消融。
- S5 本轮已完成 `section_tree_v1` 的确定性节点元数据、父子/祖先链路由和旧索引回退，但尚未在
  正式加密工作区重建现有索引，也尚未在长论文/标准真实样本上完成完整检索、回答和多文档引用对照；
  当前隔离 dev 证据仍主要证明无回归和路由可观测性，不能据此宣称 RAG 精度提升。
- S5 检索测试已补齐用户确认后的关键词修正闭环，但当前只按规范化原查询精确匹配，且必须由用户
  明确点击保存；尚未启用未经评测的相似问题语义泛化。Playbook 的用户自定义/版本迁移仍未完成；
  本轮已增加跨 Playbook 的引用命名空间边界，但自动事实级 `E#/V#/A#/M#` 语义映射和产物级校验
  仍未完成。PPT 和草稿目前已记录来源
  版本/解析状态，但未绑定项目目录的全局文档没有 `V#`，历史未带项目的工具调用也不会凭空补写
  项目归属。
- 项目/全局 GraphRAG 联合检索已在隔离回归中锁定项目索引不遮蔽全局共享索引，但正式加密工作区
  的索引重建、跨项目真实问答、真实/ sealed-test 答案级引用语义对照仍未完成；不得把隔离回归的
  `18 passed` 或后端 `736 passed` 解释为 RAG 准确率提升。
- S3 自动预热目前覆盖用户明确选择慢视觉/目标复核、已审核工作流 slow 路由和已知问题/UI 目标
  提示；OCR 低置信度等运行时才确定的分支仍可能承担冷启动，且尚未完成真实跨应用、多样图片和
  完整回答的 P50/P95 测量，因此“预热后单样本 2.66 秒”不能替代路线图要求的完整模态 P95<5 秒。
- 生成后的 claim-to-citation 目前是确定性词法支持代理，不是完整 NLI/事实真值验证；本轮仅增加
  秒/毫秒/分钟/小时/天及明确二进制存储单位的有界规范化，未知或有歧义的单位不换算。同义改写、
  复杂单位/币种换算、跨段落推理和 PPT/邮件/日程事实级 `E#/V#/A#/M#` 映射仍需在冻结数据上
  单独评测。dev 保存结果的 `100%` 支持率只是评测器重算结果，不能解释为实时回答准确率提升。

本轮未修改 sealed-test 主评测口径、未混算不同数据集结果，也未把 UIA 的结构化焦点
信息当作文档事实证据；事实性回答仍必须经过现有 RAG 证据门并带可定位引用。

## 17. 本轮继续执行记录（2026-08-30，S4 个性化管理与评测契约）

本轮转入 S4，只实现可在当前本机工作区验证的偏好画像增量。设计借鉴 Mem0 的
user/project 作用域、Letta 的可检查/可演进记忆和 Claude Code 的模型外确认边界，未直接
复制其代码或引入外部记忆服务。

已落地并有回归证据：

- 结构化偏好记录增加确认状态、确认时间、最后实际注入时间、记录版本、项目范围和原始
  候选消息追踪；来源消息索引此前只保存在候选表，本轮在准入时同步写入加密记忆，避免
  管理界面出现“有候选但无来源”的断链。
- 新增受保护的 `PATCH /learning/memories/{memory_id}`、`DELETE /learning/memories/{memory_id}?confirm=true`
  和带 `project_id` 的管理查询。编辑在同一事务内重写 `memories` 与 `learning_chunks` 的
  加密内容和摘要，已验证旧标题/内容不再被活动检索块召回；删除按显式确认从活动索引
  移除并通过现有加密持久化/外部向量清理入口留下审计记录；停用继续使用可追溯的软撤销。
- 前端新增“偏好画像”面板，显示当前项目 + 全局的范围策略、确认状态、版本、确认/最近
  使用时间和来源摘录，支持保存修改、停用和二次确认删除；项目 A 的偏好不会出现在项目 B
  的管理列表中。
- 实际被读取门纳入问答上下文的偏好会更新 `last_used_at`；超过 30/90/180 天的偏好
  依次采用有界读取权重 `1.0/0.85/0.65/0.45`，未人工确认的运行时显式信号从创建时间开始
  衰减，不自动删除也不覆盖事实证据。
- 对明确相反的 `user_preference`，读取门不再把两条规则同时注入模型；当前项目范围优先于
  全局范围，人工确认优先于未复核信号，落选项以 `conflicting_preference` 记录在拒绝列表中。
- 新增 `scripts/eval_personalization.py`、成对样例和 schema：同一问题、同一允许证据、
  只替换画像，分别检查 Preference Adherence、Factual Invariance、Citation Invariance、
  Irrelevant Preference Rejection 和 Cross-project Leakage Rate；重复 ID、空数据、错误
  schema、非法引用 ID 和无引用结果均失败关闭。种子 2 条仅用于验证评测契约。
- 写入侧补齐中文自动/手动、每次回答/关闭前、需/无需审核、中文/英文等冲突候选识别；
  不相关记忆读取门、冲突候选和项目切换均有回归测试。

逐项证据见 `eval/results/personalization_profile_management_recheck_20260830.json`，成对
评测种子结果见 `eval/results/personalization_pair_seed_recheck_20260830.json`。

验证结果：新增偏好存储/API/读取门/评测定向回归此前为 `58 passed, 1 warning`，冲突读取门
追加定向回归 `14 passed`；后端完整回归 `764 passed, 1 warning`；前端 `npm test`、
`npm run build`，Python 编译和 diff 检查通过。
前端仍只有既有 bundle 体积提示；以上结果证明管理契约和无回归，不代表真实用户偏好遵循率、
多模态回答准确率或竞赛完成门槛已达标。

S4 验收门仍需保留：至少 30 条独立真实 episode 的成对画像评测、真实项目切换与冲突样本、
事实/引用人工复核、用户撤销后的残留率，以及和 text-only 的同条件端到端比较尚未完成。
因此本节将 S4 任务标记为已实现，但不把 S4 或整条路线图标记为完成，也不改变 sealed-test
主评测口径。

## 18. 本轮继续执行记录（2026-08-30，S5 用户确认检索反馈）

本轮针对 S5 的检索测试缺口，参考 RAGFlow 的分块/检索测试与可调试检索链路，以及 CRAG 的
“先评估检索质量、再纠错重检索”原则，增加最小的用户确认反馈闭环；没有复制外部项目代码，也
没有把单次用户选择直接变成全库自动规则。

已落地并有回归证据：

- 加密学习库新增 `retrieval_feedback` 表，保存原查询和用户确认的改写；数据库只保留带工作区
  密钥的查询摘要和加密负载，支持全局/当前项目两种范围，同一范围同一查询只保留最新有效改写。
- 检索测试增加“记住全局”“记住当前项目”和“撤销”操作；撤销要求显式 `confirm=true`，不会
  修改原文档、分块或向量/关键词索引。接口和审计只记录长度、范围、ID 等元数据，不记录查询
  原文。
- `/chat` 和 `/diagnostics/retrieval` 都读取当前查询的有效反馈；明确保存的改写以最多 3 条额外
  查询变体进入密集、关键词、标题匹配和重排前候选召回，原始用户问题仍是证据充分性和引用校验
  的唯一依据。项目反馈仅在对应项目生效，全局反馈可在各项目复用。
- 反馈仅使用规范化原查询的精确摘要匹配，未引入模型自动改写、未经确认的相似度泛化或自动写入
  文档索引；查询变体上限、内容长度和撤销路径均有界。

逐项代码与回归证据见 `core/learning_store.py`、`core/knowledge_base.py`、`core/rag_engine.py`、
`core/mcp_server.py`、`core/interaction_context.py`、`souldrive-ui/src/App.tsx`、
`eval/results/rag_retrieval_feedback_recheck_20260830.json`。

本轮验证：学习库存储、检索、交互上下文和聊天/API 定向回归 `142 passed, 1 warning`；前端
`npm test`、`npm run build`、Python 编译和差异检查通过。该结果证明反馈契约、范围隔离、撤销
和无回归，不代表真实用户反馈后的召回率、MRR、答案准确率或 sealed-test 已提升。

仍未达标：反馈样本量、真实长文档/标准对照、反馈前后同条件 Recall@K/MRR/nDCG、跨项目真实
问答、事实级引用语义验证，以及 S3 多模态 30 条真实 episode 和完整链路 P95 门槛仍需在冻结
数据上单独完成；本轮不改变 sealed-test 主评测口径。

## 19. 本轮继续执行记录（2026-08-30，S5 Playbook 引用命名空间边界）

本轮继续针对 S5 的证据可靠性缺口，参考 RAGChecker 将检索与生成诊断拆分、以及 Claude Code
将权限边界放在模型输出之外的做法，为三个业务 Playbook 增加机器可读的引用命名空间门：
`paper_review` 允许 `E#/V#`，`experiment_compare` 允许 `E#/V#/M#`，`meeting_actions`
允许 `A#/E#/V#`。该门在现有引用 ID、claim 覆盖和词法支持校验之后执行；不通过时沿用一次
有限重试，仍失败则保持现有保守回退，不把不匹配的证据类型变成事实。

实现与定向证据见 `core/multimodal_workflows.py`、`core/rag_engine.py`、
`eval/results/rag_playbook_citation_namespace_recheck_20260830.json`。本轮定向后端回归为
`68 passed, 1 warning`，覆盖 Playbook 合法/非法命名空间、生成重试和全局反馈项目边界。
该增量只证明证据类型隔离和无回归，不证明事实真值、答案准确率或多模态指标提升。

仍未完成：Playbook 用户自定义/版本迁移，事实级 NLI/语义引用验证，跨段推理与 PPT/邮件/日程
产物级 `E#/V#/A#/M#` 映射，以及真实 30 条多模态 episode、完整链路 P95、真实长文档/标准
对照和 sealed-test 评测。

## 20. 本轮继续执行记录（2026-08-30，S5 事实引用单位规范化）

本轮针对路线图中“数值单位换写”的剩余缺口，继续沿用 RAGChecker 的细粒度 claim 支持诊断和
CRAG 的不足证据后纠错边界。事实支持门现在会把无歧义的时间单位规范到 `time_ms`，把字节与
明确二进制存储单位规范到 `bytes`；百分比与小数的既有等价规则保持不变。未知单位以及可能
存在十进制/二进制歧义的 `KB/MB` 不自动换算。该变化只减少同一事实不同单位表达造成的误拒答，
不同数值仍然失败，不改变召回或证据充分性门。

逐项代码与回归证据见 `core/answer_quality.py`、`tests/test_answer_quality.py`、
`eval/results/rag_fact_unit_normalization_recheck_20260830.json`。定向回归为 `36 passed`。
该结果只证明有界单位规范化契约，无实时答案准确率、NLI 真值或 sealed-test 提升结论。

仍未完成：同义改写、跨段推理、复杂单位/币种换算、事实级 NLI、产物级 `E#/V#/A#/M#` 映射，
以及真实长文档/标准、多模态 30 条 episode、完整链路 P95 和 sealed-test 评测。

## 21. 本轮继续执行记录（2026-08-30，慢视觉输出与指示性问题路由）

本轮继续针对多模态“能看到但回答不完整、明确指着画面提问却未稳定进入视觉链路”的业务缺口，
参考 [Docling](https://github.com/docling-project/docling) 的结构化文档对象与来源追踪原则、
[PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR) 的任务相关结构化输出原则，以及
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 的图像检索/理解分路设计。
本轮只借鉴设计原则，没有复制外部项目代码、模型或依赖。

已落地：

- 视觉慢路径不再与快速 OCR 共用过低的输出预算：普通视觉描述最低 64 tokens，目标定位/结构化候选
  最低 128 tokens，统一上限 256；快速 OCR 路径仍保持原有 24 tokens，避免无关的延迟和资源变化。
- 视觉阶段记录调用方请求预算与实际生效预算，便于解释“为什么输出被截断”并支持后续 P50/P95 评测。
- 对“这个是什么”“这个数字代表什么”“哪个按钮”等指示性问题：存在用户选区时进入视觉证据链；
  没有选区时必须有明确的图片/截图指代才进入视觉链，避免截图上下文劫持无关的文档 RAG 问题。
  普通“RAG 核心思想”等问题仍走文档证据链，不因当前有截图而误路由。

逐项证据见 `core/topic5_runtime.py`、`core/interaction_context.py` 和
`eval/results/multimodal_slow_output_contract_recheck_20260830.json`。

本轮验证：视觉运行时与交互上下文定向回归 `43 passed, 1 warning`；后端完整回归 `781 passed, 1 warning`；
前端 `npm test`、`npm run build`、Python 编译和差异检查通过。前端构建仍只有既有 bundle 体积提示。
这些结果证明输出契约、指示性问题路由和现有功能无回归，不证明 VLM 识别准确率、图标/图表准确率或端到端
延迟已经提升。

仍未完成：真实 30 条多模态 episode、跨应用 UIA 覆盖、完整链路 P50/P95（包括此前 24.7 秒慢路径）、
长文档/标准的真实加密索引重建、事实级语义验证和 sealed-test。当前没有重建加密生产工作区索引，
也不把本轮输出预算修复标记为 S3 或整条路线图完成。

## 22. 本轮继续执行记录（2026-08-30，结构化视觉事件不丢失）

本轮针对真实运行结果在问答层被静默丢弃的链路缺口，参考 [Microsoft OmniParser](https://github.com/microsoft/omniparser)
先将屏幕感知结果组织为结构化元素、再交给目标绑定的思路，以及 [LlamaIndex 多模态文档](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html)
对视觉理解与文本检索分路的做法。没有引入外部模型或复制代码。

已落地：

- 问答上下文现在同时接受 `image`、`image_region` 和 `ocr_region` 三类带文本视觉事件；它们统一进入
  `V#` 视觉证据命名空间，并保留 `source_modality` 记录原始来源。
- `image_region`/`ocr_region` 不再因不是字面上的 `image` 而无法触发视觉问题路由或视觉证据门；
  无文本的 UIA、普通 `screen` 元数据仍不会被当作视觉事实，避免凭空生成引用。
- 该修复只补齐事件到证据的传播，不放宽用户授权、选区时效、视觉置信度门或项目文档证据门。

逐项证据见 `core/interaction_context.py`、`tests/test_interaction_context.py`、`tests/test_rag_prompting.py` 和
`eval/results/multimodal_structured_event_contract_recheck_20260830.json`。

本轮定向回归为 `133 passed, 1 warning`。本轮之前的完整后端回归、前端测试、前端构建和 Python 编译均已通过；
在本轮文件写入完成后仍需再跑完整后端、编译和差异检查。该增量只证明结构化视觉事件不再静默丢失，不证明
图标/图表识别准确率、多模态相对 text-only 增益或完整链路 P95 已达标。

仍未完成：真实 30 条独立 episode、跨应用 UIA/不同分辨率和说话人覆盖、图标目标超过约 1.9% 基线、
完整回答 P95 小于 5 秒、视觉置信度校准、长文档/标准的真实加密索引重建和 sealed-test。当前仍不把
S1/S3 或整条路线图标记为完成。

## 23. 本轮继续执行记录（2026-08-30，视觉直答与项目事实证据门分流）

本轮继续针对 S3 验收项“无关截图不能绕过普通 RAG 证据门”，并处理视觉与项目事实混合提问的边界。
设计参考 [Microsoft OmniParser](https://github.com/microsoft/omniparser) 的结构化屏幕元素后续处理、
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 的视觉/文本分路，
以及 [RAGChecker](https://arxiv.org/abs/2408.08067) 对检索证据与生成质量分开诊断的原则；没有复制代码或引入依赖。

已落地：

- 新增确定性的“项目事实依赖”判定。纯粹询问当前画面可见内容（例如“这个图表显示了什么”）仍允许
  使用 V# 视觉证据直接回答；涉及原因、项目、实验、论文、配置、数据集、版本、对比或上个月等内容时，
  标记为需要项目文档证据。
- 视觉证据不再为上述混合问题绕过文档 `evidence_gate`。文档证据不足时不调用生成模型，不把截图中的观察
  推断成项目结论，同时保留本轮视觉证据块和明确的证据缺口说明。
- 审计和 Agent 状态栏记录 `visual_document_required`，便于后续逐样本评测分辨视觉直答与视觉+RAG 联合路径。
  用户授权范围、选区时效、视觉置信度门和项目隔离规则均未放宽。

逐项证据见 `core/interaction_context.py`、`core/rag_engine.py`、`tests/test_interaction_context.py`、
`tests/test_rag_prompting.py` 和 `eval/results/multimodal_visual_document_gate_recheck_20260830.json`。

本轮定向回归为 `135 passed, 1 warning`；新增代码写入后的完整后端回归为 `786 passed, 1 warning`，
Python 编译和差异检查通过。前端本轮没有改动，既有 `npm test` 与 `npm run build` 结果保持有效。该增量证明已知的视觉证据绕过路径
被关闭，不证明分类器覆盖所有自然语言、答案事实真值、检索召回率或多模态基准准确率提升。

仍未完成：真实 30 条独立 episode、复杂混合问题的人工标注与成对评测、跨应用 UIA、图标/图表准确率、
完整回答 P95 小于 5 秒、事实级语义/NLI 验证、生产加密索引重建和 sealed-test。当前仍不把 S1/S3 或整条
路线图标记为完成。

## 24. 本轮继续执行记录（2026-08-30，真实 episode 采集契约与集合审计）

本轮继续推进 S1 的数据先行门槛，针对“接口能运行但无法证明真实任务质量”的缺口，参考
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 对结构化 UI 目标的表达、
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 对媒体与文本通道分路，
以及 [RAGChecker](https://arxiv.org/abs/2408.08067) 对检索与生成分开评测的原则。只借鉴数据契约和审计思路，
没有复制外部代码、模型或引入新的运行时依赖。

已落地：

- `episode_v0.1` 在保持既有 seed 兼容的前提下，增加可选的说话人匿名 ID、采集应用、屏幕分辨率、目标类别
  和归一化目标框；目标框只保存用户授权区域的结构化标注，不扩展屏幕采集范围。
- `scripts/ingest_real_mmi_episode.py` 支持显式写入上述采集元数据，并从实际截图读取分辨率；非法、越界或空目标框
  不会被静默写入。
- 新增只读 `scripts/audit_real_mmi_collection.py`。审计器在严格真实模式下检查 schema/媒体引用、episode/task/event
  唯一性、对齐事件引用、语音/音频/屏幕三模态完整性、真实授权 provenance、说话人/分辨率/项目多样性，以及文字/图标/图表、
  时间错位、项目冲突、低置信度和 OOS 覆盖；损坏 JSON、非 UTF-8、任务卡或 manifest 会失败即停，不生成或修补样本。
- 对现有 30 条合成 seed 和 3 条媒体 smoke 集合进行了只读实跑。两者均通过各自的 schema/唯一性/媒体契约，但报告明确
  `real_capture_claim=false`，没有把合成数据当成真实多模态成绩。
- 对仓库已有的 `data/local/souldrive_pc_mmi_real_v0.1` 做了严格只读审计：10 条样本的 schema 和媒体引用有效，
  但因缺少新采集元数据、跨说话人/分辨率/项目和困难样本覆盖而按预期失败；这确认了当前真实 pilot 不能直接作为 S1
  完整验收集，也没有修改其中的私有音频、截图或标注。

逐项证据见 `eval/schemas/episode_v0.1.schema.json`、`scripts/ingest_real_mmi_episode.py`、
`scripts/audit_real_mmi_collection.py`、`tests/test_real_mmi_collection.py` 和
`eval/results/real_mmi_collection_audit_contract_recheck_20260830.json`。

本轮增量回归：采集集合测试 `5 passed`，评测资产测试 `27 passed`，全后端回归 `789 passed, 1 warning`；
Python 编译和差异检查通过，seed 与 media smoke 集合审计均返回 `valid=true`，真实 pilot 严格模式按预期返回
`valid=false`（10/30 且多样性/困难覆盖不足）。前端没有改动。上述结果只证明
采集契约与审计器可复现，不证明目标绑定、意图、冲突识别、回答、RAG、首 token 或完整回答延迟。

仍未完成：用户主动授权的至少 30 条独立真实 episode、不同说话人/分辨率/项目/指代方式、逐阶段 ASR/OCR/VLM/对齐/检索/
首 token/完整回答延迟、成对 text-only/完整模态逐样本冻结、图标/图表准确率和完整链路 P95 小于 5 秒。当前不勾选 S1
真实数据验收项，也不把合成 seed 审计结果写成竞赛成绩。

## 25. 本轮继续执行记录（2026-08-30，工作流视觉事件契约对齐）

本轮继续修复多模态从感知到业务工作流之间的实际断点。参考 [Microsoft OmniParser](https://github.com/microsoft/omniparser)
对结构化视觉元素的表达，以及 [LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html)
对视觉通道独立处理的思路，没有复制代码或引入外部模型。

已落地：

- 修复 `/chat` 工作流准备层只识别 `modality=image` 的过窄判断；当前运行时产生的 `image_region`、`ocr_region`
  事件，只要带有文本、授权框或结构化候选，就能正确进入视觉工作流准备状态。
- 该改动只修正“是否提供了视觉输入”的契约，不改变视觉置信度门、用户授权边界、当前项目隔离、项目文档证据门或引用校验。
  因此实验对比等工作流不会再因事件标签不同而误报缺少屏幕证据，但仍会在后续证据不足时拒答。

证据见 `core/mcp_server.py`、`tests/test_multimodal_workflows.py` 和
`eval/results/multimodal_workflow_visual_event_recheck_20260830.json`。定向 API 回归为 `18 passed, 1 warning`；
本轮修改没有前端变更，新增后的完整后端回归为 `790 passed, 1 warning`。

仍未完成：真实 30 条 episode、跨应用/分辨率/说话人覆盖、图标/图表目标准确率、VLM 慢路径和完整回答 P95 小于 5 秒，
以及完整模态相对 text-only 的冻结成对评测。该修复不构成多模态准确率提升结论。

## 26. 本轮继续执行记录（2026-08-30，pilot 阶段级评测账本）

本轮继续落实 S1 “每条 episode 记录 ASR、OCR、VLM、对齐、检索、首 token 和完整回答延迟”的可审计要求，
参考 [RAGChecker](https://arxiv.org/abs/2408.08067) 对检索与生成分层诊断的原则，以及
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 对结构化视觉阶段输出的思路。没有复制外部代码或模型。

已落地：

- `scripts/eval_real_mmi_pilot.py` 新增逐 episode `stage_ledger.jsonl`，统一记录 ASR、OCR、VLM、SoulAlign、
  retrieval、first token、complete answer 七个阶段。
- 没有执行的阶段使用 `not_integrated`/`not_measured` 和 `latency_ms=null`；运行时明确跳过的阶段也不再把 `0 ms`
  当作真实耗时，避免把感知 pilot 误读成完整 RAG 评测。
- 失败文件现在包含 `error_types` 和澄清期望/预测/正确性，便于后续按错误类型进行成对回归，而不是只看一个聚合准确率。
- 对当前 `data/local/souldrive_pc_mmi_real_v0.1` 的 10 条真实授权 pilot 完成了本地快路径实跑：ASR/OCR/SoulAlign 均
  `pass=10`，VLM `skipped=10`，检索/首 token/完整回答均 `not_integrated=10`；意图准确率 0.9，目标绑定准确率 1.0，
  冷启动 23382.006 ms，感知热运行 P95 1694.38 ms。原始阶段账本保留在 `data/local`，评测目录只保存聚合结果。

证据见 `scripts/eval_real_mmi_pilot.py`、`tests/test_eval_real_mmi_pilot.py` 和
`eval/results/real_mmi_stage_ledger_recheck_20260830.json`。定向测试 `2 passed`，本轮之前的完整后端回归为
`790 passed, 1 warning`；本轮只改评测脚本与文档，下一轮继续执行时再进行全量回归。

这次实跑明确暴露两个优先项：ASR 首次加载是冷启动主要瓶颈，且完整 RAG/回答链尚未纳入 pilot evaluator。当前结果不证明
完整回答 P95、小于 5 秒门槛、VLM 识别准确率或多模态相对 text-only 的稳定增益。

仍未完成：至少 30 条独立真实 episode、跨说话人/分辨率/项目/指代方式、困难样本覆盖、VLM 慢路径评测、完整 RAG/首 token/
完整回答逐样本冻结，以及相应的真实端到端 P50/P95。

## 27. 本轮继续执行记录（2026-08-30，ASR/OCR 感知层显式预热）

本轮针对第 26 节阶段账本暴露的最高优先级问题——ASR 首次懒加载造成约 23 秒冷启动——继续参考
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 的结构化感知阶段表达，以及
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 的按需分路原则。
没有复制外部代码、模型或依赖。

已落地：

- `Topic5Runtime.warmup_perception` 显式加载本地 ASR/OCR，逐阶段返回 `pass`、`blocked` 或 `failed`、是否已加载、
  阶段耗时和安全错误信息；部分成功会保留已加载阶段，但总体不会伪装成全成功。
- 新增受工作区、软件解锁和 API token 保护的 `POST /multimodal/runtime/warmup-perception`，只执行本地模型构造，
  不录音、不读取屏幕、不执行推理；写入精简审计事件。原有仅 VLM 的 `/multimodal/runtime/warmup` 保持兼容。
- `scripts/eval_real_mmi_pilot.py` 增加 `--warmup-perception`，将预热 setup 成本单独写入
  `perception_warmup.json` 和 metrics，不把它混入回答请求延迟。

真实本机 smoke 中 ASR 预热 `9448.647 ms`、OCR 预热 `565.285 ms`、总预热 `10052.369 ms`，状态为 `pass`。
在同一 10 条本地真实 pilot 集合的两次运行中，未显式预热时首请求为 `23382.006 ms`；显式预热成本为
`9856.794 ms`，预热后首请求为 `2138.695 ms`，观察到首请求下降 `90.853%`。意图准确率均为 `0.9`，
目标绑定准确率均为 `1.0`。这证明本机当前实现确实能前移 ASR/OCR 加载成本，但不是完整回答 P95 或竞赛成绩。

证据见 `core/topic5_runtime.py`、`core/mcp_server.py`、`scripts/eval_real_mmi_pilot.py`、
`tests/test_topic5_runtime.py`、`tests/test_eval_real_mmi_pilot.py` 和
`eval/results/multimodal_perception_warmup_recheck_20260830.json`。本轮定向回归为 `36 passed, 1 warning`；
新增后的全后端回归为 `794 passed, 1 warning`，Python 编译和差异检查通过。

仍未完成：VLM 慢路径预热与完整回答链路的独立测量、真实 30 条 episode、跨应用/分辨率/说话人覆盖、图标/图表
准确率、完整回答 P95 小于 5 秒、完整模态相对 text-only 的冻结成对评测、生产加密索引重建和 sealed-test。
因此不勾选 S3 的整体模型预热/延迟验收项，也不把本轮 ASR/OCR 局部改善写成完整多模态准确率提升。

## 28. 本轮继续执行记录（2026-08-30，前端结构化视觉证据门对齐）

本轮复核发现，后端已经接收 `image_region`/`ocr_region`，但前端发送前的保护逻辑仍只按字面
`modality=image` 判断，存在“感知成功、发送前被误拦截”的实际业务断点。修复参考
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 的结构化屏幕元素表达和
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 的视觉通道分路原则，
没有复制代码或引入依赖。

已落地：

- 新增 `hasUsableVisualEvidence`，前后端一致接受 `image`、`image_region`、`ocr_region` 中带有文本、授权框或候选目标的证据；
  UI Automation 和普通 `screen` 元数据不会被误当成视觉事实。
- 发送前视觉证据门改用该结构化判定，runtime 事件转发到 `/chat` 时也保留 bbox-only 授权框，避免 OCR/VLM 已有结果因事件名称
  或证据形态不同被静默丢弃；后端视觉置信度、授权时效、项目文档证据门和拒答边界保持不变。

证据见 `souldrive-ui/src/mediaCapture.ts`、`souldrive-ui/src/App.tsx`、
`souldrive-ui/scripts/media-capture.test.mjs` 和
`eval/results/multimodal_frontend_visual_evidence_gate_recheck_20260830.json`。前端媒体定向回归 `6 passed`，完整
`npm test` 和 `npm run build` 均通过；随后全后端回归为 `795 passed, 1 warning`。该增量只证明结构化视觉证据不再在前端被错误拦截，
不构成图标/图表准确率、完整回答 P95 或多模态相对 text-only 的提升结论。

仍未完成：真实 30 条 episode、跨应用/分辨率/说话人覆盖、VLM 慢路径完整链路、完整回答 P95 小于 5 秒、成对消融、
生产加密索引重建和 sealed-test。

## 29. 本轮继续执行记录（2026-08-30，比较题子查询覆盖与父文档排序）

本轮继续针对主盲测中反复出现的跨文档漏召回：比较题的两侧已经进入候选池，但同一来源的重复子块在父文档展开时占满 Top-3，导致第二来源无法进入证据。设计参考
[CRAG](https://arxiv.org/abs/2401.15884) 的检索质量评估与问题分解后纠错、
[RAGFlow](https://github.com/infiniflow/ragflow) 的可追溯分块，以及
[Dify 知识管线](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration) 的混合检索与父子块思路；没有复制外部代码、模型或依赖。

已落地但仅保留为显式实验开关，未作为生产默认：

- 比较题的每个子问题先按可解释的来源身份匹配保留来源；盲问不含完整标题时，退化到候选来源元数据、章节和有限正文的词法证据重合，不调用额外模型，也不把分数当概率。
- 父文档展开前保留不同子问题的来源槽位，避免相同父文档/来源的重复片段遮蔽另一侧；父文档槽位确定后，再按统一混合得分展示，保持最强证据优先。
- 新增比较题盲检索和父文档展开回归，覆盖来源覆盖、低分来源不置首、项目/文档证据边界不改变等行为。

为消除评测状态污染，新增显式的评测隔离：两轮均关闭文档使用账本加权和写回，并核对 `runtime/document_usage.json` 时间戳未变化。随后在同一 36 来源索引、同一数据集、同一 Top-3 配置下完成匹配配对：

- 稳定 baseline（关闭来源预留）：`retrieval_all_hit_rate@3` 为 96.3%（312/324），跨文档全命中率为 93.5%（101/108），首条命中率为 92.3%（299/324），MRR 为 0.949，平均来源覆盖率为 97.4%，平均检索耗时 3565 ms。
- 实验 candidate（开启来源预留与盲比较 margin）：全命中率为 94.4%（306/324），跨文档全命中率为 88.0%（95/108），首条命中率为 92.6%（300/324），MRR 为 0.951，平均来源覆盖率为 96.5%，平均检索耗时 3708 ms。
- 配对变化为改善 5、退化 11、不变 344，exact McNemar p=0.210114；candidate 相对 baseline 的全命中率下降 1.85 个百分点，跨文档全命中率下降 5.56 个百分点，因此未通过主指标门槛。

结论：来源预留和盲比较 margin 不进入生产默认；代码保留为 `--enable-comparison-source-reservation` 的可复现实验开关，便于后续有新数据时重新验证。公共多源去重路径作为当前默认。逐样本结果和比较证据见
`eval/results/retrieval_primary_isolated_baseline2_20260830.jsonl`、
`eval/results/retrieval_primary_isolated_candidate3_20260830.jsonl` 和
`eval/results/retrieval_primary_comparison_recheck_20260830.json`；漏召回分析见同一数据集上的
`scripts/analyze_rag_eval_misses.py` 输出。后端定向回归为 `80 passed`，之前完整后端回归为 `797 passed, 1 warning`，前端
`npm test`、`npm run build`、Python 编译和差异检查通过。

该结果不是统计显著性证明，也不等于答案准确率；它证明的是当前实验 candidate 应被否决，而不是证明整个 RAG 已达到竞赛或生产验收。未执行完整答案生成、事实级语义验证或生产加密索引重建，因此不勾选 S5 长文档/标准真实验收项，不把本轮结果写成多模态或竞赛成绩，也不改变 sealed-test 主评测口径。

## 30. 本轮继续执行记录（2026-08-30，真实多模态全链路接入与重排候选池消融）

本轮把阶段账本从“感知完成”推进到可选的真实 RAG 全链路测量，并针对当前全链路延迟最高的 cross-encoder 重排做受控消融。设计参考
[RAGFlow](https://github.com/infiniflow/ragflow) 的可追溯分块与分层检索、
[Dify 知识管线](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration) 的混合召回与父子块、
[CRAG](https://arxiv.org/abs/2401.15884) 的检索质量纠错，以及
[RAGChecker](https://arxiv.org/abs/2408.08067) 的检索/生成分层评估原则；没有复制外部代码、模型或引入新的运行时依赖。

已落地：

- `scripts/eval_real_mmi_pilot.py` 增加可选 RAG 全链路模式。运行时查询来自真实 ASR，不注入 gold 问题答案或期望来源；项目、选区和窗口信息只来自采集 episode 的当前上下文；澄清样本在检索前记为 `not_applicable`；引用检查只作用于回答正文，不把后置证据块误算成回答引用。
- 全链路阶段账本现在可以逐条记录 retrieval、first token、complete answer、evidence-backed 和 citation-present，并把预热成本与请求延迟分离。该评测使用独立 GraphRAG/审计输出目录，关闭使用账本写回，不修改冻结索引。
- `LocalKnowledgeBase` 增加可选 `rerank_candidate_limit`。它只在显式实验参数下，以已有混合信号预选 cross-encoder 候选，默认值 `None` 保持完整重排；`scripts/eval_rag.py` 同步提供该参数以支持可复现实验。

真实授权 pilot 的快路径全链路实跑结果：10 条 episode 中 7 条可进入 RAG，7/7 完成检索、首 token、完整回答和引用存在性检查；但这不是答案事实正确率，且本轮 VLM 慢路径仍跳过。完整重排运行的热延迟 P50/P95 为 `16124.5/27934.8 ms`，明显未通过官方完整回答小于 5 秒门槛。候选池和关闭重排的 pilot 仍为 7/7，但没有人工事实金标准，不能据此替换主检索结论。

在同一 `retrieval_quality_blind_v0.3`、同一 36 来源索引、同一 Top-3、同一评测隔离条件下，主集配对结果如下；baseline 为此前关闭使用账本和来源预留的稳定完整重排结果：

| 配置 | 全命中率@3 | 跨文档全命中率@3 | 首条命中率@1 | MRR | 平均检索耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 完整重排 baseline | 96.3%（312/324） | 93.5%（101/108） | 92.3% | 0.949 | 3565 ms |
| 关闭重排 | 91.7% | 83.3% | 93.5% | 0.950 | 2021 ms |
| 候选池 6 | 93.2% | 84.3% | 91.7% | 0.945 | 2240 ms |
| 候选池 12 | 93.2% | 84.3% | 90.7% | 0.940 | 2432 ms |
| 候选池 24 | 93.8% | 86.1% | 91.7% | 0.947 | 2886 ms |

配对 exact McNemar 检验相对 baseline 的 p 值分别为关闭重排 `0.001490`、候选池 6 `0.006348`、候选池 12 `0.012939`、候选池 24 `0.021484`；退化主要集中在跨文档来源覆盖。因此本轮明确否决“关闭重排”及候选池 6/12/24 作为默认优化，完整重排继续保留。候选池参数只作为后续新数据上的实验开关，不构成 RAG 提升结论。

主集逐样本结果见 `eval/results/retrieval_primary_no_reranker_20260830.jsonl`、
`eval/results/retrieval_primary_rerank6_20260830.jsonl`、
`eval/results/retrieval_primary_rerank12_20260830.jsonl` 和
`eval/results/retrieval_primary_rerank24_20260830.jsonl`；真实全链路账本见
`eval/results/real_mmi_full_chain_recheck_20260830/`、
`eval/results/real_mmi_full_chain_rerank6_recheck_20260830/`、
`eval/results/real_mmi_full_chain_rerank12_recheck_20260830/`、
`eval/results/real_mmi_full_chain_rerank24_recheck_20260830/` 和
`eval/results/real_mmi_full_chain_no_reranker_recheck_20260830/`。

项目隔离回归再次覆盖“同一全局文档可从两个项目访问、项目候选不遮蔽共享全局证据、无关项目不泄漏”四项，定向结果为 `4 passed`；完整后端回归为 `802 passed, 1 warning`，Python 编译和差异检查通过。冻结工作区的 `runtime/document_usage.json` 与关键词索引时间戳/长度未改变。

本轮仍未完成：至少 30 条独立真实 episode、跨应用/分辨率/说话人和困难样本覆盖、VLM 慢路径、完整回答 P95 小于 5 秒、答案级语义事实验证、完整模态相对 text-only 的冻结成对评测、生产加密索引重建和 sealed-test。因此不勾选 S1/S3/S6 的对应验收项，不把 `7/7` 全链路存在性通过或主集检索结果写成竞赛成绩。

## 31. 本轮继续执行记录（2026-08-30，风险感知重排路由的二次验收）

针对第 30 节中候选池在跨文档题上的退化，本轮依据主集逐类结果增加了风险感知保护：多来源比较、广泛综述、条款、表格和视觉资产问题不做候选截断，继续完整 cross-encoder 重排；只有普通查找问题才进入显式候选池实验。该路由使用已有 `QueryIntent`，不读取评测集标签，也不写入生产默认配置。

在同一 360 条冻结主集上，`adaptive_rerank6_v2` 相对稳定 baseline 的配对结果为：全命中率@3 `96.3% -> 96.6%`（提升 0.3 个百分点），任一命中率 `98.8%`、首条命中率 `92.6%`、MRR `0.952`、平均来源覆盖率 `97.7%`、跨文档全命中率 `93.5%`、图谱相关题命中率 `98.6%`；配对为改善 1、退化 0、不变 359，exact McNemar `p=1.000000`。逐样本唯一改善为 `SRQ3-074`，没有已知来源命中退化。

但是，在同一真实授权 10 条 pilot 上重新执行全链路时，7 条可检索样本仍为 `7/7` 完成检索、首 token、完整回答和引用存在性检查，VLM 仍未执行；热运行 P50/P95 为 `24713.1/31886.7 ms`，没有通过小于 5 秒门槛，也没有证明比完整重排稳定更快。主集平均延迟一次下降并不足以抵消真实端到端的高方差，因此该路由继续保留为显式 `rerank_candidate_limit` 实验参数，不切换生产默认，不能写成延迟或竞赛成绩提升。

本轮验证见 `eval/results/retrieval_primary_adaptive_rerank6_v2_20260830.jsonl` 和
`eval/results/real_mmi_full_chain_adaptive_rerank6_v2_recheck_20260830/`；风险感知保护回归覆盖项目/全局/比较路由，完整后端回归为 `803 passed, 1 warning`，Python 编译和差异检查通过。当前路线继续保留完整重排默认和证据门，后续优先级仍是：真实 30 条 episode、VLM 慢路径与完整回答延迟、答案级事实验证、生产加密索引重建及 sealed-test。

## 32. 本轮继续执行记录（2026-08-30，真实 pilot 答案级引用支持校验）

针对第 30、31 节真实全链路评测仍只检查“回答中存在引用编号”的缺口，本轮复用生产
`validate_answer_citations(semantic=true)`，将回答正文与 `souldrive-evidence` 块分离后，对每条
带引用 claim 检查引用 ID、正文支持和数字事实支持。该评测契约继续参考
[RAGChecker](https://arxiv.org/abs/2408.08067) 的检索/生成分层诊断和
[CRAG](https://arxiv.org/abs/2401.15884) 的不足证据后纠错原则；没有增加模型调用、改变检索权重
或写入冻结索引，也没有把确定性词法代理称为 NLI。

已落地：

- `scripts/eval_real_mmi_pilot.py` 的 RAG 全链路不再仅以 `evidence_count + citation` 判定
  `evidence_backed_response`，还要求证据块可解析且引用内容支持校验通过；逐条记录
  `evidence_block_valid`、`citation_support_valid`、校验原因和不支持 claim 数。
- `full_chain_coverage_detail` 增加引用支持适用数、通过数、失败数和不支持 claim 总数；
  无法解析证据块或正文 claim 不受支持的样本只能记为 partial，避免“引用存在”掩盖回答证据断裂。
- 新增回归覆盖“合法 `[E1]` 但正文与证据无关”必须不能通过；既有真实 RAG pilot 测试同步提供
  可解析证据块。

在同一 10 条用户授权 pilot、同一 `runtime/rag-eval-workspace-v4` 测试索引和同一默认完整重排配置
下重新运行：7 条可检索样本中引用支持适用数为 `7`、通过数为 `7`、失败数为 `0`、不支持 claim
数为 `0`，完整链路仍为 `7/7`。结果见
`eval/results/real_mmi_full_chain_claim_support_recheck_20260830/`。这只是当前保存回答通过
确定性词法支持门的证据，不是人工事实准确率、NLI/entailment、答案级召回率或竞赛成绩。

本轮仍未完成：至少 30 条独立真实 episode、跨应用/分辨率/说话人和困难样本覆盖、VLM 慢路径、
完整回答 P95 小于 5 秒、事实级语义/NLI 验证、完整模态相对 text-only 的冻结成对评测、生产加密
索引重建和 sealed-test；完整链路的 `7/7` 也不能替代这些门槛。

## 33. 本轮继续执行记录（2026-08-30，真实 VLM 慢路径冷/暖 smoke）

本轮把路线图要求的 VLM 慢路径从“代码可调用”推进到一条用户授权真实 episode 的冷/暖实测。设计仍遵循
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 的结构化视觉感知边界和
[LlamaIndex 多模态链路](https://docs.llamaindex.ai/en/v0.10.19/use_cases/multimodal.html) 的按需慢路径原则：
只处理 episode 已授权的图片区域，不扩大屏幕采集范围，不执行外部副作用；没有复制外部代码、模型或依赖。

已落地：

- `scripts/eval_real_mmi_vlm_retest.py` 增加显式 `--warmup-vlm`，把模型预热成本与实际请求延迟分开记录，
  并把 `vlm_warmup_pass` 纳入验收检查；冷/暖报告都保留阶段延迟、路由、目标绑定、项目上下文和无副作用结果。
- `scripts/eval_real_mmi_pilot.py` 增加显式 `--run-vlm`，可在同一真实 pilot 评测契约中选择 `visual_mode=slow`，
  不再把 VLM 是否执行隐藏在脚本默认行为中；对应转发行为有回归测试。

在 `REAL-PILOT-01`、同一用户授权语料和同一本地模型环境下：

- 冷路径：ASR/OCR/VLM/SoulAlign 均通过，慢路由与目标绑定均通过；总墙钟 `30521.705 ms`，其中 ASR
  `8105.558 ms`、OCR `2255.329 ms`、VLM `20150.340 ms`。
- 显式 VLM 预热：预热 `8248.520 ms`，随后请求总墙钟 `13175.698 ms`，ASR `5221.169 ms`、OCR
  `2452.509 ms`、VLM `5489.563 ms`；`vlm_warmup_pass`、慢路由、SoulAlign、目标绑定、项目上下文和无副作用
  检查全部通过。

证据见 `eval/results/real_mmi_vlm_slow_smoke_recheck_20260830/report.json` 和
`eval/results/real_mmi_vlm_slow_warmup_smoke_recheck_20260830/report.json`；定向评测回归为 `6 passed`，
两个报告的 acceptance checks 均为通过。预热确实降低了该 episode 的 VLM 请求成本，但暖请求仍高于完整回答
小于 5 秒门槛；单条 episode 也不能推出 VLM 图标/图表准确率、全链路 P50/P95 或竞赛成绩。

因此本轮只确认“真实授权慢路径可运行且证据边界有效”，不勾选 S3 的整体延迟验收项。仍未完成：至少 30 条独立
真实 episode、跨应用/分辨率/说话人和困难视觉样本、VLM 与完整 RAG 的组合实测、完整回答 P50/P95 小于 5 秒、
成对 text-only/full-multimodal 消融、答案级事实/NLI 验证、生产加密索引重建和 sealed-test。

## 34. 本轮继续执行记录（2026-08-30，模型驻留交接与视觉证据分层）

本轮针对真实 VLM→RAG 联合 smoke 中暴露的两个实际问题继续收敛：8 GiB 显存下 VLM 与回答模型同时驻留会导致
VLM 推理失败；VLM 摘要和整屏 OCR 混成一条证据会把菜单、工具栏和状态栏噪声带入回答。设计参考
[Transformers bitsandbytes 量化/显存说明](https://huggingface.co/docs/transformers/main/quantization/bitsandbytes)、
[Accelerate 大模型推理与 CPU/disk offload 说明](https://huggingface.co/docs/accelerate/package_reference/big_modeling)
对设备映射和显存驻留的边界，以及
[Microsoft OmniParser](https://github.com/microsoft/omniparser) 的结构化屏幕感知分层；没有复制外部代码、模型或依赖。

已落地：

- `RAGEngine` 的回答模型改为可释放、可按需重新加载；`Topic5Runtime` 增加 VLM 释放接口。MCP 聊天、VLM 预热、直接推理和真实评测在发生跨模型调用前后执行显式驻留交接，保留已生成的感知文本，不把模型权重同时压在 GPU 上。
- 直接视觉观察问题（例如“总结我当前选中的内容”）保留现有回答模型摘要能力，但创建 `visual_observation_only` 路由并跳过无关文档/GraphRAG 检索；“为什么项目下降”“比较实验”等项目事实问题仍必须经过文档证据门。
- 感知事件不再把 VLM 摘要和 OCR 原文拼成一条记录：VLM 与 OCR 分别进入 `V#` 证据，VLM 证据稳定优先，OCR 原始识别仍保留用于逐字/数字校验；已有 VLM 时，仅在回答上下文中按坐标去除屏幕边缘 chrome，诊断阶段仍保留原始 OCR。
- 生成结果在视觉观察路由中若以句中断裂结束，则使用完整 OCR 句子做确定性回退，并动态引用实际 `V#`，避免模型截断或引用编号错位。评测器把该路由的 retrieval 明确记录为 `not_applicable`，不把“未检索文档”错误算成引用失败，也不把它伪装成完整 RAG。

回归与真实证据：

- 视觉/评测定向回归为 `118 passed, 1 warning`；完整后端回归为 `815 passed, 1 warning`；Python `compileall` 通过，`git diff --check` 无实际空白错误（仅报告工作区既有 LF/CRLF 转换提示）。
- 在同一 `REAL-PILOT-01`、同一用户授权 WAV/截图和 `runtime/rag-eval-workspace-v4` 测试索引上，最终清理回退复核记录于 `eval/results/real_mmi_full_chain_visual_evidence_final_clean_complete_recheck_20260830/`：VLM、ASR、OCR、SoulAlign 均通过，`failure_count=0`；视觉引用支持为 `1/1`，retrieval 为 `not_applicable`，complete answer 为 `pass`，总墙钟为 `23289.212 ms`，VLM 阶段为 `12799.462 ms`，完整回答阶段为 `2975.715 ms`。这是一条被明确选取的真实 episode，不是 10 条 pilot 总体成绩，也不是答案事实准确率。
- 前置的真实 VLM→RAG 驻留交接复核记录于 `eval/results/real_mmi_full_chain_vlm_handoff_recheck_20260830/`，同一联合链路的模型驻留失败已消失，但完整联合请求仍远高于 5 秒目标；该结果只证明资源交接可行，不证明延迟达标。

本轮不勾选 S3/S6 的整体验收项。仍未完成：至少 30 条独立真实 episode、跨应用/分辨率/说话人/指代和困难视觉样本、图标目标准确率超过约 1.9% 基线、完整 VLM+RAG P50/P95 小于 5 秒、成对 text-only/full-multimodal 消融、答案级事实/NLI 验证、真实加密生产索引重建以及 sealed-test/官方隐藏集。当前视觉引用支持仍是确定性词法代理，不能替代人工事实判断或竞赛成绩。

## 35. 本轮继续执行记录（2026-08-30，真实 pilot 意图路由与视觉观察状态修正）

本轮对第 34 节之后的同一 10 条真实授权 pilot 做了回归。复盘发现，
`REAL-PILOT-06` 的语音“搜索项目里关于这个指标的实验记录”同时命中“指标”和“搜索”规则，
但泛化的事实问答规则先执行，造成 `private_qa` / `search_document` 意图错位；另外，视觉观察路由
已经正确跳过文档检索，却在聚合状态层因 `full_chain_complete=false` 被显示成 `partial`。

已落地：

- `core/soulalign.py` 增加有边界的显式文档检索动作判定：明确的“搜索/查找/检索/寻找/查出”与
  项目、文档、资料、记录、证据、来源或文件上下文组合时，优先进入 `search_document`；同时保留
  “检索文档后给出有依据的回答”“根据实验结果说明这个指标”等求解型问题的 `private_qa` 语义，
  不用单一关键词粗暴覆盖。
- `scripts/eval_real_mmi_pilot.py` 将纯视觉观察路由的总体状态定义为“该路由完成”时记为 `pass`；
  `retrieval=not_applicable` 和 `full_chain_complete=false` 仍保留，避免把视觉观察冒充完整文档 RAG。
- 新增规则冲突回归和视觉观察状态回归，确保搜索型、问答型和视觉直答型指标互不混淆。

在同一数据、同一 `runtime/rag-eval-workspace-v4`、同一 Top-6 和快路径配置下重新执行：

- 样本 `10`，`failure_count=0`；意图准确率、目标绑定准确率、动作准确率、澄清准确率均为 `1.0`。
- `REAL-PILOT-06` 已为 `search_document`，文档检索、首 token、完整回答和引用支持均通过；
  视觉观察样本的总体 RAG 路由状态为 `pass`，其文档 retrieval 仍为 `not_applicable`。
- 7 条可进入回答链的样本引用支持校验为 `7/7`，但仍是确定性词法支持代理，不是人工事实真值或 NLI。
- 快路径没有执行 VLM：VLM `skipped=10`；完整回答热运行 P50/P95/max 为
  `10458.444/35113.916/35113.916 ms`（聚合请求墙钟 P50/P95/max 为
  `5760.952/35236.854/35236.854 ms`），仍未达到完整回答小于 5 秒门槛。

逐样本与阶段账本见 `eval/results/real_mmi_full_chain_fast_intent_fix_20260830/`；规则与评测回归由
`tests/test_soulalign.py`、`tests/test_eval_real_mmi_pilot.py` 覆盖。此次结果说明一条明确的真实搜索任务
路由错误已修复，不能外推为 30 条真实集合、VLM 慢路径、跨应用或竞赛成绩通过。

仍未完成：至少 30 条独立真实 episode、跨应用/分辨率/说话人/指代和困难视觉样本、VLM 与完整 RAG
组合评测、完整 VLM+RAG P50/P95 小于 5 秒、成对 text-only/full-multimodal 消融、答案级事实/NLI
验证、真实加密生产索引重建以及 sealed-test/官方隐藏集。当前不勾选 S1/S3/S6 整体验收项。

## 36. 本轮继续执行记录（2026-08-30，VLM 慢路径与 RAG 全链路复核）

在第 35 节的意图路由修正后，继续使用同一批 10 条用户授权真实 pilot、同一
`runtime/rag-eval-workspace-v4`、同一 Top-6 和本地模型环境，显式执行 `--run-vlm --warmup-perception`。
该实跑只读取已有授权音频/截图和冻结测试索引，不扩大采集范围、不写回索引、不注入 gold 问题或答案。

已验证：

- ASR、OCR、VLM、SoulAlign 均 `10/10 pass`，运行时 `failure_count=0`；意图、目标绑定、动作和澄清
  准确率均为 `1.0`。`REAL-PILOT-06` 保持 `search_document`，没有被视觉慢路径回归覆盖。
- 3 条纯视觉观察路由均为总体 `pass`，文档检索为 `not_applicable`；4 条文档 RAG 样本的检索、首 token、
  完整回答和引用支持均通过；3 条需要澄清的样本在检索前保持 `not_applicable`。
- 7 条进入回答链的样本引用支持为 `7/7`，但仍是确定性词法支持代理，不是人工事实真值、NLI 或竞赛成绩。
- 模型驻留交接没有产生 OOM/运行时失败；冻结关键词索引 `keyword_index.sqlite` 与父文档库的大小/时间戳
  未被该评测写回。

性能仍未通过路线图门槛：VLM P50/P95 为 `8875.102/19571.868 ms`，完整回答 P50/P95/max 为
`11798.504/81353.250/81353.250 ms`，聚合请求墙钟 P50/P95/max 为
`16577.965/93224.693/93224.693 ms`。这轮明确证明的是慢路径可运行、资源交接和证据状态可审计，
不是完整 VLM+RAG 小于 5 秒，也不能外推到图标/图表准确率或 30 条真实集合。

逐样本预测、引用校验、阶段账本和预热记录见 `eval/results/real_mmi_full_chain_vlm_intent_fix_20260830/`；
第 35 节的快路径复跑仍保留在 `eval/results/real_mmi_full_chain_fast_intent_fix_20260830/`。
本轮不勾选 S3/S6 整体验收项。仍未完成：至少 30 条真实 episode、跨应用/分辨率/说话人/困难视觉样本、
成对 text-only/full-multimodal 消融、答案级事实/NLI 验证、完整 VLM+RAG 性能门、真实加密生产索引重建
以及 sealed-test/官方隐藏集。

## 37. 本轮继续执行记录（2026-08-30，文档级召回候选的冻结盲测否决）

针对第 32 节后仍存在的跨文档 Top-3 漏召回，本轮参考
[RAGFlow](https://github.com/infiniflow/ragflow) 的父子块/文档追踪设计和
[Dify 检索管线](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的全文、向量与层级召回思路，增加了一个显式实验开关 `enable_source_profile_recall`：从文档标题、编号、
章节路径和表头构造有界的 source profile，先做文档级语义候选，再回到现有子块、父文档、词法/向量混合
重排和证据选择。该候选不改变默认配置、不写入冻结索引，且保留逐阶段耗时和来源追踪。

实现与验证：

- `core/knowledge_base.py` 增加可缓存的文档 profile 候选和 `source_profile_ms` 可观测阶段；
  `core/retrieval.py` 仅在显式开启候选时读取 profile 分数；`scripts/eval_rag.py` 增加对应命令行开关。
- 新增 profile 候选的单元回归；定向知识库/检索回归 `59 passed`。默认实例仍为
  `source_profile_recall_enabled=false`。
- 在独立开发集 `eval/fixtures/rag_cn_raw_v0.1/dev.jsonl`、对应开发索引和 Top-6 条件下，
  profile 候选为 `16/16` 全命中、首条命中和来源覆盖均为 `100%`，平均检索 `2155 ms`；该结果
  仅说明它没有在这组小型开发样本上失效，不能抵消冻结盲测上的退化。
- 在同一 `eval/retrieval_quality_blind_v0.3.jsonl`、同一 `runtime/rag-eval-workspace-v4`、同一
  Top-3、同一模型和评测契约下完成 360 条冻结盲测。现有隔离 baseline 为：all-hit@3 `96.3%`、
  any-hit@3 `98.5%`、first-hit@1 `92.3%`、MRR `0.949`、平均来源覆盖 `97.4%`、跨文档 all-hit
  `93.5%`、平均检索 `3565 ms`；profile 候选为：all-hit@3 `94.4%`、any-hit@3 `97.5%`、
  first-hit@1 `92.3%`、MRR `0.944`、平均来源覆盖 `96.0%`、跨文档 all-hit `90.7%`、平均检索
  `4711 ms`。配对变化为改善 `2`、退化 `8`、不变 `350`，精确 McNemar `p=0.109375`。
- 结果文件为 `eval/results/retrieval_primary_source_profile_v1_20260830.jsonl`；评测器已对 ID、问题、
  来源和重复记录执行快速失败校验。

结论：文档级 profile 能提高部分文档的候选可见性，但当前代表子块选择、来源竞争和额外延迟抵消了
收益；该候选被冻结盲测否决，不得切换为生产默认，也不能把局部命中提升描述为整体 RAG 精度提升。
当前生产链路继续使用已验证的完整重排和项目优先/全局回退路径；profile 代码仅作为后续在独立开发集、
明确的来源路由置信门和答案级证据评测下继续验证的实验分支。

本轮仍未完成：至少 30 条独立真实 episode、跨应用/分辨率/说话人/指代和困难视觉样本、VLM 与完整 RAG
组合评测、完整 VLM+RAG P50/P95 小于 5 秒、成对 text-only/full-multimodal 消融、答案级事实/NLI
验证、真实加密生产索引重建以及 sealed-test/官方隐藏集。当前不勾选 S1/S3/S6 整体验收项。

## 38. 本轮继续执行记录（2026-08-30，跨项目全局文档回退一致性复核）

针对不同项目对同一份全局测试文档出现回答不一致的问题，本轮在不改变索引、不启用实验性 profile
召回的条件下，用同一冻结全局索引和同一个问题分别请求 `project-vision` 与 `project-battery`。
链路继续遵循 [Microsoft GraphRAG 查询范围分路](https://microsoft.github.io/graphrag/query/overview/)
和 [RAGFlow 原始证据回读](https://github.com/infiniflow/ragflow) 的适配边界：项目域和全局域分别
路由，回答仍只能使用可回读的原始证据。

实测结果：两个项目均走 `global_fallback`，各返回 3 条证据，来源名称和候选 ID 完全一致；
`source_profile_recall_enabled=false` 且 profile 阶段计数为 0。逐项结果保存于
`eval/results/rag_global_project_scope_recheck_20260830.json`。这确认了“没有项目专属资产时，全局
共享文档不会因当前项目切换而消失”的当前契约。

证据边界：本次两个项目 ID 在该冻结索引中均没有项目专属资产，因此验证的是全局回退一致性，不是
“项目图谱存在时的 project-first + global shared 联合检索”全覆盖，也不是答案事实准确率或真实业务
跨项目通过。仍需在正式加密工作区重建 GraphRAG 后，使用多项目真实问答和答案级引用语义评测复核；
S1/S5/S6 对应整体验收项保持未勾选。

## 39. 本轮收口验证（2026-08-30）

本轮代码变更完成收口回归：后端全量 `pytest -q` 为 `817 passed, 1 warning`，`core/`、`scripts/`、
`tests/` 的 Python `compileall` 通过，`git diff --check` 未发现实际空白错误。默认 RAG 运行时冒烟确认
`source_profile_recall_enabled=false`、profile 阶段计数为 `0`；同一全局索引下切换两个无项目专属资产的
项目 ID，均返回相同的 `global_fallback` 路由和相同引用，记录见
`eval/results/rag_global_project_scope_recheck_20260830.json`。

本轮完成的是可在当前工作区客观验证的增量和候选否决，不等于路线图完成。S1 的真实 30 episode、S3
完整 VLM+RAG P95 小于 5 秒及成对消融、S4 真实画像评测、S5 真实加密索引重建/答案级语义引用、S6
冻结提交包和官方/隐藏集仍需后续真实数据与人工审核，不能由本地回归替代。

## 40. 本轮继续执行记录（2026-08-30，加密工作区锁定态 fail-closed）

在只读检查当前 `runtime/workspace` 时发现其 `secure_vectors.sqlite` 有 `404` 个子块和 `341` 个父文档，
但本次独立 Python 进程没有持有解锁密钥。旧行为会因为 `LocalKnowledgeBase` 未看到内存密钥而打开一个
空的明文 Chroma 路径，最终表现为“全局文档没有证据”，容易被误判为 RAG 召回失败或项目隔离问题。

已落地：

- `core/knowledge_base.py` 在检测到加密向量索引存在但工作区密钥不可用时立即 fail-closed，返回明确的
  `workspace keys are not available for encrypted index`，不再静默降级到明文索引。
- 新增 `tests/test_knowledge_base_paths.py` 回归，覆盖已初始化加密工作区、存在加密向量文件但未解锁时
  必须阻断；普通未加密测试工作区仍保持原路径。
- 当前工作区实际锁定态冒烟通过；知识库路径、加密检索和安全接口定向回归为 `48 passed, 1 warning`。
  诊断与验证账本见 `eval/results/rag_locked_workspace_fail_closed_20260830.json`。

这项修复提升的是状态可解释性和证据可靠性，不是检索精度提升；只有通过正式 UI/sidecar 解锁后，才能
继续对真实加密索引做跨项目全局问答和 GraphRAG 重建验收。当前不把锁定态 0 条结果当作索引损坏，也不把
该 fail-closed 回归当作 S1/S5/S6 业务验收通过。

## 41. 本轮继续执行记录（2026-08-30，长文档持久化树祖先路由修复）

继续核验 S5“长论文和标准增加层级文档树路由”时发现，之前的 `section_tree_v1` 只在存在祖先自身记录时
才能构造节点；普通文档摄取通常只产生叶章节记录，查询祖先章节会退回旧 `section_path` 字符串匹配，
未真正使用持久化树。参考 [PageIndex](https://github.com/VectifyAI/PageIndex) 的节点/祖先导航和
[RAGFlow](https://github.com/infiniflow/ragflow) 的父子分块元数据，本轮进行了最小修复：

- `core/document_tree.py` 为每个父块保存 `section_tree_document_id`；
- `core/knowledge_base.py` 从完整章节路径重建缺失的祖先节点；旧索引没有该字段时从稳定的 `parent_id`
  兼容推导，仍保留旧 `section_path` 回退；
- 完整执行 `ingest_chunks -> search_with_evidence` 的测试文档：查询“检索”祖先章节实际返回
  `persisted_section_tree_v1 / descendant / 总览 > 检索`，并回读正确的后代证据；章节树、解析、普通/安全
  元数据和索引观测定向回归为 `53 passed`。证据见
  `eval/results/rag_section_tree_ancestor_route_recheck_20260830.json`。

这项修复证明了层级路由在完整摄取链路中生效，不能外推为长论文真实集合的召回率或答案准确率提升。
正式加密生产索引尚未在未取得口令的情况下重建，S5 长文档真实回答、多文档引用语义和 S6 整体验收仍保持
未完成。

## 42. 本轮继续执行记录（2026-08-30，加密工作区诊断状态与 GraphRAG UI 映射）

为避免“GraphRAG 未构建”和“工作区已锁定、派生索引暂时不可读”被混为一谈，本轮对当前
`runtime/workspace` 做了只读核验。评测检查器返回 `exit_code=1`，但这不是文档缺失或索引为空的充分证据：
36 份安全文档的元数据和安全向量索引均处于锁定不可读状态；底层安全向量库仍可确认存在 `404` 个子块、
`341` 个父文档和 `34467` 个关键词倒排项，安全 GraphRAG 存储文件也存在。未持有工作区密钥时，
文档数量和已索引数量必须视为未知，不能按 `0/36` 参与 RAG 指标或 GraphRAG 验收。

已落地：

- `souldrive-ui/src/App.tsx` 将 GraphRAG 诊断接口的 HTTP 423 明确映射为“工作区已锁定（解锁后查看）”；
  只有接口不可达才显示“诊断不可用”，不会把锁定态伪装成“未构建”。
- `eval/results/rag_workspace_readiness_recheck_20260830.json` 固化原始检查值、只读存储观测、锁定态解释和
  允许的下一步；没有读取、猜测或记录用户口令。

验证：前端生产构建通过；后端已有的 stale-unlocked 安全回归仍覆盖 `/security/status`、`/index/run` 在
内存密钥缺失时回落到 423 的行为。该变更提升的是状态可解释性和证据边界，不是 RAG 精度或 GraphRAG
业务效果提升。只有经用户授权解锁后，才能重新执行 36 份语料完整性、GraphRAG 构建和跨项目问答验收。
新增的 GraphRAG 诊断锁定态回归加入完整后端套件后，全量结果为 `820 passed, 1 warning`；前端现有测试
和生产构建同样通过。

本轮仍不勾选 S1/S3/S5/S6 整体验收项；真实 30 episode、完整 VLM+RAG 小于 5 秒、答案级语义引用、
正式加密索引重建、sealed-test 和官方隐藏集仍未完成。

## 43. 本轮继续执行记录（2026-08-30，默认 RAG 冻结盲测回归）

为验证最近的长文档持久化树修复、查询变体接入和锁定态 fail-closed 改动没有改变默认检索结果，
在独立冻结工作区 `runtime/rag-eval-workspace-v4` 上按原条件重跑
`eval/retrieval_quality_blind_v0.3.jsonl`：360 条样本、Top-3、关闭使用账本、关闭比较来源预留、
关闭 source profile、只跑检索不生成答案，过程中不写索引。

结果：324 条有期望来源的样本中，all-hit@3 `96.3%`、any-hit@3 `98.5%`、first-hit@1 `92.3%`、
MRR `0.949`、平均来源覆盖率 `97.4%`、跨文档 all-hit@3 `93.5%`、图谱相关题 any-hit@3 `98.2%`，
平均检索 `3720 ms`。与既有隔离 baseline 逐 ID 对照为改善 `0`、退化 `0`、不变 `360`，精确
McNemar `p=1.0`；因此近期修复没有造成默认 RAG 召回回归，但也没有形成精度提升。

逐样本结果见 `eval/results/retrieval_primary_isolated_recheck_20260830.jsonl`，聚合和条件见
`eval/results/retrieval_primary_isolated_recheck_20260830.json`。该结果只更新可复现的检索现状，
不能替代答案级事实/NLI、真实业务 episode、正式加密工作区或官方隐藏集验收。候选 source profile 和
比较来源预留仍维持实验/否决状态，不切换生产默认。

## 44. 本轮继续执行记录（2026-08-30，UI Automation 系统可读选区快路径）

路线图的 S3 明确要求“系统可读文本优先于 OCR”，并要求多模态事件带时间、空间、来源和置信度；
本轮针对真实 pilot 暴露的业务缺口——UIA 只有“结果与结论段落”这类控件标签，没有可改写正文——
参考 [OmniParser](https://github.com/microsoft/omniparser) 的结构化目标优先思想、
[Microsoft GraphRAG 查询分路](https://microsoft.github.io/graphrag/query/overview/) 的局部/全局边界和
[RAGFlow](https://github.com/infiniflow/ragflow) 的来源可追溯原则，补上最小的系统可读选区文本闭环。
没有复制外部代码、模型或依赖，也没有扩大屏幕采集范围。

已落地：

- Tauri Windows UI Automation 在用户显式触发“读取当前焦点控件”时调用 `TextPattern.GetSelection`，
  最多读取 4 个选区范围、4000 字符；密码控件和密码状态未知时不返回文本。控件名仍作为目标标签，
  `selected_text` 单独保存，避免把标签误当正文。
- `selected_text` 已贯通 Tauri snapshot、前端感知事件、`/chat` 的 `PerceptionEvent`、运行时事件公共化、
  工作流规划、Python 交互上下文和检索查询提示。系统可读选区在 `V#` 命名空间中生成 `screen_text_selection` 证据，
  `source_modality=ui_automation`，不与私有文献 `E#` 混用；事实型问题仍把它作为检索提示并保留文档证据门。
- 只有“当前选区改写/润色”且不包含原因、比较、数值、版本、数据集等事实标记时，才进入视觉直答并跳过
  文档检索；“根据当前选区为什么变化”“比较选区中的本月和上月”等问题继续进入文档/图谱证据链。
  该路由仍是无副作用的文本生成，不会自动执行粘贴、点击或发送。

验证：新增系统可读选区的 Python 契约、RAG 路由、运行时传播和 `/chat` schema 回归后，定向测试为
`134 passed, 1 warning`；全量后端测试为 `828 passed, 1 warning`；`compileall`、前端测试和生产构建、
Tauri `cargo fmt -- --check` 与 `cargo check` 均通过。可复现记录见
`eval/results/multimodal_uia_selected_text_contract_recheck_20260830.json`。

证据边界：当前真实 pilot 中实际 UIA 选中文字 episode 数为 `0`；已有 pilot 的 `ui_selection` 文本可能只是
区域标签。因此本轮只确认数据契约、隐私边界、证据命名空间和路由分流成立，不宣称改写准确率、目标绑定率、
答案事实/NLI、跨应用泛化或 P95 提升，也不勾选 S3/S6 整体验收项。下一道门是补采至少覆盖浏览器、Office/PDF
阅读器和桌面应用的真实选中文字 episode，保留人工确认的原文与改写真值，再进行 text-only/full-multimodal
成对消融和端到端延迟评测；在此之前生产默认的 VLM 图像预算仍保持 256/768，之前的 128/384 候选继续否决。

## 45. 本轮继续执行记录（2026-08-30，真实 pilot 选区标签污染审计）

在 UIA 选区快路径接入后，继续审计现有真实集合，发现评测器原先会把 `ui_selection.payload.text` 当作
`active_context.selected_text`。这会把“结果与结论段落”一类目标标签误当成正文，导致改写和检索出现虚假的多模态输入。
该行为与路线图的“原始模态、来源和置信度可追溯”以及 EvidenceGraph 的 `V#`/`E#` 边界不一致。

已落地：

- `scripts/eval_real_mmi_pilot.py` 现在只接受显式的 `selected_text` 字段，不再从 `text` 标签推导正文；
  同时兼容 `ui_selection` 和 `ui_automation` 事件。
- `scripts/ingest_real_mmi_episode.py` 保留旧 `--selection-text` 作为标签参数，新增 `--selected-text` 和
  `--selection-source`；只有用户明确确认的正文才能进入改写/RAG 上下文，来源可记录为人工、Windows UIA
  或 OCR。旧集合不被自动改写，也不把 OCR 结果回填成 gold。
- 集合审计增加 `selection_text_coverage`，区分“有真实选中文字”和“只有目标标签”，便于在补采 30 条真实
  episode 时把正文可用性作为独立数据质量指标。

对当前 `data/local/souldrive_pc_mmi_real_v0.1` 做只读审计：10 条 episode 中有 5 个选区事件，
`with_selected_text=0`、`label_only=5`；schema、ID、媒体引用在不要求真实多样性时仍通过。记录见
`eval/results/real_mmi_selection_text_audit_20260830.json`。这证明旧 pilot 不具备 UIA 真实选中文字数据，
也证明评测器已不再凭标签制造正文证据；它不是准确率下降或提升的模型结论。

验证回归：选区上下文隔离、采集脚本新参数、集合审计、RAG 视觉路由和全量后端套件均通过；未修改现有
真实媒体和冻结评测数据。随后对现有集合的 `REAL-PILOT-02` 做了单样本只读回归：意图、目标、动作和澄清判定均通过，
且评测上下文未出现由 `ui_selection.payload.text` 推导的正文。该结果只证明防污染契约在一条旧样本上生效，
不增加真实选中文字覆盖率，也不构成准确率或跨应用泛化证据。下一道真实数据门仍是用户授权采集带人工确认原文的跨应用 episode；
在此之前，任何选区改写结果都必须以实际 `selected_text` 或可靠视觉证据为依据，不能用区域标签补齐。

## 46. 本轮继续执行记录（2026-08-30，采集触发的 ASR/OCR 后台预热）

路线图 S3 要求优化模型预热、缓存和路由，但不能把“预热后一次请求变快”写成完整多模态
P95 达标。本轮根据前一轮 10 条 pilot 的阶段账本，将已有的本地感知预热从单独手动按钮扩展为
用户明确授权录音或屏幕采集成功后的后台任务；设计仍遵循成熟 RAG/Agent 系统将资源准备、证据
链路和副作用边界分离的原则，没有复制外部项目代码，也没有扩大屏幕采集范围。

已落地：

- 前端用单个 in-flight Promise 合并重复的 `warmup-perception` 请求；麦克风授权并正式开始录音后
  启动一次预热，用户立即发送时等待该任务完成，失败则回到原有运行时按需加载，不阻断普通文字问答。
  纯屏幕采集不强制加载 ASR，避免与图片/VLM 路径争用无关模型资源。
- 手动“预热语音与文字感知”入口复用同一请求，仍显示服务端返回的阶段状态；预热接口只构造
  本地 ASR/OCR，不录音、不读取屏幕、不执行推理。
- 新增前端 wiring 回归和记录文件
  `eval/results/multimodal_perception_capture_warmup_recheck_20260830.json`。

在同一现有 10 条真实 pilot、同一本地环境下，显式预热耗时 `9665.257 ms`；预热后的首次测量
请求为 `2020.935 ms`，稳态感知 P50/P95 为 `84.691/1540.954 ms`，失败数为 `0`。这组数据的
测量范围是 `perception_and_alignment_only`，未接入 RAG、首 token 或完整回答；它说明准备成本
可以前移并被用户授权采集阶段吸收一部分，不说明完整业务链路已经小于 5 秒。

验证：前端测试、生产构建、后端全量 `828 passed, 1 warning`、Python 编译、Tauri 格式检查和
编译均通过。当前仍不勾选 S3 的整体预热/延迟验收项；下一道门仍是至少 30 条跨浏览器、Office/
PDF 阅读器和桌面应用的真实 episode，并在完整 RAG/首 token/回答链路上完成冷暖、text-only/完整
模态和失败降级的成对评测。详细限制见上述 JSON 记录。

## 47. 本轮继续执行记录（2026-08-30，空召回的有界证据纠错）

继续核查 S5 的 CRAG 风格“先评估检索质量、再纠错重检索”链路时发现，原实现只对
`retry` 状态触发第二次检索；首轮完全没有可采纳证据时，`evidence_gate` 为 `reject`，会直接拒答，
因此最差的漏召回情形没有获得一次受控的查询改写机会。该缺口不改变证据门，也不允许模型在没有证据时
生成回答。

已落地：

- `core/rag_engine.py` 在普通文档问题的首轮证据为空且拒绝原因可由检索纠正时，触发一次确定性纠错检索；
  普通问题沿用查询变体，比较问题沿用已存在的子查询分解，并继续保持当前项目/全局过滤。
- 空内容、过短片段和低置信 OCR 明确不进入无效重试；二次检索仍不能通过证据门时继续拒答。
- 新增 `tests/test_rag_prompting.py` 契约覆盖“空召回后找到更强证据”和“低置信 OCR 不重复检索”；
  逐项结果见 `eval/results/rag_empty_recall_correction_recheck_20260830.json`。

验证：RAG 定向回归 `104 passed`，后端全量 `830 passed, 1 warning`，Python 编译和差异检查通过。
同一 `eval/retrieval_quality_blind_v0.3.jsonl`、同一 `runtime/rag-eval-workspace-v4`、同一 Top-3
条件的冻结检索复核逐条 `360` 条不变：all-hit@3 `96.3%`、any-hit@3 `98.5%`、MRR `0.949`、
跨文档 all-hit@3 `93.5%`，paired 改善 `0`、退化 `0`、不变 `360`。这符合该改动只作用于
`RAGEngine` 生成前分支、而检索评测不执行生成链的事实；因此本轮保留的是漏召回时的可靠性契约，
不是检索精度提升结论。

当前仍不勾选 S1/S3/S5/S6 整体验收项：真实 30 episode、答案级事实/NLI、完整 VLM+RAG P95 小于
5 秒、正式加密工作区重建及 GraphRAG 业务验收仍未完成。正式工作区锁定状态也不能被本轮测试结果
解释为索引损坏或生产问答已达标。

## 48. 本轮继续执行记录（2026-08-30，章节原文定位的答案级摘录保护）

继续复核 S5 的答案级证据链时，发现一个已由保存的 dev 结果复现的错误：问题
“请定位资料中关于‘3. graphrag的源码剖析’的关键原文证据”已经召回正确文档，但因为查询意图没有识别出
位于引号内部的编号章节，仍进入生成模型；旧答案将无关的 `global查询` 证据拼入回答。该问题不是简单的
检索命中问题，而是“章节定位任务”和“开放式生成任务”路由边界错误。

本轮参考 [PageIndex](https://github.com/VectifyAI/PageIndex) 的层级章节导航和
[RAGFlow](https://github.com/infiniflow/ragflow) 的证据可追溯分层原则，完成最小修复：

- `core/retrieval.py` 的章节意图识别不再只检查查询开头，新增对 `2.3`、`3.`、`3、` 等编号章节的识别；
  `clause_references` 继续经过现有规范化逻辑，不改变默认检索分数。
- `core/rag_engine.py` 将“定位/查找 + 章节引用”纳入直接文档定位路由。证据门通过后，
  从首个证据中只摘录请求章节标题到下一章节边界的局部正文，并继续使用 `[E#]` 引用；不再把 Top-K 中的
  无关证据交给生成模型自由拼接。
- 若证据只有章节标题、没有足够正文，摘录函数不补写结论；原有证据门和拒答策略保持不变。

验证结果：章节/RAG 定向回归 `97 passed`；后端全量回归 `831 passed, 1 warning`。随后在同一冻结主集
上按原条件重跑 360 条检索，并以 baseline2 做逐 ID 配对：all-hit@3 `96.3%`、any-hit@3 `98.5%`、
首条命中 `92.3%`、MRR `0.949`、跨文档 all-hit `93.5%`，改善 `0`、退化 `0`、不变 `360`，
exact McNemar `p=1.0`；平均检索 `3820 ms`，只作为回归记录，不构成性能提升。随后在现有 dev 集 4 条
`evidence_lookup` 样本上对已保存证据做确定性只读重放，4/4 进入直接定位路由、4/4 保留期望关键词；其中
`RAGCN-7da648f9c564-03` 现在只返回 `3. graphrag 的源码剖析 - 代码的流程解读，prompt 的设计逻辑解读`
并引用 `[E1]`，不再出现 `global查询`。详细结果见
`eval/results/retrieval_primary_section_route_candidate_20260830.jsonl` 和
`eval/results/rag_section_lookup_answer_recheck_20260830.json`。这些数字是检索回归与路由/摘录契约代理，不是人工答案
准确率、事实级 NLI 或竞赛成绩。

本轮仍不勾选 S1/S3/S5/S6 整体验收项：至少 30 条独立真实 episode、真实多模态跨应用数据、完整 VLM+RAG
回答 P95 小于 5 秒、答案级人工/NLI 语义验证、正式加密索引重建、GraphRAG 业务验收和 sealed-test 仍未完成。

## 49. 本轮继续执行记录（2026-08-30，引用主题与证据绑定保护）

对现有答案级结果继续做逐条审计时，发现仅用通用词重叠的引用支持代理仍可能放过错误归因：旧答案虽然把
“图中的概念在构建索引中都已创建”原文放在 `[E2]` 下，却声称它支持“3. graphrag的源码剖析”；`E2` 的
章节是 `global查询`，并不包含被回答的章节主题。该结果此前在词法支持门中误通过，说明引用编号存在和局部
词重叠都不足以保证证据绑定。

本轮增加一条仍可解释、但不冒充 NLI 的保护：`core/answer_quality.py` 对带有“关于/围绕/针对/有关
‘主题’”结构的事实 claim，要求每个被引用证据的章节、路径、摘要或正文包含该主题（忽略空白差异）；缺失时
答案校验失败，进入既有一次重试和确定性证据回退，不放宽数字事实校验和证据门。

验证结果：答案质量、RAG 和检索定向回归 `135 passed`，后端全量回归 `833 passed, 1 warning`。对旧的 `RAGCN-7da648f9c564-03` 保存答案做只读
重放，校验由误通过变为拒绝，明确记录缺失主题 `3. graphrag的源码剖析`；当前章节摘录回退答案校验通过，
仅引用 `[E1]`。现有 dev 16 条结果的词法代理汇总因此从引用内容支持 `100.0%` 调整为 `93.8%`，该下降
不是模型变差，而是把此前被保护门漏掉的 1 条错误归因显性化。详细记录见
`eval/results/rag_section_lookup_answer_recheck_20260830.json`。

这仍然不是人工事实准确率、答案级召回率或 NLI 结果；主题字符串存在也不能证明整句语义蕴含。路线图下一道
答案级门仍需冻结的人工/NLI 语义评测，并在生产加密工作区解锁后重跑真实生成链路。本轮继续不勾选 S1/S3/S5/S6
整体验收项。

## 50. 本轮继续执行记录（2026-08-30，检索证据与上下文正文错位保护）

继续审计 GraphRAG 与普通检索合并后的证据准入时，发现 `documents`、`metadatas`、`evidence` 三列如果由
适配器返回不同长度，当前索引关联可能把“证据项已有正文、documents 槽为空”的可用证据误判为
`empty_context`。这会造成检索已经命中但上下文门误拒，尤其影响图谱回读或降级适配器路径。

本轮在 `core/context_gate.py` 增加最小恢复规则：当前正文槽为空时，只从同一证据项的 `context`，再从同一证据项
的 `snippet` 恢复正文，并写入 `context_recovered_from_evidence=true` 审计标记；没有同一证据正文仍然拒绝，
绝不跨证据借用内容。该修复保持项目/全局权限过滤、低置信 OCR 门、重复证据门、Top-K 和检索权重不变。

验证结果：上下文、GraphRAG、RAG 答案和引用定向回归 `127 passed`，后端全量回归 `834 passed, 1 warning`，
编译通过。新增契约覆盖“documents 为空但同一 evidence 有正文”时恢复且带审计标记。结果见
`eval/results/rag_context_evidence_alignment_recheck_20260830.json`。该结果是上下文准入可靠性证据，未重新计算
冻结检索指标，不宣称召回率、答案准确率或 GraphRAG 业务效果提升。

本轮继续不勾选 S1/S3/S5/S6 整体验收项：真实多模态 30 episode、完整 VLM+RAG P95 小于 5 秒、答案级人工/NLI、
正式加密索引重建、GraphRAG 生产业务验收和 sealed-test 仍未完成。

## 51. 本轮继续执行记录（2026-08-30，视觉引用命名空间与模态一致性门禁）

继续审计多模态答案链时，发现历史 dev 答案 `RAGCN-8f9f13630d1f-01` 在只有 `[E1]` 文档证据的情况下，
生成了“根据本轮截图/视觉识别，证据位于……”的视觉表述。该答案的引用编号存在，但来源模态与声明不一致；
如果只检查引用编号和词法重叠，会把文档定位误认为截图观察，违背 EvidenceGraph 的来源边界。

本轮参考 [RAGFlow](https://github.com/infiniflow/ragflow) 的可追溯引用分层和
[OmniParser](https://github.com/microsoft/omniparser) 的屏幕/UI 感知边界，在 `core/answer_quality.py` 增加最小门禁：

- `[V#]` 必须对应带有视觉模态元数据的证据项；缺失或被错误标成文档模态时拒绝。
- 答案出现“截图、视觉识别、当前画面/屏幕、图表/图片/图像”等明确视觉声明时，必须同时引用合法 `[V#]`；
  `[E#]` 不能冒充当前屏幕证据。
- 失败沿用既有一次重试和证据回退路径；没有修改检索排序、Top-K、项目权限或索引结构。并新增 RAG 生产链集成回归，
  确认模态错配会触发重试而不是直接向用户输出。

验证结果：答案质量、RAG 和真实多模态评测定向回归 `112 passed`；后端全量回归 `837 passed, 1 warning`，
编译和差异检查通过。对同一 `eval/fixtures/rag_cn_raw_v0.1/dev.jsonl` 的 16 条历史答案做只读重放，
新门禁明确拦截 `RAGCN-8f9f13630d1f-01` 的 `missing_visual_evidence`；当前所有答案质量门合计有效 `14/16`，
另 1 条仍是第 49 节已发现的引用主题/内容支持问题，不归因于本轮视觉门禁。结果见
`eval/results/rag_visual_citation_namespace_recheck_20260830.json`。该回放用于验证错误拦截与审计，不是新的
真实多模态准确率、人工/NLI 语义准确率或竞赛成绩。

本轮仍不勾选 S1/S3/S5/S6 整体验收项：至少 30 条独立真实多模态 episode、跨应用/跨说话人数据、完整 VLM+RAG
P95 小于 5 秒、答案级人工/NLI、正式加密索引重建、GraphRAG 生产业务验收和 sealed-test 仍未完成。

## 52. 本轮继续执行记录（2026-08-30，结构化文件来源定位与答案引用追溯）

继续按 S2/S5 审计产品语料时，发现 CSV、XLSX、配置和日志虽然能够被摄取和召回，但普通 RAG 证据仍统一
显示为“第 1 页/文本段落”。这会让实验指标、配置参数和日志结论无法在答案中快速回到原始行或 sheet，削弱
“和上个月实验对比”以及多模态指标问答的可复核性。该缺口与 [RAGFlow](https://github.com/infiniflow/ragflow)
的可追溯解析原则、[Dify Knowledge Pipeline](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的摄取/检索/上下文分层原则不一致。

已落地：

- `core/plain_document_parser.py` 在不改变正文和检索排序的前提下，为 CSV 增加 `rows:2-N` 行范围，为
  XLSX 增加 `sheet:name/rows:2-N`；对缺少 `workbook.xml` 的轻量 XLSX 按 `sheetN` 回退，并兼容标准与无命名空间
  worksheet XML。JSON/YAML/YML/TXT/MD/LOG 增加 `lines:1-N`，DOCX/PPTX 保留可追溯 Office XML part。
- `core/retrieval.py` 将 `source_locator` 与 `source_locator_kind` 写入最终引用；`core/rag_engine.py` 将相同定位
  传入生成上下文。定位字段有长度上限，不能替代原始资产和项目目录。
- 新增解析、引用和生成上下文回归，覆盖标准 XLSX、轻量无 workbook XLSX、CSV 行范围和结构化定位展示。

验证结果：相关定向回归 `115 passed`；后端全量回归 `840 passed, 1 warning`；`compileall` 通过；差异检查除 Windows
LF/CRLF 规范化提示外通过。对当前产品夹具的只读核验得到：`vision_july_metrics.csv -> rows:2-3`，
`vision_august_metrics.xlsx -> sheet:sheet1/header:row:1`，`vision_july_config.yaml -> lines:1-3`，
`08_ui_context_config.json -> lines:1-1`，`08_tasks.md -> lines:1-3`。完整记录见
`eval/results/rag_structured_source_locator_recheck_20260830.json`。

证据边界：本轮只改变来源元数据传递和引用展示，没有改变检索正文、Top-K、排序权重或权限过滤，因此不重新计算、
也不宣称冻结检索指标、答案准确率或多模态业务效果提升。当前定位仍是 chunk 级范围，不是单元格级地址；正式
加密工作区仍锁定，GraphRAG 生产业务验收、真实多模态 30 episode、完整 VLM+RAG P95、答案级人工/NLI 和 sealed-test
仍未完成。本轮继续不勾选 S1/S3/S5/S6 整体验收项。

## 53. 本轮继续执行记录（2026-08-30，CSV 指标表的行保持切片）

在完成来源定位后继续审计 S2/S5 的结构化检索链，发现 CSV 虽被格式化成带 `|` 的文本表格，但缺少 Markdown
表格分隔线。现有父子切片器因此可能把长指标表当普通长文本处理，切片边界会横跨字段行，降低诸如
`target_binding_accuracy`、`p95_latency_ms` 等指标问答的字段关联可靠性。这与 [RAGFlow](https://github.com/infiniflow/ragflow)
的结构化表格处理和 [Dify Knowledge Pipeline](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的分层摄取原则不一致。

已落地：

- `core/plain_document_parser.py` 将有数据行的 CSV 输出为标准 Markdown 表格：首行表头、分隔线、按原顺序排列的数据行；
  只有表头的文件不伪造数据。
- 复用现有 `core/parent_child_index.py` 的 table-aware splitter，让长 CSV 按表格行分块并在每个 child 重复表头，避免普通
  文本 overlap 把相邻指标语义混在一起；原有 `rows:2-N` 来源定位继续保留。
- 新增长指标表回归，验证 29 行数据被切成 3 个 child，所有 child 都保留表头且末行不丢失；当前产品
  `08_alignment_metrics.csv` 也得到 3 个带表头 child。

验证结果：解析/父子切片定向回归 `20 passed`；后端全量回归 `841 passed, 1 warning`；`compileall` 通过；差异检查除
Windows LF/CRLF 规范化提示外通过。详细记录见 `eval/results/rag_csv_row_chunking_recheck_20260830.json`。

证据边界：本轮只改变 CSV 的索引前格式，冻结主集没有 CSV，且尚未在新索引上重跑产品答案生成，因此不宣称检索召回率、
答案准确率或多模态业务效果提升。XLSX 完整单元格类型/公式/共享字符串、精确单元格地址、答案级人工/NLI、正式加密索引重建、
GraphRAG 生产验收、真实多模态 30 episode、完整链路 P95 和 sealed-test 仍未完成；本轮继续不勾选 S1/S3/S5/S6 整体验收项。

## 54. 本轮继续执行记录（2026-08-30，双项目全局文档可见性回放）

针对用户反馈“同一全局测试文档在不同项目中有时能回答、有时证据不足”，本轮在临时隔离工作区做了双项目回放，
不使用正式加密工作区，也不把项目真实数据注入查询。测试同时覆盖：项目 A 有本地相关文档、项目 B 只有无关私有文档，
两者都应能看到未绑定的共享 RAG 原理文档；项目 A 还必须保持“当前项目优先 + 全局补充”，不能被共享文档或其他项目私有文档遮蔽。

回放结果：

- `p1` 路由为 `project_first_with_global`，来源为 `p1-rag.pdf` 与 `global-rag.pdf`，项目 ID 为 `p1/null`；全局来源存在，
  未带入 `p2-noise.pdf`，引用编号为 `[E1]/[E2]`。
- `p2` 路由为 `global_fallback`，来源为 `global-rag.pdf`，项目 ID 为 `null`；全局来源存在，未带入 `p1` 或 `p2` 的私有
  噪声文档，引用编号为 `[E1]`。
- 双项目均能看到同一全局来源，跨项目私有来源泄露为 `false`。详细记录见
  `eval/results/rag_global_scope_two_project_recheck_20260830.json`；既有安全检索契约也在全量回归中通过。

证据边界：这次回放确认的是检索路由、共享文档可见性和项目隔离，不是本地模型生成答案的事实/NLI 准确率，也不是正式
生产索引验收。正式加密工作区仍锁定，真实项目绑定/权限配置、跨项目生成回答、GraphRAG 生产业务验收、答案级人工/NLI、
真实多模态 30 episode、完整 VLM+RAG P95 和 sealed-test 仍未完成；本轮继续不勾选 S1/S3/S5/S6 整体验收项。

## 55. 本轮继续执行记录（2026-08-30，实验指标 XLSX 轻量导出回退与行级定位）

继续落实 S2 的“CSV/XLSX 指标摄取”和“每个数值可定位”要求时，发现当前产品夹具中的
`vision_august_metrics.xlsx` 不是完整 Excel 包，只有 worksheet XML，标准 `openpyxl` 解析会失败。
如果只保留标准解析，产品指标无法进入实验账本；如果把任意 XML 当成完整 Excel 解释，又会静默制造不可靠字段。

本轮参考 [RAGFlow](https://github.com/infiniflow/ragflow) 的结构化来源追溯、
[Dify Knowledge Pipeline](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的分层摄取，以及 [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/) 的本地 run/metric 元数据边界，完成最小增量：

- `core/experiment_ledger.py` 保持完整 XLSX 优先走 `openpyxl`；仅在该路径失败时，对有明确 worksheet XML 的轻量 ZIP 导出启用受限回退。
- 回退兼容共享字符串、inline string、标准/无命名空间 XML 和缺少 `workbook.xml` 的 `sheetN`；无法确认结构时仍 fail-closed。
- 当前产品夹具已经解析出 `accuracy=0.921`、`p95_latency_ms=960`，分别定位到
  `sheet:sheet1/embedded_row:2` 和 `sheet:sheet1/embedded_row:3`；标准 Excel 回归定位为
  `sheet:Metrics/row:2`。
- 新增标准包与轻量导出的回归，避免回退路径遮蔽正常 Excel 解析。

验证结果：结构化摄取、解析、父子切片、检索、引用和 RAG 提示词定向回归 `129 passed`；后端全量回归
`843 passed, 1 warning`。详细记录见
`eval/results/rag_experiment_xlsx_fallback_recheck_20260830.json`。

证据边界：这次修复证明的是指标文件摄取和来源定位契约，不是检索召回率、答案准确率、实验因果结论或多模态业务效果提升。
轻量回退不负责公式计算、样式和复杂 Excel 语义；日期、单位、数据集版本和 split 仍必须确认后写入账本。正式加密工作区仍锁定，
GraphRAG 生产业务验收、真实多模态 30 episode、完整 VLM+RAG P95、答案级人工/NLI 和 sealed-test 仍未完成；本轮继续不勾选
S1/S3/S5/S6 整体验收项。

## 56. 本轮继续执行记录（2026-08-30，全局文档从检索到最终回答的双项目回放）

针对“同一全局测试文档在不同项目里回答结果不一致”的业务反馈，前一轮已经证明了检索层的共享来源可见性，
但还不能证明来源真正进入最终问答。本轮继续参考 [Microsoft GraphRAG](https://microsoft.github.io/graphrag/query/overview/)
的局部/全局查询分路和 [RAGFlow](https://github.com/infiniflow/ragflow) 的可追溯引用原则，补上
`LocalKnowledgeBase -> RAGEngine -> 引用输出` 的双项目回归：

- `p1`、`p2` 都只有无关私有噪声文档，未绑定的 `global.pdf` 是唯一相关来源，因此两者都走 `global_fallback`。
- 两个项目的最终输出都包含 `global.pdf` 和合法 `[E1]`，且均未出现另一个项目的私有噪声来源。
- 第一次测试故意使用与证据不一致的中文改写时，答案质量门拒绝生成并回退到证据摘录；改用证据原文后才通过，
  证明本回归同时覆盖了“全局来源传递”和“引用支持门”，没有用固定答案绕过证据校验。

验证结果：`tests/test_secure_retrieval_flow.py` 共 `15 passed`；详细双项目结果见
`eval/results/rag_global_scope_answer_recheck_20260830.json`。该结果验证的是项目隔离、共享来源进入最终上下文和引用存在性，
不等于本地模型事实/NLI 准确率。

证据边界：本回放使用临时工作区和固定生成器，正式加密工作区仍锁定，真实项目绑定/生产索引、GraphRAG 业务验收、答案级人工/NLI、
真实多模态 30 episode、完整 VLM+RAG P95 和 sealed-test 仍未完成；本轮继续不勾选 S1/S3/S5/S6 整体验收项。

## 57. 本轮继续执行记录（2026-08-30，XLSX 工作表/表头/行结构恢复）

继续按 S2/S5 检查产品语料时，发现指标 XLSX 虽已能被实验账本读取，但普通 RAG 摄取仍会把 XML 单元格值压成一段文本，
导致 `metric`、数值和不同指标行之间缺少显式结构。这样即使来源被召回，生成阶段也更容易发生字段—数值错配，尤其影响
“准确率是多少”“P95 是多少”这类指标问答。

本轮参考 [RAGFlow](https://github.com/infiniflow/ragflow) 的表格结构保留和
[Dify Knowledge Pipeline](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的结构化摄取分层，完成最小修复：

- `core/plain_document_parser.py` 新增受限 XLSX XML 表格读取：保留工作表标题，按单元格列坐标恢复表头、空列和数据行，
  并支持普通值、inline/shared string 与公式缓存值。
- 对当前产品夹具中缺少 `workbook.xml`、单元格内嵌 CSV 的轻量导出，恢复为标准 Markdown 表格，得到
  `accuracy=0.921`、`p95_latency_ms=960` 两行；来源定位为 `sheet:sheet1/embedded_rows:2-3`。
- 继续复用现有 table-aware 父子切片器，不改向量模型、排序权重、Top-K、项目权限或 GraphRAG 路由。

验证结果：实验账本、XLSX/CSV 解析、父子切片、检索、引用、RAG 提示词和双项目隔离定向回归 `145 passed`；产品夹具只读核验
得到上述表格结构。详细记录见 `eval/results/rag_xlsx_table_structure_recheck_20260830.json`。

证据边界：本轮证明的是 XLSX 结构化摄取和行级来源定位，不是新索引上的 Recall@K、MRR、答案准确率或多模态业务效果提升；
完整 Excel 公式/样式/图表语义和单元格级地址仍未支持。正式加密工作区仍锁定，GraphRAG 生产验收、真实多模态 30 episode、
完整 VLM+RAG P95、答案级人工/NLI 和 sealed-test 仍未完成；本轮继续不勾选 S1/S3/S5/S6 整体验收项。

## 58. 本轮继续执行记录（2026-08-30，XLSX 表格结构经过真实摄取与项目检索烟测）

仅验证解析器还不足以证明 RAG 链路没有丢结构。本轮把当前产品的轻量
`vision_august_metrics.xlsx` 送入临时 `LocalKnowledgeBase`，再通过
`search_with_evidence` 执行项目内检索，参考 [RAGFlow](https://github.com/infiniflow/ragflow) 的来源可追溯原则和
[Dify Knowledge Pipeline](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-pipeline/knowledge-pipeline-orchestration)
的分层验证方式：

- 解析得到 `1` 个父文档，实际入库得到 `1` 个 child；来源仍为 `vision_august_metrics.xlsx`。
- 检索证据保留 `## sheet1`、`metric/value` 表头、`accuracy=0.921` 和 `p95_latency_ms=960` 两行，
  定位为 `sheet:sheet1/embedded_rows:2-3`。
- 项目路由为 `project_first`，全局候选数为 `0`，私有来源泄露为 `false`；这证明结构化来源没有在“解析→入库→检索”
  之间消失，但不证明真实模型的召回排序。

详细记录见 `eval/results/rag_xlsx_table_ingestion_smoke_recheck_20260830.json`。烟测使用确定性假 embedding，
因此不将它计入 Recall@K、MRR、答案准确率或多模态指标；当前产品 30 条完整评测仍需在新索引上单独重跑。正式加密工作区仍锁定，
GraphRAG 生产验收、真实多模态 30 episode、完整 VLM+RAG P95、答案级人工/NLI 和 sealed-test 仍未完成；本轮继续不勾选
S1/S3/S5/S6 整体验收项。
