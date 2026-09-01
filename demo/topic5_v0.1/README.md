# SoulDrive 赛题五演示工作区 v0.1

这是一个可脱敏、可复现的 PC 端演示包，用于展示“语音 + 当前选区/截图 + 项目知识库”的局部闭环。材料为 synthetic public demo，不含真实姓名、邮箱、密钥或未公开资料。

## 启动

```powershell
conda activate souldrive
cd D:\AI_AGENTS\SoulDrive-main\souldrive-ui
npm.cmd run tauri dev
```

首次启动在 SoulDrive 界面初始化并解锁本地工作区；不要把演示口令写入代码、日志或提交文件。

## 导入材料

从 `materials/project_souldrive_agent/` 导入 3—5 份材料（需求、架构、数据审计、基线报告），然后执行“建立索引”。`project_vision_optimizer/` 是用于验证项目隔离的第二个脱敏项目，可在需要时导入。

## 主演示问题

1. `总结我当前选中的实验结论`
2. `回答当前选区里的关键指标`
3. `从当前选区提取下一步待办`

备用：`比较当前实验和上个月的同类实验`。若历史材料不足，系统应主动说明证据不足并请求澄清，不得编造比较结论。

## 预期展示

按住麦克风说出问题，松开后主动授权当前 SoulDrive 项目窗口或选区截图。界面展示 ASR、OCR/视觉目标、意图、置信度、隐私范围和证据引用；确认后可生成回答或 PPT 草案。

## 口径

- `ingestion_metrics.json` 是产品 B1 摄取验证，不是华为内部集成绩。
- `materials/product_benchmark_manifest_v0.1.json` 记录材料清单与来源。
- 该包用于演示和回归，不替代赛题方未公开的内部测试集。
