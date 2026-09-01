# SoulDrive-PC-MMI-480

这是从人工审阅 seed v0.2 扩展出的封闭业务集，用于训练和评测 harness。

- 划分：320 train / 80 validation / 80 test。
- 任务：语音文本 + UI/OCR/屏幕上下文 + 项目范围 + 冲突与澄清。
- 模板：每条样本唯一 template_id，跨 split 不复用模板 ID。
- 边界：不把合成结果当作公开集或比赛成绩。
