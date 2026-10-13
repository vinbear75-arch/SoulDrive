# 2026-10-14 既有项目包桌面验收：导入修复与未通过项

本轮起点为 `cef201dba128f297cfffd82c6f9b46d3924e5868`。接续原计划 E7，不把原四 CSV 主线替换成全部验收，也不把工程检查通过替换成研究回答质量通过。goal 保持 active。

## 范围、baseline 与最小候选

- 使用仓库既有 `eval/fixtures/product_b1_v0.1` 的六份原件：两份 PDF 文本报告、两份 CSV 指标及两份 YAML 配置。它们是既有合成演示材料，不是新业务评测集；不读取冻结题目/gold 来调参。
- manifest SHA-256 为 `5a040c620ea7a2fd8ebec1bc99456826ae6950cc61d926dc60dd20c9ded69b40`。另建虚构项目及加密副本，不改原件、原 30 条业务种子、四条标签草案或统计/时间定义。
- B0：真实桌面文件选择器仅提供 PDF/图片过滤，CSV/YAML 被隐藏；原按钮也写着“导入 PDF 或截图”。新增工程反例先失败，缺少后端已有的十类扩展名。
- Candidate：只把既有 `rfd` 过滤和两个按钮说明对齐 `SUPPORTED_DOCUMENT_EXTENSIONS` 的 15 类格式。没有新增解析器、框架、依赖或 TSV/ZIP 支持。后两者不能因指标解析器支持就冒充通用文档入口支持。
- 15 个扩展名的契约检查不等于逐类桌面验收；本轮实际摄取范围仅 PDF、CSV、YAML。
- 真实选中六份资料后又复现会话内部列被长文件名撑宽、发送按钮越界。新增布局反例先失败，只约束现有单列宽度，并让文件名省略、范围说明独立换行。实际重启后再次选中六份资料，发送、清除上下文和范围说明均可见。

外部机制对照仅用于定位入口与解析能力一致性：[Haystack FileTypeRouter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/routers/file_type_router.py) 区分按类型路由、未分类及失败输出；[MultiFileConverter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/converters/multi_file_converter.py) 将已支持类型接入对应转换器。[rfd add_filter](https://docs.rs/rfd/0.17.2/rfd/struct.FileDialog.html#method.add_filter) 提供现成扩展名过滤。SoulDrive 复用自己的解析链路，没有安装或复制 Haystack；这些参考不能证明本项目质量。

## 原件、导入与索引身份

实际桌面新建“E7 既有项目包验收”，项目 ID 为 `project_cf4317c65338e25e6b60f33b003ba58c`；导入前显式选择“当前项目专属”。原生选择器一次选择六个准确文件，返回新导入 6 / 已存在 0。A/B 原有四份资料没有改动。

| 原件 | SHA-256 |
| --- | --- |
| `07_baseline_report.pdf` | `e27286f97171d3fd08a337136d55398923adae153629b1c76f446b41e51b581d` |
| `07_baseline_config.yaml` | `6d4a1e254a02395a9a3c5b97055923b5d507f17580a4de3268425f24964a5ef2` |
| `07_baseline_metrics.csv` | `6e9833fe04b398b859a4034785187eeeafe8a2ed442e95bb40b4306506668449` |
| `08_alignment_report.pdf` | `5c97c6814b553335f71e3aeccadf42e34693ce58887eeeb0a6c9e52d981d0fe8` |
| `08_alignment_config.yaml` | `10d3164319c10ccfe392a56964aa4c9b3753684ff6f8ef029d33794092386ebc` |
| `08_alignment_metrics.csv` | `f5b023b47b6c429156b5a1ae065ec2b0ca72ce63b242419cca457df132b822cf` |

只读检查逐份解密自有副本，SHA-256 与 manifest/原件相同；六个目录版本的 MD5 为实际原件值。每个索引块的 `project_id`、`secure_` 对象 ID、`asset_version_id` 与对应目录记录一致，`all_six_index_identities_match=true`。CSV 保留 `rows:2-4`，YAML 保留原行范围；PDF 保留页码。真实界面逐份预览两份 YAML，字段和值均可读，未修改文件。

真实 UI 启动索引，run ID `75c527bc-825b-452c-b501-8765972fa804`，发现 10 份、跳过原有 4 份、新处理 6/6、失败 0，最终 completed。各文档实际嵌入元数据为 `qwen3-embedding:4b`。

该次索引墙钟约 286.349 秒。文档计时合计 `elapsed_ms=205147`，其中 parse 9029、ingest 49197、embedding 49120、graph 146810；另有 GraphRAG 构建汇总 70827 毫秒。不同统计层不能直接相加作为总时间。新项目图索引记录 6 份文档、4 个社区，未失败。此处没有验证图边/社区语义正确率，也没有证明吞吐提升或稳定 P95。结构化小文件的图处理成本是后续候选，不据单次结果直接关闭图谱。

## 指标预览：正确不写，口径仍缺失

在真实实验会话“E7 原件实验核对”选择 `07_baseline_metrics.csv` 预览，界面显示 `0/3 行可用`，确认不可用，并提示“缺失项尚未补齐；当前没有写入运行”。新项目账本实际仍为 0 条运行，A 的 2 条/B 的 0 条没有改变。

原 CSV 只有指标名与数值。预览列出的缺失字段为 `dataset_version`、`definition_version`、`direction`、`evaluation_script_ref`、`performed_at`、`split`、`unit`。没有把日期 placeholder 当已确认值，也没有使用统一默认单位/方向把准确率和 P95 混填。

manifest 两组的 dataset_version 不同，报告又涉及不同指标/数据集，逐指标对应仍需核对。它不是可比性已经成立的证明。本轮不补造口径，不按文件修改日期冒充 performed_at，不计算提升，不把报告中的因果措辞变成已证实原因。

## 真实材料问答：未通过，不能宣称 E7 闭合

确认自有虚构项目的实验用途后，重启仍保持同一会话与用途。选择六份准确来源，在真实桌面发送带明确“2026 年 7 月至 8 月”范围的问题，询问指标记录、数据集/配置差异、可比性及验证计划。

月份来自问题和材料标题。账本尚未入库，本次没有证明按 `performed_at` 完成时间过滤，不能用文件日期或这次提问代替时间语义验收。

实际 trace `9eda9f10-fbdc-4f10-a87e-93d1eda754f7`，request `984c3add-2c81-4a62-9f3f-a1d99611c917`，session `session_6638b25b-53b6-4b32-a729-922c2030ba47`：

- 范围为当前项目、六个显式 document ID；GraphRAG 因 `selected_document_scope` 暂停，避免引入范围外来源。无全局候选、无 A/B 资料混入。
- 混合召回、重排和展开均得到六份来源；**不是 Top-K 只取四份**。随后 context admission 将两份配置分别以 `score_below_floor` 拒绝，分数 0.013525 / 0.011355，最终四份 PDF/CSV 证据、424 字符进入生成。
- 检索耗时约 8.54 秒，实际检索 pass 为 1；两个固定 workflow 变体已应用，但动态 subquery 未生成，记录 `subquery_generation_unavailable` / `no_subqueries`。不能称问题分解或配置覆盖已成功。
- 模型答案尝试两次，分别因事实/引用不支持、引用了未提供的 `M1` 而拒绝。现有安全校验保留，未降低阈值或允许无来源标记。
- 最终只返回三条原文摘录，其他研究栏目为“待确认”；会话原文和 completed receipt 均落盘。`chat.completed total_ms=132278`、`under_5s=false`。`retrieval_trace.status=pass` 只表示进入生成，不是答案质量通过。

**结论：摄取/身份检查通过，问答安全拒绝生效，但配置比较、明确缺失原因、最小验证计划和响应性能未达成证明。** 报告原文的因果描述只是来源措辞，不作为本轮认可的结论。不根据这个演示样例调提示词、阈值或模型参数。

PPT 入口实际自动选中当前项目这六份来源，未混入其他项目；本轮尚未提交大纲请求或确认生成。桌面检测到人工输入后保留当前设置，等待操作偏好回复，不覆盖用户输入，不将参数表单当成已生成大纲。此前四 CSV 主线的真实 PPT/WPS/恢复证据仍独立保留，不能冒充此六原件主线已经通过。

## 完整工程回归与运行形态

- 最终 Windows PowerShell 5.1 完整隔离检查退出 0：后端 **1838 passed / 1 warning，329.85 秒**，前端 **105 项**，既有 memory/GraphRAG route/workflow roles/episodic runtime 四个有限工程门、compileall、生产构建及锁定 Cargo 均通过。成功/故意缺解释器失败路径均恢复原环境和位置，仓库 audit 指纹不变。工程门不是开放业务质量评测。
- Release Rust **7 passed**，包括开发解锁变量剥离及可选状态后端边界。首次单独测试日志路径写错，未获得有效输出，不计通过；修正绝对日志路径后重新运行退出 0。
- Debug/source-backend 第六次实际启动仍显示“本机开发模式已自动解锁”，GUI PID 85736；仅限固定虚构资料，未自动操作认证界面。真实工作区、默认用户启动器、Release 保护不变。
- 最终 Debug exe SHA-256 `956e3231c7276152633faa0867f9538a0f743b75b96f7b58dfa9eb1b8085ca1a`；最终 Release exe `0655ac3c6e362824117d3e4391714ab6af61918bf75f833fbeb8db380635c666`。最新布局的真实视觉检查是在 Debug；Release 构建/Rust 测试不是安装器或最新 Release 视觉验收。
- Python 生产模块本轮未改，不重新复制或打包一套 sidecar。首次组合测试误写不存在的测试文件，退出 4/no tests；更正后的摄取/打包相关测试为 36 passed，最终打包契约 7 passed。调用错误不计作产品缺陷或通过证据。

## 私有证据位置与指纹

均位于 `build/quality-gates/`，不上传临时密钥、工作区或日志正文：

| 记录 | SHA-256 |
| --- | --- |
| `check-round52-final-context-ps51.log` | `fcb4d1b02bee195ca4cc11b941a11293baf6f608d8117eb0c8c746b2d0dbda51` |
| `project-package-round52-index-final.json` | `d341483d3bf9d15f1519149a0c21e111e1ce88b563c9064f5a38856bb5e27ec0` |
| `project-package-round52-final-identity.json`（问答前快照） | `4f5bafe75ba458623232c07f22ace37d41fc047dfc4d7729e5be9040f2eb4bf3` |
| `project-package-round52-chat-final.json`（追加会话的独立快照） | `845b2a9035ad433764ca30abf6becab4db5b90878ca70f006b0da8e01dc337db` |
| `frontend-context-overflow-round52-final.log` | `3c0499360c2fd78b7406ac32e4a4aabcb2c5b82755ed151097514c6de5388da3` |
| `desktop-debug-context-overflow-round52.log` | `ab9917e64476beffcd99d7c64b084bd473c16f3b1a3d236ee97fab414a235ca8` |
| `desktop-release-context-overflow-round52.log` | `7c012d12226bee4c908bb90df6c7e6899f78b6538bcf6202227d98ae29f048eb` |
| `rust-release-round52-final-corrected.log` | `f090d06d65ba8b7fbfd1fbf29342ff2f72ffd543762258bcba1a9c4d56c05b65` |

失败优先记录为 `document-picker-round52-b0.log` 与 `context-overflow-round52-b0.log`。原件、派生索引身份和未通过回答分开保存，不覆盖先前快照。Ponytail 促使复用既有入口和 CSS，Computer Use 促使真实选择/预览/发送；Presentations 的事实与审核要求促使不把来源选择表单或缺口摘录当成已认可的完整演示产物。

## 保留的下一阶段边界

1. 先用独立工程反例确认“回答已回退却没有明确说明原因”的显示缺口；不放松来源校验，不把文档材料模式强制改成仅账本模式。
2. 对配置证据覆盖与动态分解提出同条件 baseline/candidate 方案后再评测。不得由本次失败直接调整默认阈值或伪造可比 M 测量。
3. 完成同一六原件主线的实际大纲审阅、确认及 PPT/WPS 检查；缺失或冲突口径须先审核，不能为继续演示而补值。
4. 业务数据审核、开放研究质量、性能及安装器等未验收项继续保留。本轮没有删除数据，不虚报新的磁盘释放量；先前约 40.8 GiB 清理与旧包删除被拦截的记录不变。
