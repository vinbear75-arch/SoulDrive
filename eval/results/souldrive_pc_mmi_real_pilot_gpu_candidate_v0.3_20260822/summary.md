# SoulDrive real MMI pilot

- 样本数：`10`
- Intent accuracy（B1/Candidate）：`1.0000/1.0000`
- Target binding（B1/Candidate）：`0.0/1.0`
- Action accuracy（B1/Candidate）：`1.0000/1.0000`
- Clarification accuracy（B1/Candidate）：`1.0000/1.0000`
- 冷启动：`14930.0 ms`
- 热运行 P50/P95/max：`1513.9/1638.5/1638.5 ms`
- 失败样本：`0`

## 限制

- 真实用户授权 WAV/截图，但仅 10 条 pilot；不是公开集或华为隐藏集成绩。
- 本轮使用快路径，未包含 VLM、完整 RAG、首 token 和完整回答生成。
- PILOT-11/12 未采集，不补造样本。
