import json

import pytest

from scripts.eval_real_mmi_pilot import _captured_active_context, _select_rows, build_stage_ledger, evaluate, summarize_stage_coverage
from scripts.init_real_mmi_collection import initialize


def test_pilot_evaluator_empty_collection(tmp_path):
    initialize(tmp_path)
    result = evaluate(tmp_path, tmp_path / "out")
    assert result["sample_count"] == 0
    assert (tmp_path / "out" / "metrics.json").is_file()
    assert (tmp_path / "out" / "stage_ledger.jsonl").is_file()
    assert json.loads((tmp_path / "out" / "perception_warmup.json").read_text(encoding="utf-8"))["requested"] is False


def test_pilot_episode_selection_is_exact_and_fail_closed():
    rows = [{"episode_id": "REAL-01"}, {"episode_id": "REAL-02"}]

    assert _select_rows(rows, ["REAL-02"])[0]["episode_id"] == "REAL-02"
    assert [row["episode_id"] for row in _select_rows(rows, ["REAL-02", "REAL-01"])] == ["REAL-01", "REAL-02"]
    with pytest.raises(ValueError, match="REAL-03"):
        _select_rows(rows, ["REAL-03"])


def test_pilot_context_does_not_promote_selection_label_to_selected_text():
    row = {
        "project_id": "demo_project",
        "events": [{
            "event_id": "selection",
            "modality": "ui_selection",
            "payload": {"text": "结果与结论段落", "target_id": "selected"},
        }],
    }

    assert _captured_active_context(row) == {"project_id": "demo_project"}
    row["events"][0]["payload"]["selected_text"] = "本轮准确率为 85%。"
    assert _captured_active_context(row)["selected_text"] == "本轮准确率为 85%。"


def test_pilot_evaluator_records_explicit_perception_warmup_without_mixing_latency(tmp_path, monkeypatch):
    initialize(tmp_path)

    class FakeRuntime:
        def warmup_perception(self):
            return {
                "schema_version": "souldrive_topic5_runtime_perception_warmup_v0.1",
                "status": "pass",
                "stages": {},
            }

        def close(self):
            return None

    monkeypatch.setattr("scripts.eval_real_mmi_pilot.Topic5Runtime", FakeRuntime)
    result = evaluate(tmp_path, tmp_path / "out", warmup_perception=True)

    assert result["perception_warmup"] == {
        "requested": True,
        "status": "pass",
        "wall_clock_latency_ms": result["perception_warmup"]["wall_clock_latency_ms"],
    }
    assert result["perception_warmup"]["wall_clock_latency_ms"] >= 0
    assert result["cold_start_ms"] == 0.0
    saved = json.loads((tmp_path / "out" / "perception_warmup.json").read_text(encoding="utf-8"))
    assert saved["result"]["status"] == "pass"


def test_stage_ledger_distinguishes_unintegrated_stages_from_zero_latency():
    ledger = build_stage_ledger(
        "REAL-01",
        {
            "status": "pass",
            "stages": {
                "asr": {"status": "pass", "latency_ms": 12.5},
                "ocr": {"status": "skipped", "latency_ms": 0.0, "reason": "no_image"},
            },
        },
        15.0,
    )

    assert ledger["stages"]["asr"] == {"status": "pass", "latency_ms": 12.5}
    assert ledger["stages"]["ocr"]["status"] == "skipped"
    assert ledger["stages"]["ocr"]["latency_ms"] is None
    assert ledger["stages"]["retrieval"] == {
        "status": "not_integrated",
        "latency_ms": None,
        "reason": "pilot evaluator currently measures perception/alignment only",
    }
    coverage = summarize_stage_coverage([ledger])
    assert coverage["asr"]["latency_count"] == 1
    assert coverage["ocr"]["latency_count"] == 0
    assert coverage["retrieval"]["latency_count"] == 0


def test_pilot_evaluator_can_measure_real_asr_context_through_rag(tmp_path, monkeypatch):
    initialize(tmp_path)
    (tmp_path / "episodes.jsonl").write_text(json.dumps({
        "episode_id": "REAL-01",
        "project_id": "demo_project",
        "events": [
            {"event_id": "audio", "modality": "audio", "payload_ref": "media/audio/a.wav"},
            {"event_id": "screen", "modality": "screen", "payload_ref": "media/screens/a.png", "payload": {"window_title": "实验.txt - 记事本"}},
            {"event_id": "selection", "modality": "ui_selection", "payload": {"text": "结果段落", "selected_text": "结果段落", "target_id": "selected"}},
        ],
        "ground_truth": {
            "intent": "private_qa",
            "target_id": "selected",
            "expected_action": "respond",
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    rag_workspace = tmp_path / "rag-workspace"
    (rag_workspace / "index").mkdir(parents=True)
    (rag_workspace / "index" / "keyword_index.sqlite").touch()
    (rag_workspace / "index" / "parent_docs.sqlite").touch()

    class FakeRuntime:
        def infer(self, **_kwargs):
            return {
                "status": "pass",
                "speech_text": "回答当前选区里的关键指标",
                "perception_events": [
                    {"event_id": "image", "modality": "image", "timestamp_ms": 0, "text": "指标为 0.85", "confidence": 0.9},
                    {"event_id": "selection", "modality": "ui_selection", "timestamp_ms": 0, "text": "结果段落", "project_id": "demo_project"},
                ],
                "decision": {"clarification_required": False},
                "stages": {
                    "asr": {"status": "pass", "latency_ms": 1},
                    "ocr": {"status": "pass", "latency_ms": 2},
                    "vlm": {"status": "skipped", "latency_ms": 0},
                    "soulalign": {"status": "pass", "latency_ms": 3},
                },
            }

        def close(self):
            return None

    class FakeKb:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            return None

    class FakeRag:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def generate_response_stream(self, query, **kwargs):
            assert query == "回答当前选区里的关键指标"
            context = kwargs["interaction_context"]
            assert context["active_context"]["project_id"] == "demo_project"
            assert context["active_context"]["selected_text"] == "结果段落"
            kwargs["on_retrieval_completed"]({"latency_ms": 4, "evidence_count": 1})
            kwargs["on_generation_chunk"]("结论")
            yield "结论 [E1]"
            yield "\n\n```souldrive-evidence\n[{\"id\": \"E1\", \"snippet\": \"结论\"}]\n```"

        def close(self):
            return None

    monkeypatch.setattr("scripts.eval_real_mmi_pilot.Topic5Runtime", FakeRuntime)
    monkeypatch.setattr("scripts.eval_real_mmi_pilot.LocalKnowledgeBase", FakeKb)
    monkeypatch.setattr("scripts.eval_real_mmi_pilot.RAGEngine", FakeRag)

    result = evaluate(
        tmp_path,
        tmp_path / "out",
        rag_workspace=rag_workspace,
        rag_rerank_candidate_limit=24,
    )

    record = json.loads((tmp_path / "out" / "predictions.jsonl").read_text(encoding="utf-8"))
    assert result["full_chain_coverage"] is True
    assert result["full_chain_coverage_detail"] == {
        "enabled": True,
        "run_vlm": False,
        "rerank_candidate_limit": 24,
        "eligible_count": 1,
        "complete_count": 1,
        "citation_support_applicable_count": 1,
        "citation_support_valid_count": 1,
        "citation_support_invalid_count": 0,
        "citation_support_unsupported_claim_count": 0,
    }
    assert record["rag"]["query"] == "回答当前选区里的关键指标"
    assert record["rag"]["response_has_citation"] is True
    assert record["rag"]["citation_support_valid"] is True
    assert record["stage_ledger"]["measurement_scope"] == "perception_alignment_and_rag"
    assert record["stage_ledger"]["full_chain_complete"] is True


def test_pilot_evaluator_forwards_explicit_vlm_slow_path(tmp_path, monkeypatch):
    initialize(tmp_path)
    (tmp_path / "episodes.jsonl").write_text(json.dumps({
        "episode_id": "REAL-01",
        "project_id": "demo_project",
        "events": [
            {"event_id": "audio", "modality": "audio", "payload_ref": "media/audio/a.wav"},
            {"event_id": "screen", "modality": "screen", "payload_ref": "media/screens/a.png"},
        ],
        "ground_truth": {
            "intent": "private_qa",
            "target_id": None,
            "expected_action": "respond",
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    calls = []

    class FakeRuntime:
        def infer(self, **kwargs):
            calls.append(kwargs)
            return {
                "status": "pass",
                "speech_text": "查询",
                "perception_events": [],
                "decision": {"intent": "private_qa", "action": "respond", "clarification_required": False},
                "stages": {
                    "asr": {"status": "pass", "latency_ms": 1},
                    "ocr": {"status": "pass", "latency_ms": 2},
                    "vlm": {"status": "pass", "latency_ms": 3},
                    "soulalign": {"status": "pass", "latency_ms": 4},
                },
            }

        def close(self):
            return None

    monkeypatch.setattr("scripts.eval_real_mmi_pilot.Topic5Runtime", FakeRuntime)
    result = evaluate(tmp_path, tmp_path / "out", run_vlm=True)

    assert calls[0]["run_vlm"] is True
    assert calls[0]["visual_mode"] == "slow"
    assert calls[0]["max_new_tokens"] == 48
    assert result["visual_mode"] == "slow"


def test_pilot_full_chain_rejects_citation_id_without_content_support():
    from scripts.eval_real_mmi_pilot import _run_rag_episode

    class FakeRag:
        def generate_response_stream(self, **kwargs):
            kwargs["on_retrieval_completed"]({"latency_ms": 4, "evidence_count": 1})
            kwargs["on_generation_chunk"]("不相关结论")
            yield "不相关结论 [E1]"
            yield "\n\n```souldrive-evidence\n[{\"id\": \"E1\", \"snippet\": \"系统采用本地加密存储。\"}]\n```"

    result = _run_rag_episode(
        FakeRag(),
        {"project_id": "demo_project"},
        {"speech_text": "查询", "decision": {}, "perception_events": []},
        top_k=6,
    )

    assert result["response_has_citation"] is True
    assert result["citation_support_valid"] is False
    assert result["full_chain_complete"] is False


def test_pilot_accepts_cited_visual_observation_without_calling_it_document_retrieval():
    from scripts.eval_real_mmi_pilot import _run_rag_episode

    class FakeRag:
        def generate_response_stream(self, **kwargs):
            kwargs["on_retrieval_completed"]({
                "latency_ms": 4,
                "evidence_count": 0,
                "status": "not_applicable",
                "route": "visual_observation_only",
            })
            kwargs["on_generation_chunk"]("当前选区显示 85%。")
            yield "当前选区显示 85%。[V1]"
            yield "\n\n```souldrive-evidence\n[{\"id\": \"V1\", \"snippet\": \"当前选区显示 85%。\", \"modality\": \"image\"}]\n```"

    result = _run_rag_episode(
        FakeRag(),
        {"project_id": "demo_project"},
        {"speech_text": "截图里显示了什么？", "decision": {}, "perception_events": []},
        top_k=6,
    )

    assert result["retrieval_route"] == "visual_observation_only"
    assert result["stages"]["retrieval"]["status"] == "not_applicable"
    assert result["citation_support_scope"] == "visual_observation"
    assert result["citation_support_valid"] is True
    assert result["stages"]["complete_answer"]["status"] == "pass"
    assert result["status"] == "pass"
    assert result["full_chain_complete"] is False
