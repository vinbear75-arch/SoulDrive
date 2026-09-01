# 真实授权 pilot VLM 慢路径复测

- episode：`REAL-PILOT-01`
- 显式 VLM 预热：`True`；预热耗时：`8248.52 ms`
- 状态：`pass`；总耗时：`13175.698 ms`
- ASR：`True`；OCR：`True`；VLM：`True`；SoulAlign：`True`
- 实际路由：`slow`
- ASR 原文：`总结我当前选中的实验结论。`
- VLM 原文：`总结：在60条模拟任务中，加入显式选区和2.5秒时间窗后，目标绑定准确率从0.68提升至0.85；热路径P95延迟为0.79秒。对于文字选区任务，应优先采用UI选`
- 目标绑定：`selected_experiment_conclusion`（期望 `selected_experiment_conclusion`）
- 项目上下文来自授权事件：`True`；副作用：`none`

## 限制

- 本次只验证一条用户授权 pilot 的本地感知/对齐链路，不是比赛成绩。
- 没有把 RAG 检索、回答生成或 PPT/邮件副作用接入本次判定。
- VLM 慢路径的延迟与快路径分开记录，不能合并成一个 P95。
- 显式 VLM 预热成本单独记录；预热后的请求耗时仍未达到竞赛门槛时，不得把预热隐藏为零成本。
