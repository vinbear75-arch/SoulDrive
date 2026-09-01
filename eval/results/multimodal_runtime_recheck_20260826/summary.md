# 合成媒体真实 Runtime 评测

- 测试样本：`2`；完整封闭 test：`False`
- B1 原转写 intent accuracy：`1.0000`
- Candidate ASR+OCR+SoulAlign intent accuracy：`1.0000`
- Candidate fast-path（热运行）P95：`952.410 ms`
- 冷启动首条 runtime：`40963.375 ms`
- ASR status：`{'pass': 2}`
- OCR status：`{'pass': 2}`

## 限制

- This is an actual local SenseVoice + RapidOCR + SoulAlign runtime run on synthetic TTS/UI media, not a Huawei Topic 5 competition score.
- The test split is used only once for post-hoc evaluation. Threshold 0.7 is fixed before this run from validation; no test-driven tuning is permitted.
- B1 sees the provided synthetic transcript; Candidate transcribes local WAV and reads local synthetic screenshots. The two scores do not measure a public benchmark or human interaction quality.
- VLM is sampled only on a fixed prefix when requested. Fast-path P95 and VLM-sampled P95 must be reported separately and cannot be averaged into a competition claim.
