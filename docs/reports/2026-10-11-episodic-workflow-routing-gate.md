# episodic 研究窗口行为差异评测接入工程门禁

## 范围

本轮只处理一个可复现的工程缺口：已有 episodic workflow routing 评测可以输出 runtime 与 baseline 的结果，但它此前没有失败退出语义，也没有接入 `scripts/check.ps1`，因此不能作为持续验收条件。没有修改 episodic 固定样本、gold、业务种子、训练数据或调参配置。

外部机制仍以已固定的候选材料为参照：[Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的时间/来源 provenance、[LongMemEval 固定仓库](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 对时间推理与知识更新边界的关注，以及 [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的显式 pipeline 路由和可观测性。这里借鉴的是“路由契约必须可复现、失败必须阻断”的工程边界，不把本地 5 条规则样本冒充 LongMemEval 或开放式答案质量结果。

## 发现的 B0 缺口

- `scripts/eval_episodic_workflow_routing.py` 原先只打印结果，`main()` 无论 runtime 样本是否通过都返回 0。
- 评测没有校验样本 ID、工作流、事件身份、项目范围、时间戳以及 expected/forbidden 引用是否自洽，错误数据可能被误当成通过。
- `scripts/check.ps1` 没有运行这条评测；之前的“runtime 5/5”只能作为手工命令结果，不能控制工程门禁。

## 最小改动

- 增加固定数据的结构校验：唯一记录 ID、受支持的窗口/事件类型、非空项目范围、ISO-8601 事件时间、事件身份唯一性、expected/forbidden 不重叠，以及 expected 事件确实存在于该样本。
- 增加 `evaluate()` 和 `--output`，runtime 模式要求所有样本通过才返回非零；baseline 继续作为诊断对照，不因预期缺少 activity projection 而阻断；默认比较模式仍以 runtime 结果决定退出状态。
- 将 `--runtime` 评测接入 `scripts/check.ps1`，逐样本结果保存到 `build/quality-gates/episodic-workflow-routing.json`。
- 增加失败优先、数据校验、固定集通过和总门禁短路测试，共 4 项。

## 验收证据

- 新增定向回归：`4 passed`。
- 研究窗口、episodic view、工作流角色及评测组合：`51 passed, 1 warning`。
- 独立完整 `scripts/check.ps1`：退出 0；后端 `1768 passed, 1 warning，426.50 秒`；记忆质量 `7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/禁止泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口角色 `6/6`；episodic runtime `5/5`；前端生产构建成功；UI 脚本 `66 passed`；`compileall` 与锁定 Cargo 检查通过。
- 同一固定集的 baseline 为 `2/5`，这是有意保留的对照结果：它显示没有 episodic 投影时，会议历史/任务和实验运行召回会失败，而当前 runtime 能通过对应契约；baseline 不被错误提升为质量门通过。

## 仍未证明

- 5 条固定样本只验证项目内活动投影、窗口范围和显式时间词的确定性路由，不证明长期记忆、跨文档综合、GraphRAG 内容、模型答案事实性、召回率或 LongMemEval 分数。
- `paper_review` 当前仍按证据边界保持 `workflow_not_enabled`；本轮只把这一行为锁定为可观察结果，没有擅自把论文窗口接入历史活动。是否需要论文窗口的跨会话研究史，应由后续业务样本与人工审核决定。
- 仍未证明真实 Redis/PostgreSQL、跨进程/跨主机 exactly-once、断电恢复、真实 Tauri/WPS UI/E2E 或发布包行为。
