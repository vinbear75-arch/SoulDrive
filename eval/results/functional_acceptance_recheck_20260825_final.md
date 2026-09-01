# SoulDrive 核心功能链路复核矩阵

复核时间：2026-08-25（Asia/Shanghai）  
范围：Windows 本机单用户模式；只把有真实服务响应、数据库记录、审计事件或产物的项目标为通过。  
说明：本文件记录“功能通路”而不是竞赛准确率或性能成绩。

## 总结

核心链路已经可以从本机 sidecar 入口跑通：文档索引可读、普通 RAG 可回答、GraphRAG 索引已构建、多模态快慢路径均可执行、PPT 可生成并通过 OpenXML/溢出检查、QQ SMTP 配置和发送确认门可用、Redis/PostgreSQL/文件归档可以绑定同一个会话并恢复。

当前不能写成“竞赛级完成”的原因：RAG 普通问答本轮完整响应约 205 秒，重排占主要时间；多模态真实授权样本仍不足 30 条；PPT 末页仍可能生成“未命名”空泛页；真实 QQ 投递的外部收件箱回执仍需用户查看；Skill 的联网下载在本轮只验证了本地 mock 下载和审批链，尚未把任意远程 Skill 当作可信代码自动执行。

## 验收矩阵

| 功能 | 本轮/既有真实证据 | 结果 | 备注 |
|---|---|---|---|
| 主 Tauri sidecar 冷运行 | 主窗口 sidecar `127.0.0.1:52512/health` 返回 HTTP 200 | 通过 | 文件归档、Redis、PostgreSQL 均 ok，`degraded=false` |
| 文档库与索引状态 | `GET /documents/list` 返回 11/11 已索引；`GET /presentations/sources` 返回可用且已索引来源 | 通过 | 派生索引存在，可供 RAG/PPT 使用 |
| 普通 RAG | 当前复核服务 `/chat`：HTTP 200；首字节约 119 ms；完整响应约 205372 ms；有 `[E#]`、`souldrive-evidence` 且未拒答 | 功能通过，性能不通过 | 当前 Ollama `qwen3.5:9b` + Qwen3 Reranker 0.6B 链路过慢 |
| GraphRAG 索引 | `GET /diagnostics/graphrag`：Leiden `leiden_v1_local_move_refine`，242 实体、294 关系、10 文档、4 社区 | 通过 | 4 个社区满足实验范围；详见 `eval/results/graphrag_acceptance_recheck_20260825.json` |
| GraphRAG Global/Local 路由 | 同一工作区的真实 `/chat` 审计事件记录 `rag.graph_route`，Global 与 Local 均进入 `use_graph=true`，并完成证据回答 | 功能通过 | 详见 `eval/results/graphrag_acceptance_recheck_20260825.json` |
| 多模态快路径 | WAV+PNG 上传 HTTP 200；SenseVoice ASR、RapidOCR、SoulAlign 均 pass；端到端约 1072 ms | 通过 | 合成开发媒体，不是 30 条真实授权样本 |
| 多模态慢视觉路径 | PNG+文本上传 HTTP 200；Qwen2.5-VL-3B-Instruct slow 路径 pass；VLM 约 13507 ms；SoulAlign pass | 通过 | 冷启动并发时曾超时，重启后单独复测成功；需继续做冷/热 P95 |
| PPT 生成 API | `/presentations/generate` HTTP 202；任务 5/5 完成；4 页；`SoulDrive OpenXML`；来源覆盖率 100% | 通过 | 产物：`runtime/workspace/data/presentations/RAG GraphRAG 链路复核汇报-e35b8f10.pptx` |
| PPT 产物校验 | `render_slides.py` 渲染 4 页；`slides_test.py` 输出 `Test passed. No overflow detected.`；逐页视觉检查完成 | 通过但需调优 | 第 4 页标题为“未命名”且内容过少 |
| QQ SMTP 配置 | `/email/smtp-status?sender=1342325858@qq.com` HTTP 200，`configured=true`，provider=qq | 通过 | 授权码未进入日志/报告 |
| 本地联系人 | `/contacts` 与 `/contacts/resolve` HTTP 200；`handsome_Vin` -> `2956584050@qq.com` | 通过 | 联系人已保存并可按别名解析 |
| 邮件确认门 | 创建草稿 HTTP 200；未确认发送 HTTP 409 `email draft requires confirmation`；测试草稿随后取消 | 通过 | 防止未确认的真实外发 |
| QQ 真实外发 | 审计链已有 `email.smtp_configured`、`artifact.status_changed(confirmed)`、`email.sent`；artifact=`artifact_325a...` | 已有真实投递证据 | 外部收件箱是否收到仍需用户查看；本轮未重复外发 |
| 邮件 MIME/附件 | `eval/results/artifact_smoke_functional_recheck_20260825.json`：QQ SMTP MIME mock、1 个附件、授权码未暴露 | 通过 | 这是本地无副作用发送测试，不等同于真实外网投递 |
| Redis 短期会话 | Redis PING；会话 `http-recheck-20260825` 消息列表 8 条、metadata 存在、TTL 约 602098 秒、Vector Set 维度 2560 | 通过 | 实际键前缀为 `souldrive:session:*` |
| PostgreSQL 会话控制面 | 同一会话行 project_id=`rag-cn-demo-20260824`、latest_sequence=8；消息 8 条；pending outbox=0 | 通过 | DSN 仅记录到 D 盘本机服务 |
| 文件归档与重启恢复 | 同一会话历史 API HTTP 200；归档位于 D 盘；临时 sidecar 重启后仍可读取；主 sidecar health 归档 ok | 通过 | 旧残留测试文件的坏密文不在主当前 health 中复现 |
| 项目空间/会话 | 项目列表、项目会话列表均 HTTP 200；会话有标题；新会话创建/列出/删除后历史 404；Redis 键和 PostgreSQL 行均清理 | 通过 | 删除测试会话：`api-session-delete-recheck-94d2f5e8` |
| Skill 本地审批链 | `artifact_smoke_functional_recheck_20260825.json`：下载进入 inbox、审批后 active skill 可发现 | 本地链通过 | 下载器为 mock；未授权自动执行脚本，`scripts_auto_executed=false` |
| 前端功能回归 | `npm.cmd run test`：17 个前端断言通过；`npm.cmd run build` 成功 | 通过 | 含错误分类、媒体捕获、思维导图解析、学习入口 |
| Python 回归 | `python -m pytest -q`：585 passed，1 warning | 通过 | warning 为 Starlette/httpx 兼容性弃用提示 |
| 审计链 | 当前 `diagnostics/product`：769/769 事件校验，`broken_at=null` | 通过 | 审计哈希链完整 |

## 明确未通过/未完全证明的项

1. **性能**：普通 RAG 当前约 205 秒；既有 GraphRAG 运行也在约 77--249 秒量级，功能通过但不满足交互或竞赛 P95。
2. **真实多模态数据**：当前快慢路径使用合成开发媒体；30 条真实授权语音+屏幕选区样本和冷/热 P50/P95 尚未冻结。
3. **PPT 内容质量**：末页“未命名”说明大纲抽取兜底仍会产生空泛页面，需要在效果阶段修复。
4. **真实邮件外部回执**：系统审计已记录一次 QQ SMTP `sent`，但必须由用户在 `2956584050@qq.com` 收件箱确认，不能由本地日志替代。
5. **Skill 安全与兼容性**：当前证明的是“下载 -> inbox -> 人工审批 -> 项目发现”链路，不证明任意 GitHub Skill 都能安全、兼容地像 Codex/Claude Code 一样执行；这应继续保持人工审批和沙箱边界。
6. **旧坏测试归档**：`goal-storage-recheck-2a709b8dc1/messages.jsonl.enc` 曾被旧 sidecar 读到认证失败；文件未删除，因其属于旧测试会话，保留给用户决定是否隔离/清理。当前 Tauri 主 sidecar（52512）health 未再报告 degraded。

## 结论

以“功能通路优先”为标准，核心功能已经具备可运行证据；以“竞赛级产品”为标准，下一阶段应优先优化重排/检索延迟、补齐真实多模态样本与 P95、修复 PPT 空泛页，并完成真实邮件收件箱回执确认。准确率、召回率、引用覆盖率和消融实验不能用本次通路验收替代。
