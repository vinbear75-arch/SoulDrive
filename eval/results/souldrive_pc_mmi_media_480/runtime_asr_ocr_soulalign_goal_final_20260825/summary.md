# 合成媒体真实 Runtime 评测

- 测试样本：`80`；完整封闭 test：`True`
- B1 原转写 intent accuracy：`0.6250`
- Candidate ASR+OCR+SoulAlign intent accuracy：`0.9875`
- Candidate fast-path（热运行）P95：`1215.641 ms`
- 冷启动首条 runtime：`14794.565 ms`
- ASR status：`{'pass': 80}`
- OCR status：`{'pass': 80}`

## 限制

- This is an actual local SenseVoice + RapidOCR + SoulAlign runtime run on synthetic TTS/UI media, not a Huawei Topic 5 competition score.
- The test split is used only once for post-hoc evaluation. Threshold 0.7 is fixed before this run from validation; no test-driven tuning is permitted.
- B1 sees the provided synthetic transcript; Candidate transcribes local WAV and reads local synthetic screenshots. The two scores do not measure a public benchmark or human interaction quality.
- VLM is sampled only on a fixed prefix when requested. Fast-path P95 and VLM-sampled P95 must be reported separately and cannot be averaged into a competition claim.
