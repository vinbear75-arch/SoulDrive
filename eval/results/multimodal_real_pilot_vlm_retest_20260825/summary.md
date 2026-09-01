# 真实授权 pilot VLM 慢路径复测

- episode：`REAL-PILOT-01`
- 状态：`pass`；总耗时：`24159.052 ms`
- ASR：`True`；OCR：`True`；VLM：`True`；SoulAlign：`True`
- 实际路由：`slow`
- ASR 原文：`总结我当前选中的实验结论。`
- VLM 原文：`选区中的界面内容是关于SoulDrive多模态对齐实验的结论，其中提到在60条模拟任务中，目标绑定准确率从0.68提升至0.85，热路径P`
- 目标绑定：`selected_experiment_conclusion`（期望 `selected_experiment_conclusion`）
- 项目保持：`True`；副作用：`none`

## 限制

- 本次只验证一条用户授权 pilot 的本地感知/对齐链路，不是比赛成绩。
- 没有把 RAG 检索、回答生成或 PPT/邮件副作用接入本次判定。
- VLM 慢路径的延迟与快路径分开记录，不能合并成一个 P95。
