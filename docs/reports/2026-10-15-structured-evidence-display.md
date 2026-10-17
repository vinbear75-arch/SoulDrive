# 结构化证据呈现：基线与验收

## 范围

B0 为 `f2b1614d411577ae762125c4580a7f4554873e85`。上一轮实际桌面仍把 CSV 显示成连续管道符，把 YAML 合成一行；处理中统一称“模型生成中”，即使该路线实际没有调用模型。

本轮只做结构保留和呈现，不修改原数据、指标、比较条件、排序阈值或模型参数。独立虚构表格/配置先验证，既有六原件只作同版本实际链路检查，不读待审核种子/gold 调参。

官方参考：

- [React Markdown](https://github.com/remarkjs/react-markdown/blob/main/readme.md) 与 [remark-gfm](https://github.com/remarkjs/remark-gfm)：沿用已有表格与安全 Markdown 渲染能力，不安装表格框架。
- [Carbon 数据表](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/guidelines)：资料以行列展示，表头和结果区边界明确；只参考呈现原则，不引入 Carbon 依赖。

通过条件：准入、提示词和未确认比较准备结果保留表头/行、配置换行与嵌套缩进；截断不把首尾两个表格片段拼成虚假行，预算不变。结果以原生表格/文本代码块展示，保留来源和原始值，不自动添加单位或增幅；宽表只在结果区内滚动。复用一个小的 Markdown 组件覆盖问答和两处文档预览，图片链接不自动加载，原有明确授权的图片预览独立保留。处理提示使用真实通用状态。

验证顺序：失败优先单测 → 后端/前端回归及真实组件渲染 → 同六原件实际桌面/保存/找回 → 完整工程检查。当前未预写通过；Debug 不当作发行包验收。

## 已执行结果

- 原实现先复现换行/表格截断失败。准入与最终向量提示共用有界原文压缩：保留换行、缩进，结构化内容仅保留完整前缀行，不拼接首尾片段；不提高预算或绕过证据门。
- 生成结果按来源分别展示表格、配置代码块和报告原文。真实 React Markdown 服务端渲染验证表头、空白单元格、转义管道、原值和不可执行 HTML；同一组件用于聊天和两处文档预览，图片 URL 不自动获取，不新增依赖。
- 附带发现的引用解析断点先复现：外层代码块里的 `souldrive-evidence` 示例曾变成 E99 卡片并吞掉原文。现在识别外层围栏，引用只取末尾系统快照；嵌套代码、引文、较早示例及未闭合围栏不合并为引用。这是有限协议解析，不宣称通用 Markdown 安全证明或来源签名。
- 定向后端 330 项通过（1 个既有依赖警告）；前端组件/协议 5 项通过。完整 Windows PowerShell 5.1 工程检查后端 **1867 passed, 1 warning / 324.26 秒**，前端累计 111 项，原 memory / graph route / workflow / episodic 工程门、前端构建、编译和锁定 Cargo 通过。之后增强的打包 smoke 独立实跑，结果另记，不冒充上述检查已经覆盖该新增脚本路线。
- 现有六原件后端实际回放保持六份来源，600 字符（上一轮 596，差异是保留原始换行），预算不变；两表/两配置结构检查通过，未确认路线模型调用 0、写入 0。暖检索 362.524 ms 仅为该次回放，不作为稳定 P95 或模型综合速度提升。
- 实际嵌入页面的 Debug 窗口重发同一问题，六来源、两张原生表格、两段多行配置和原值可见；会话切换与真实关闭/重启后找回相同结果，虚构开发工作区自动就绪，没有要求用户输入口令。原件、目录/版本和索引绑定复核一致，项目仍 0 条实验运行。
- 本次桌面请求 **15516 ms，under_5s=false**；检索 7992 ms，其中问题嵌入 5914 ms、重排 1992 ms。它是未确认准备流程，不是开放式比较质量通过，不能拿它与上一轮 4668 ms 或模型失败请求混作速度提升。
- 首次直接 Cargo Debug 构建依赖未启动的开发网页服务，实际窗口加载失败；随后使用官方 Tauri 内嵌页面构建并实测。该失败保留，不当作成功启动。

## 可复核证据（本机，不上传私有工作区）

- 完整检查：`build/quality-gates/check-round55-structured-ps51.log`，SHA-256 `dbb58f2dc38bf2505689cb0bc2468720cb3d1dee6e9fc896faa0822b63bdd89e`。
- 桌面请求 `5c3345a5-66e6-4f54-a093-3109613ae0c6`，trace `e649ccf1-1f66-4f3c-998f-71c532a37add`；保存答案 SHA-256 `188c9c1d97520b2bea4d15d917c562035659df12283df6aaed3daf5ec418be28`。
- 只读核验：`build/quality-gates/research-context-round55-ui.json`，SHA-256 `9b7e82b7b96979ef17dde8362acba7300c1021fe152512125d0b44bae34abab8`；包含完成收据、6 来源/600 字符、2 表/2 配置、0 模型调用、0 新运行。
- Debug SHA-256 `9ad0a303b6a059e84c6df64d5a304076e8d533f297e11f181dcdb21c2f4d8a87`。完整原件 manifest 保持 `5a040c620ea7a2fd8ebec1bc99456826ae6950cc61d926dc60dd20c9ded69b40`。
- 当前服务包已正常重建，六个相关模块的打包字节码与源码一致，包 SHA-256 `b934919bc0769170fe78427d2262ede2a9498f417f50a51333b521e48af63ddb`。两次冷启动/实际实验及准备流程、Release 和安装器的最终状态分别记录，不由字节码一致性推定通过。

## 打包实测暴露的选定来源断点

首次新包 smoke 保留在 `build/quality-gates/packaged-round55-experiment/`，**fail**，不是通过：四个独立虚构 CSV 索引 4/4 完成，指标确认/重复提交/跨项目拒绝通过；新增未绑定测量准备问题却得到 0 来源。项目相关性 0.05 低于原 0.34 门槛，先取得的两个选定候选被全局 fallback 替换为空；不是 OCR、模型回答或来源本身缺失。错误拒绝文案把 0 来源写成“一个来源”的呈现问题仍需修复。

参考 [Haystack 查询时元数据过滤](https://docs.haystack.deepset.ai/docs/metadata-filtering)，先用另一个独立 precision 表反例复现失败，再区分“检索语料范围”和“事实足以回答问题”：明确选定文档保留原项目/对象过滤后的候选，不因项目语言重叠低而替换检索范围。共享全局文档仍只能是明确选中的对象，其他项目、同名但不同对象、未知对象及 GraphRAG 摘要不混入；无选定文档时原项目优先/fallback 不变，最终准入、可比性和答案事实门不变。

新独立测试覆盖本项目两对象、同名其他项目、未知对象、明确选中的共享对象，失败优先后定向检索/上下文组合 **198 passed / 58.74 秒**。路由记录 `selected_documents`，仍如实保留相关性 `false`，不伪造相关性分数。修复后的完整检查、七模块包一致性和同四文件 smoke 另行复验；首次构建和首次失败都保留，不改原件、指标、时间定义或待审核业务标签。

修复后完整 `scripts/check.ps1` 已退出 0：后端 **1868 passed, 1 warning / 289.59 秒**，前端 111 项、原四工程门、编译/构建/锁定 Cargo 通过。该次使用本机已有 PowerShell 7.6.5；Windows PowerShell 5.1 的单独脚本启动被执行策略拒绝，没有修改策略，不能将本次结果标成 PS5.1。最终日志 `build/quality-gates/check-round55-final-scope.log`，SHA-256 `be706e38255fc0fb2feb3b5fd5ead0ff0cccc5ebe5cab59200c29ddcc06a1838`。七模块封装和首次失败问题的实际重放仍待完成。

## 仍未闭环

### 2026-10-17 补齐第五十五轮已结束的发行证据

修复后独立四 CSV 的同链路 smoke **pass**，两个真实服务进程均正常退出 0，冷启动 2109 / 1063 ms。4/4 索引、指标确认/同键重放/跨项目拒绝、未绑定测量准备、已选运行比较、四页 OpenXML PPT 和第二次进程启动后的结果回放均完成；准备结果模型调用 0、新运行 0。回答的模型生成仍两次未通过事实校验，最终为证据核对，不能把链路通过当作自由研究生成通过。

最终服务包 SHA-256 `6e5d7768becce03084743607e038655cb5dfa857408af9f7432d38dea9ffa0c5`；七个相关模块（含选定来源修复）封装字节码与源码一致，核验日志 SHA-256 `8edc0f1bfe7903cc411fcd884f69e344d40fb43ba7cae75ae6c176e3e6e98996`。第二次正常构建仅复用自有 R55 工作目录，无 `--clean`，不触及先前被拦截的清理目标；构建日志 SHA-256 `3ca60bfb5b3281de2b3056330a3c2bf16eeb668156627be3f102346b7debe97e`。

Smoke 报告 `build/quality-gates/packaged-round55-scope-repair/report.json`，SHA-256 `77fad852a0034cd10c4d5ae1a2b1ab4a05e236f26f3b23f72455a9c87087242f`；PPT SHA-256 `00462a0e2830a5ac8cd817edfa5a1046d059366c24185c57de09c82d4e76072f`。该项 `desktop_or_installer_tested=false` 保持原值，本轮没有重新逐页视觉检查此工程 PPT，也不将其当安装器验收。

当时验证的 R55 Release SHA-256 `9d535c8a0227dc6794e51b23f9afe5c0bfac61e58d0bb4b565bd7f2d3bbc30f8`，构建日志 SHA-256 `2fba228e2f15f6a6a42097b887ecff41b24afe82b06a0fdd6bec19f853e8e44f`。2026-10-17 使用该包实际启动自有虚构工作区，运行态 `locked=true / software_unlocked=false / HARDWARE_ONLY`，实际界面锁定，继承的开发解锁变量没有生效；未输入口令。用户随后按 Escape 停止桌面控制，立即停止，没有宣称已关闭该窗口或完成解锁后发行流程。源码提交 `b7e8a4a` 已核对 GitHub 同步；后续源码变更不由本次包验证自动覆盖。

此轮未修复证据侧栏的单行片段展示、窄窗口标题拥挤，也未证明自由研究综合、因果、长期记忆业务质量或 5 秒性能。尚未审核冻结的业务标签不能用来调参/评分。

下一记忆验收参考 [Graphiti 时间字段与原始 episode](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)、[Graphiti 检索与有效时间示例](https://help.getzep.com/graphiti/getting-started/quick-start) 和 [Copilot 记忆管理及保留边界](https://learn.microsoft.com/en-us/microsoft-365/copilot/copilot-personalization-memory)：在独立虚构项目核查来源定位、修订历史/有效时间、跨项目和跨研究窗口隔离、撤销后不再使用。只借鉴机制，不接入云服务/图数据库，不把对方能力当成本项目已实现。当前 goal 保持 active。
