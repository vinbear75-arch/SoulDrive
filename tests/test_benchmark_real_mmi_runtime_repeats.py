import json
import wave
from pathlib import Path

from PIL import Image

from scripts.benchmark_real_mmi_runtime_repeats import benchmark


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)


def test_repeat_benchmark_keeps_unique_count_separate(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    (collection / "media").mkdir(parents=True)
    _write_wav(collection / "media" / "a.wav")
    Image.new("RGB", (8, 8), "white").save(collection / "media" / "a.png")
    row = {
        "episode_id": "E1",
        "events": [
            {"modality": "speech_text", "payload": {"text": "总结"}},
            {"modality": "audio", "payload_ref": "media/a.wav"},
            {"modality": "screen", "payload_ref": "media/a.png"},
        ],
        "ground_truth": {"target_id": None},
    }
    (collection / "episodes.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    class FakeRuntime:
        def infer(self, **kwargs):
            return {"status": "pass", "latency_ms": {"total": 1.0}, "stages": {"asr": {"latency_ms": 0.5}}}

        def close(self):
            pass

    monkeypatch.setattr("scripts.benchmark_real_mmi_runtime_repeats.Topic5Runtime", FakeRuntime)
    report = benchmark(collection, tmp_path / "report.json", repeats=3)
    assert report["unique_episode_count"] == 1
    assert report["measurement_count"] == 3
    assert report["cold_start_measurement_count"] == 1
    assert report["warm_measurement_count"] == 2
    assert report["pipeline_coverage"]["complete_response"] == "not_integrated"


def test_repeat_benchmark_accepts_current_episode_media_schema(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    (collection / "media").mkdir(parents=True)
    _write_wav(collection / "media" / "a.wav")
    Image.new("RGB", (8, 8), "white").save(collection / "media" / "a.png")
    row = {
        "episode_id": "E2",
        "events": [
            {"modality": "speech_audio", "payload_ref": "media/a.wav"},
            {"modality": "ui_selection", "payload_ref": "media/a.png", "payload": {"target_id": "target"}},
        ],
        "ground_truth": {"target_id": "target"},
    }
    (collection / "episodes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    class FakeRuntime:
        def infer(self, **kwargs):
            assert kwargs["audio_ref"] == "media/a.wav"
            assert kwargs["image_ref"] == "media/a.png"
            return {"status": "pass", "latency_ms": {"total": 1.0}, "stages": {}}

        def close(self):
            pass

    monkeypatch.setattr("scripts.benchmark_real_mmi_runtime_repeats.Topic5Runtime", FakeRuntime)
    report = benchmark(collection, tmp_path / "report.json", repeats=2)
    assert report["measurement_count"] == 2
