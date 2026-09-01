# SoulDrive-PC-MMI 真实媒体采集门禁

每条正式比赛 episode 必须由已授权的采集者在本地 PC 上生成，不能上传真实项目资料或在后台录制。

1. 先创建项目演示包，使用公开或合成资料。
2. 用户主动按住录音按钮，保存为单声道、16-bit、16kHz PCM WAV，时长不超过 15 秒。
3. 用户主动触发当前选区/窗口截取，仅保存当前授权区域的 PNG/JPEG/WebP。
4. 在 episode 的 `audio`、`screen` 或 `ocr_region` 事件写入相对 `payload_ref`；不得使用绝对路径、URL 或上级目录。
5. 审阅 intent、target、aligned_event_ids、冲突与动作后再冻结 split。

检查命令：

```powershell
conda run -n souldrive python scripts/validate_souldrive_media.py `
  --dataset <episodes.jsonl> `
  --media-root <媒体根目录> `
  --require-media `
  --output <媒体校验报告.json>
```

校验失败不能进入训练或作为比赛数据。该门禁会拒绝网络 URL、绝对路径、路径穿越、非 16kHz WAV、过长音频和损坏/过大的截图。
