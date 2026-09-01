# SoulDrive WP0 B0 Current Baseline

- Run: `wp0_current_20260822_r3`
- Status: `partial`（归档证据 + 本地 smoke；不作为竞赛成绩）
- Unified prediction rows: `1948`
- Failures/limitations rows: `86`
- PPT smoke valid rate: `100.0%`

## Component status

- Retrieval: archived 360 samples
- Ingestion: archived 216 samples
- Product retrieval/ingestion: fresh 30-asset local corpus replay
- OCR: archived fixed-center 1,272 samples
- Runtime: fresh 1-episode smoke plus 33-request local media fixture repeat
- Image OCR: fresh 3-image product smoke
- PPT: fresh 3-plan OpenXML smoke

## Limitations

- Retrieval and ingestion are archived evidence because the original live 36-document corpus is unavailable.
- OCR uses a fixed-center ScreenSpot baseline and is not a fresh full parser replay.
- Runtime repeat evidence covers local ASR+OCR+SoulAlign only; retrieval, first token, and complete answer remain unintegrated.
- Fresh OCR smoke has three fixed product images and no bbox localization labels; it is not the required larger annotated OCR evaluation set.
- PPT covers three deterministic OpenXML smoke plans and does not measure human edit effort.
