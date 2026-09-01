# SoulDrive-PC-MMI-media-480-v0.2

这是一个用于 SoulDrive PC 多模态 harness 和 runtime 联调的**合成媒体业务集**，不是公开竞赛测试集，也不是华为内部数据。

## 内容

- 基础标签：`SoulDrive-PC-MMI-480-v0.1`，`train/validation/test = 320/80/80`。
- 每条 episode：中文转写、结构化项目/UI 事件、一个本地单声道 16 kHz WAV、一个 1440×900 合成 PC 工作台截图。
- 模板：继承基础集的唯一 `template_id`，跨划分无模板重叠（由 `manifest.json` 校验）。
- 音频后端：`synthetic_tone_fallback`。Windows SAPI 生成的是合成语音；不可用时为确定性 PCM 音调回退，必须在报告中区分。
- 截图后端：Pillow 绘制的非敏感 UI，选区坐标保存在 `screen` 事件的 `selection_bbox`。

## 运行

```powershell
conda run -n souldrive python scripts/build_souldrive_mmi_media_480.py
conda run -n souldrive python scripts/validate_souldrive_media.py `
  --dataset eval/fixtures/souldrive_pc_mmi_media_480_v0.2/episodes.jsonl `
  --media-root eval/fixtures/souldrive_pc_mmi_media_480_v0.2 `
  --require-media `
  --output eval/results/souldrive_pc_mmi_media_480/media_validation.json
conda run -n souldrive python scripts/eval_souldrive_mmi_media.py
```

## 解释边界

此集可以验证文件引用、格式门禁、事件对齐 harness、端到端接口和资源记录；它不能证明真实语音识别、真实 UI grounding、个性化效果或华为赛题得分。正式比赛结果必须使用组委会认可的公开集和用户授权的真实 PC episode，并单独报告公开基准、业务集和隐藏集。

数据哈希：`fcca876c77b16a5c35051766d2df9f9a5c31dc854aee0da2bdbc18c0725f8961`
