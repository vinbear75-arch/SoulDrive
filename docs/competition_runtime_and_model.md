# SoulDrive 运行与模型说明

## 运行边界

SoulDrive 当前定位为 Windows 本机单用户项目工作台。模型、索引、会话、审计和媒体文件都留在本机工作区内，不默认走云端。

## 当前使用的关键组件

- `SenseVoiceSmall`：本地语音识别。
- `RapidOCR`：本地 OCR 快路径。
- `Qwen2.5-VL-3B-Instruct`：授权区域上的视觉慢路径。
- `SoulAlign-lite`：多模态意图/目标对齐的轻量融合头。
- UIA / OCR / VLM 协同视觉证据：同一屏幕内容如果被多源一致确认，会合并成更强的可引用视觉证据。

## 主要运行路径

- `POST /chat`：项目问答和证据式回答入口。
- `POST /multimodal/runtime/infer-upload`：语音/图片/事件上传入口。
- `POST /projects/{project_id}`、`DELETE /projects/{project_id}`：项目生命周期入口。
- `POST /projects/{project_id}/sessions`、`DELETE /projects/{project_id}/sessions/{session_id}`：项目会话入口。

## 已验证的运行事实

| 项目 | 结果 |
| --- | --- |
| 后端关键组合测试 | `136 passed, 1 warning` |
| 前端媒体事件测试 | `8 passed` |
| 前端项目/文档测试 | `3 passed` |
| Python 编译 | 通过 |
| 前端生产构建 | 通过 |
| Tauri `cargo check --locked` | 通过 |

## 运行时表现边界

| 场景 | 已验证结果 | 含义 |
| --- | --- | --- |
| 真实授权 pilot | 10 条样本，目标绑定为 1.0 | 证明本机多模态目标绑定可用 |
| 真实授权 pilot | P95 仍偏高 | 不能当成完整端到端交付完成 |
| 80 条合成媒体测试 | 目标绑定 1.0 | 证明快路径联调可用 |
| 视觉慢路径 smoke | 单样本可跑通 | 不能当作完整 P95 结论 |

## 建议写法

参赛材料里可以写：

- SoulDrive 运行在本机 Windows PC 上。
- 系统以项目为边界组织会话、知识、实验和产物。
- 语音、屏幕、OCR、视觉和项目元数据在同一条事件链上对齐。

不要写成：

- 所有场景都已达到最终性能门槛。
- 视觉慢路径已经稳定满足完整端到端延迟要求。
- 试点样本可以替代正式业务评测。
