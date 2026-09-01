# SoulDrive B0 Current Baseline

- Run: `b0_current_20260817`
- Status: partial (archived retrieval/ingestion evidence)
- Tests: 410 passed, 1 warning in 45.56s
- Retrieval samples: 360
- Retrieval all-hit@6: 91.7%
- Retrieval any-hit@6: 98.5%
- MRR: 0.887
- Ingestion samples: 216
- Ingestion quality: 100.0%

## Limitations

- SOULDRIVE_WORKSPACE was not configured for a live retrieval replay.
- The original 36-document evaluation corpus was not available on this machine.
- Retrieval predictions and ingestion metrics are archived evidence, not a fresh end-to-end run.
- OCR and full answer-generation baselines require separate fixed datasets.
