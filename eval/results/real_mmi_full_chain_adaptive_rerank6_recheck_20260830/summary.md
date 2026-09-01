# SoulDrive real MMI pilot

- 样本数：`10`
- Intent accuracy（B1/Candidate）：`0.9000/0.9000`
- Target binding（B1/Candidate）：`0.0/1.0`
- Action accuracy（B1/Candidate）：`1.0000/1.0000`
- Clarification accuracy（B1/Candidate）：`1.0000/1.0000`
- RAG 全链路：`7/7` 个可检索样本完整通过
- 显式感知预热：`pass`，`22875.3 ms`
- 冷启动：`16314.6 ms`
- 热运行 P50/P95/max：`6958.2/12177.8/12177.8 ms`
- 失败样本：`1`

## 限制

- 真实用户授权 WAV/截图，但仅 10 条 pilot；不是公开集或华为隐藏集成绩。
- 本轮使用快路径，VLM 慢路径仍未纳入；PILOT-11/12 未采集，不补造样本。
- RAG 全链路只在显式提供的本地测试工作区运行；查询来自 ASR，未注入答案标签；澄清样本在检索前记为 not_applicable。
- 该模式测量检索、首模型 token、完整回答和引用是否存在，不等于答案事实准确率或竞赛成绩。
