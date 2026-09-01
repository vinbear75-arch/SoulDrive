# SoulDrive-PC-MMI 合成媒体 Harness

- 数据集：`SoulDrive-PC-MMI-media-480-v0.2`
- 划分：`{'train': 320, 'validation': 80, 'test': 80}`
- B1 test intent accuracy：`0.6250`
- C0 test action accuracy：`0.7750`
- C0 media-header P95：`0.219 ms`

## 边界

- This is a synthetic media-backed harness result, not a public benchmark or Huawei Topic 5 score.
- B1 uses the provided synthetic transcript only; the candidate reads WAV/PNG headers and structured events, but does not run ASR, OCR, VLM, QLoRA, or a learned multimodal fusion model.
- The result proves media-file integrity and target/timestamp gate behavior only. It must not be presented as real speech or visual recognition accuracy.
