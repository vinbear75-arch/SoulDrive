import json
import wave
from pathlib import Path

from PIL import Image

from scripts.benchmark_business_runtime_repeats import benchmark


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)


def test_business_benchmark_records_all_runtime_stages(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    (collection / "media").mkdir(parents=True)
    _write_wav(collection / "media" / "a.wav")
    Image.new("RGB", (8, 8), "white").save(collection / "media" / "a.png")
    (collection / "episodes.jsonl").write_text(json.dumps({
        "episode_id": "E1", "project_id": "project_souldrive_agent", "events": [
            {"modality": "speech_audio", "payload_ref": "media/a.wav"},
            {"modality": "ui_selection", "payload_ref": "media/a.png"},
        ], "ground_truth": {"target_id": "target"},
    }) + "\n", encoding="utf-8")

    class FakeWorkspace:
        root_path = str(tmp_path / "workspace")
        graph_db_path = str(tmp_path / "workspace" / "index" / "graph.sqlite")

        def ensure(self):
            Path(self.root_path).mkdir(parents=True, exist_ok=True)
            return self

    class FakeRuntime:
        def infer(self, **kwargs):
            assert kwargs["audio_ref"].startswith("runtime/benchmark_media/")
            return {"status": "pass", "speech_text": "示例", "perception_events": [], "stages": {"asr": {"latency_ms": 1}, "ocr": {"latency_ms": 2}, "soulalign": {"latency_ms": 3}}}

        def close(self):
            pass

    class FakeKb:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    class FakeRag:
        evidence_count = 1

        def __init__(self, **kwargs):
            pass

        def generate_response_stream(self, query, **kwargs):
            kwargs["on_retrieval_completed"]({"latency_ms": 4, "evidence_count": self.evidence_count})
            kwargs["on_generation_chunk"]("答")
            yield "答案 [E1]"

        def close(self):
            pass

    monkeypatch.setattr("scripts.benchmark_business_runtime_repeats.SoulDriveWorkspace", lambda _: FakeWorkspace())
    monkeypatch.setattr("scripts.benchmark_business_runtime_repeats.Topic5Runtime", FakeRuntime)
    monkeypatch.setattr("scripts.benchmark_business_runtime_repeats.LocalKnowledgeBase", FakeKb)
    monkeypatch.setattr("scripts.benchmark_business_runtime_repeats.RAGEngine", FakeRag)
    report = benchmark(collection, tmp_path / "workspace", tmp_path / "report.json", 3, "项目指标是多少？")

    assert report["measurement_count"] == 3
    assert report["warm_measurement_count"] == 2
    assert report["pipeline_coverage"]["complete_response"] == "measured"
    assert report["records"][0]["query_source"] == "frozen_structured_business_query"
    assert report["records"][0]["stage_latency_ms"]["retrieval_and_context_gate"] == 4
    assert report["warm_stage_latency_ms"]["model_first_token"]["count"] == 2
    assert report["records"][0]["response_has_citation"] is True
    assert report["records"][0]["evidence_backed_response"] is True
    assert "process_memory_mib" in report["records"][0]["resources"]
    assert "llama_cpp" in report["environment"]

    FakeRag.evidence_count = 0
    partial = benchmark(collection, tmp_path / "workspace", tmp_path / "partial-report.json", 1, "项目指标是多少？")
    assert partial["records"][0]["status"] == "partial"
    assert partial["records"][0]["evidence_backed_response"] is False
