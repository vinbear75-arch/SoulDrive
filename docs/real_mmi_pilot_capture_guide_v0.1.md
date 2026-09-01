# SoulDrive-PC-MMI-Real pilot 采集指南

当前第一步只采集 12 条 pilot，不用于宣称比赛成绩；目标是验证真实语音、真实 PC 选区和本地 episode 组装链路。
后续扩展到路线图要求的至少 30 条时，沿用同一字段契约，不要只保存音频和截图。

## 你需要做的部分

只需要一次本机人工操作：

1. 准备一个不含真实姓名、邮箱、密钥和未公开材料的演示项目。
2. 打开 SoulDrive，按 `data/local/souldrive_pc_mmi_real_v0.1/tasks.json` 的顺序执行 12 张任务卡。
3. 每张卡按住录音按钮，说出 `prompt`；松开后保留生成的 WAV。
4. 主动授权当前项目窗口或选区截图；不要共享整个桌面或后台窗口。
5. 记录本轮匿名说话人 ID（例如 `speaker-01`）、实际应用名、截图分辨率、目标类别（`text`/`icon`/`chart`/`table`/`window`/`none`）和授权目标框；目标框使用相对截图的 `x,y,width,height`，每项范围均为 0 到 1。
6. 将 WAV 放入 `data/local/souldrive_pc_mmi_real_v0.1/incoming/audio/`，截图放入 `data/local/souldrive_pc_mmi_real_v0.1/incoming/screens/`，文件名使用任务编号，例如 `PILOT-01.wav`、`PILOT-01.png`。
7. 告诉我“pilot 文件已放好”；我负责组装、校验、评测和生成报告。

## 我会自动完成的部分

- 检查 WAV 是否为单声道、16-bit、16 kHz 且不超过 15 秒；
- 检查截图格式、大小和路径安全；
- 从截图读取实际分辨率，并校验说话人、应用、目标类别和目标框等元数据；
- 生成标准 `episode_v0.1`；
- 运行 text-only B1、规则对齐 B1 和当前 Candidate；
- 保存逐条预测、延迟、失败分类和 manifest；
- 根据结果决定是否扩展到 60/120 条和是否需要重新训练。

## 标签原则

任务卡只提供“期望任务类型”，不是不可修改的金标。真实采集后必须确认：

- 截图是否确实包含用户想指向的目标；
- 项目上下文是否正确；
- `clarify` 是否真的需要；
- 邮件和日程是否只是草稿；
- 删除、发送等越界请求是否拒绝。

若一条样本无法确认，不要猜测；标记为待审阅，我会将其排除出训练集并保留在失败分析中。

## 集合审计

写入后先运行普通审计；准备作为真实多模态评测集前，再运行严格审计。严格审计失败时不要手工改 JSON，
应补采或补齐可追溯的人工标注：

```powershell
conda run -n souldrive python scripts/audit_real_mmi_collection.py `
  --collection-root data/local/souldrive_pc_mmi_real_v0.1 `
  --output eval/results/real_mmi_collection_audit.json `
  --minimum-episodes 30 `
  --require-real-capture `
  --require-reviewed
```

审计器只读检查，不生成样本、不修改媒体、不替用户决定目标；合成 seed 通过审计也不能替代真实采集。

## 安全边界

本 pilot 不需要真实邮箱、不需要真实发送、不需要云端上传。语音和截图只保存在本机 `data/local/`，该目录已被 Git 忽略。不要把真实私密材料放入评测数据。
