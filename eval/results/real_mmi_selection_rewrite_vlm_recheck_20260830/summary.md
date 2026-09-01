# SoulDrive real MMI pilot

- 样本数：`1`
- Intent accuracy（B1/Candidate）：`1.0000/1.0000`
- Target binding（B1/Candidate）：`0.0/1.0`
- Action accuracy（B1/Candidate）：`1.0000/1.0000`
- Clarification accuracy（B1/Candidate）：`1.0000/1.0000`
- RAG 全链路：`0/1` 个可检索样本完整通过
- 显式感知预热：`pass`，`5829.9 ms`
- 冷启动：`28968.9 ms`
- 热运行 P50/P95/max：`0.0/0.0/0.0 ms`
- 失败样本：`0`

## 限制

- 真实用户授权 WAV/截图，但仅 10 条 pilot；不是公开集或华为隐藏集成绩。
- 本轮显式执行 VLM 慢路径；是否通过和耗时以逐阶段记录为准，不能外推为图标/图表准确率。
- RAG 全链路只在显式提供的本地测试工作区运行；查询来自 ASR，未注入答案标签；澄清样本在检索前记为 not_applicable。
- 该模式测量检索、首模型 token、完整回答，以及复用生产校验器的引用内容支持；支持检查是确定性词法代理，不等于答案事实准确率或竞赛成绩。
