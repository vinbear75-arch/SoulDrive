# SoulDrive WP0 B0 Current Baseline

- Run: `wp0_current_20260822`
- Status: `partial`（归档证据 + 本地 smoke；不作为竞赛成绩）
- Unified prediction rows: `1852`
- Failures/limitations rows: `64`
- PPT smoke valid rate: `100.0%`

## Component status

- Retrieval: archived 360 samples
- Ingestion: archived 216 samples
- OCR: archived fixed-center 1,272 samples
- Runtime: fresh 1-episode smoke
- PPT: fresh 3-plan OpenXML smoke

## Limitations

- Retrieval and ingestion are archived evidence because the original live 36-document corpus is unavailable.
- OCR uses a fixed-center ScreenSpot baseline and is not a fresh full parser replay.
- Runtime has one fresh smoke episode only; it is not a cold/warm P95 measurement.
- PPT covers three deterministic OpenXML smoke plans and does not measure human edit effort.
