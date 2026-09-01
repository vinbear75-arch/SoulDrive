from pathlib import Path
from io import BytesIO
import json
import tempfile
from unittest.mock import patch
import wave

from PIL import Image
from fastapi.testclient import TestClient

from core.topic5_runtime import (
    DEFAULT_MAX_NEW_TOKENS,
    MIN_GROUNDING_VLM_NEW_TOKENS,
    MIN_SLOW_VLM_NEW_TOKENS,
    RuntimeStageBlocked,
    Topic5Runtime,
    _vlm_processor_pixel_budget,
    parse_grounding_candidates,
)
from core import mcp_server
from core.enterprise_security import SlidingWindowRateLimiter
from core.workspace import SoulDriveWorkspace
from core.workspace import SoulDriveWorkspace
import core.runtime_state as runtime_state


def _wav(path: Path):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)


def test_runtime_rejects_network_media_reference(tmp_path):
    result = Topic5Runtime().infer(workspace_root=tmp_path, speech_text="总结", image_ref="https://example.test/a.png")

    assert result["status"] == "invalid"
    assert result["diagnostics"][0]["code"] == "invalid_media"


def test_runtime_reports_missing_audio_without_fake_transcript(tmp_path):
    result = Topic5Runtime().infer(workspace_root=tmp_path)

    assert result["status"] == "blocked"
    assert result["speech_text"] == ""
    assert result["stages"]["asr"]["code"] == "missing_audio_or_speech"


def test_runtime_accepts_text_only_and_keeps_soulalign_side_effect_free(tmp_path):
    result = Topic5Runtime().infer(workspace_root=tmp_path, speech_text="总结当前选区", run_ocr=False, run_vlm=False)

    assert result["status"] == "pass"
    assert result["stages"]["asr"]["status"] == "skipped"
    assert result["decision"]["intent"] == "selection_summary"
    assert result["decision"]["side_effect"] == "none"


def test_runtime_preserves_structured_ui_evidence_for_follow_up_chat(tmp_path):
    result = Topic5Runtime().infer(
        workspace_root=tmp_path,
        speech_text="总结当前指标",
        events=[{
            "event_id": "uia-1",
            "modality": "ui_automation",
            "start_ms": 1000,
            "confidence": 0.96,
            "source": "windows_uia",
            "text": "指标A",
            "selected_text": "本轮准确率为 85%。",
            "window_title": "实验面板",
            "explicit": True,
            "focus_point": {"x": 0.42, "y": 0.37},
            "payload": {
                "target_candidates": [{
                    "target_id": "metric-a",
                    "target_kind": "text",
                    "source": "windows_uia",
                    "confidence": 0.96,
                    "bbox": {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.3},
                }],
            },
        }],
        run_ocr=False,
        run_vlm=False,
    )

    event = next(item for item in result["perception_events"] if item["event_id"] == "uia-1")
    assert event["source"] == "windows_uia"
    assert event["text"] == "指标A"
    assert event["selected_text"] == "本轮准确率为 85%。"
    assert event["window_title"] == "实验面板"
    assert event["explicit"] is True
    assert event["focus_point"] == {"x": 0.42, "y": 0.37}
    assert event["target_candidates"][0]["target_id"] == "metric-a"
    assert event["target_candidates"][0]["source"] == "windows_uia"


def test_target_grounding_can_use_focused_ui_automation_without_vlm(tmp_path):
    result = Topic5Runtime().infer(
        workspace_root=tmp_path,
        speech_text="总结当前控件",
        events=[{
            "event_id": "uia-focused",
            "modality": "ui_automation",
            "start_ms": 0,
            "end_ms": 0,
            "confidence": 0.99,
            "source": "windows_uia",
            "is_focused": True,
            "explicit": True,
            "target_candidates": [{
                "target_id": "button-save",
                "target_kind": "button",
                "source": "windows_uia",
                "confidence": 0.99,
                "is_focused": True,
                "explicit": True,
            }],
        }],
        run_ocr=False,
        run_vlm=False,
        target_grounding=True,
    )

    assert result["status"] == "pass"
    assert result["decision"]["target_id"] == "button-save"
    assert result["stages"]["visual_route"]["route"] == "none"
    assert result["stages"]["vlm"]["status"] == "skipped"


def test_target_grounding_without_ui_automation_still_requires_vlm_permission(tmp_path):
    result = Topic5Runtime().infer(
        workspace_root=tmp_path,
        speech_text="总结当前控件",
        run_ocr=False,
        run_vlm=False,
        target_grounding=True,
    )

    assert result["status"] == "invalid"
    assert result["diagnostics"][0]["code"] == "target_grounding_requires_vlm_permission"


def test_runtime_accepts_image_only_as_partial_perception(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "实验报告", "confidence": 0.96}]):
        result = runtime.infer(workspace_root=tmp_path, image_ref="screen.png", visual_mode="fast")

    assert result["status"] == "partial"
    assert result["speech_text"] == ""
    assert result["decision"] is None
    assert result["stages"]["asr"]["code"] == "missing_audio_or_speech"
    assert result["perception_events"][0]["text"] == "实验报告"


def test_grounding_parser_requires_normalized_candidate_boxes():
    parsed = parse_grounding_candidates(
        "```json\n"
        "[{\"target_id\": \"chart-a\", \"target_kind\": \"chart\", "
        "\"text\": \"准确率趋势\", \"confidence\": 0.91, "
        "\"bbox\": {\"x\": 0.1, \"y\": 0.2, \"width\": 0.5, \"height\": 0.4}}, "
        "{\"target_id\": \"bad\", \"confidence\": 0.99, "
        "\"bbox\": {\"x\": 0.8, \"y\": 0.8, \"width\": 0.4, \"height\": 0.2}}]\n"
        "```"
    )

    assert len(parsed) == 1
    assert parsed[0]["target_id"] == "chart-a"
    assert parsed[0]["source"] == "vlm"
    assert parsed[0]["bbox"] == {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.4}
    assert parse_grounding_candidates("图中有一个按钮，但没有结构化坐标") == []


def test_vlm_cache_returns_same_authorized_capture_without_loading_model(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    runtime = Topic5Runtime()
    key = runtime._vlm_cache_key(image_path, "关注按钮", 24)
    second_key = runtime._vlm_cache_key(image_path, "关注图表", 24)
    runtime._vlm_cache[key] = "缓存的视觉结果"
    runtime._vlm_cache[second_key] = "另一个视觉结果"
    with patch.object(runtime, "_load_vlm") as load_vlm:
        result = runtime._understand_image(image_path, "关注按钮", 24)

    assert result == "缓存的视觉结果"
    assert runtime._last_vlm_cache_hit is True
    assert list(runtime._vlm_cache) == [second_key, key]
    load_vlm.assert_not_called()


def test_vlm_warmup_loads_once_and_reports_explicit_local_mode():
    runtime = Topic5Runtime()
    with patch.object(runtime, "_load_vlm", return_value=("model", "processor")) as load_vlm:
        first = runtime.warmup_vlm()
        second = runtime.warmup_vlm()

    assert first["status"] == "pass"
    assert first["already_loaded"] is False
    assert second["already_loaded"] is True
    assert second["policy"] == {"side_effect": "none", "mode": "explicit_local_warmup"}
    load_vlm.assert_called_once()


def test_vlm_processor_pixel_budget_keeps_default_and_bounds_experimental_values(monkeypatch):
    monkeypatch.delenv("SOULDRIVE_VLM_MIN_IMAGE_TOKENS", raising=False)
    monkeypatch.delenv("SOULDRIVE_VLM_MAX_IMAGE_TOKENS", raising=False)
    assert _vlm_processor_pixel_budget() == {
        "min_pixels": 256 * 28 * 28,
        "max_pixels": 768 * 28 * 28,
        "min_image_tokens": 256,
        "max_image_tokens": 768,
    }

    monkeypatch.setenv("SOULDRIVE_VLM_MIN_IMAGE_TOKENS", "64")
    monkeypatch.setenv("SOULDRIVE_VLM_MAX_IMAGE_TOKENS", "384")
    assert _vlm_processor_pixel_budget() == {
        "min_pixels": 128 * 28 * 28,
        "max_pixels": 384 * 28 * 28,
        "min_image_tokens": 128,
        "max_image_tokens": 384,
    }

    monkeypatch.setenv("SOULDRIVE_VLM_MIN_IMAGE_TOKENS", "900")
    monkeypatch.setenv("SOULDRIVE_VLM_MAX_IMAGE_TOKENS", "256")
    budget = _vlm_processor_pixel_budget()
    assert budget["min_image_tokens"] == 900
    assert budget["max_image_tokens"] == 900


def test_vlm_warmup_redacts_blocked_model_details():
    runtime = Topic5Runtime()
    with patch.object(
        runtime,
        "_load_vlm",
        side_effect=RuntimeStageBlocked("model_missing", r"D:\private\models\qwen"),
    ):
        result = runtime.warmup_vlm()

    assert result["status"] == "blocked"
    assert result["code"] == "model_missing"
    assert "private" not in result["detail"]
    assert "[local path]" in result["detail"]


def test_vlm_release_clears_model_but_keeps_bounded_text_cache():
    runtime = Topic5Runtime()
    runtime._vlm = ("model", "processor")
    runtime._vlm_cache["capture:prompt:24"] = "已缓存的视觉摘要"

    first = runtime.release_vlm()
    second = runtime.release_vlm()

    assert first["released"] is True
    assert second["released"] is False
    assert runtime._vlm is None
    assert runtime._vlm_cache["capture:prompt:24"] == "已缓存的视觉摘要"


def test_perception_warmup_loads_asr_and_ocr_once():
    runtime = Topic5Runtime()
    with patch.object(runtime, "_load_asr", return_value=object()) as load_asr, patch.object(
        runtime, "_load_ocr", return_value=object()
    ) as load_ocr:
        first = runtime.warmup_perception()
        second = runtime.warmup_perception()

    assert first["status"] == "pass"
    assert first["loaded"] == {"asr": True, "ocr": True}
    assert first["already_loaded"] is False
    assert second["already_loaded"] is True
    assert second["policy"] == {"side_effect": "none", "mode": "explicit_local_perception_warmup"}
    assert all(stage["latency_ms"] is not None for stage in first["stages"].values())
    load_asr.assert_called_once()
    load_ocr.assert_called_once()


def test_perception_warmup_reports_partial_block_without_fake_loading():
    runtime = Topic5Runtime()
    with patch.object(runtime, "_load_asr", side_effect=RuntimeStageBlocked("model_missing", r"D:\private\asr")), patch.object(
        runtime, "_load_ocr", return_value=object()
    ):
        result = runtime.warmup_perception()

    assert result["status"] == "blocked"
    assert result["loaded"] == {"asr": False, "ocr": True}
    assert result["stages"]["asr"]["code"] == "model_missing"
    assert "private" not in result["stages"]["asr"]["detail"]
    assert "[local path]" in result["stages"]["asr"]["detail"]


def test_slow_visual_route_uses_a_bounded_description_budget(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[]), patch.object(
        runtime, "_understand_image", return_value="选区显示实验结果。"
    ) as understand:
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="解释这张图",
            image_ref="screen.png",
            run_vlm=True,
            visual_mode="slow",
            max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        )

    assert result["stages"]["vlm"]["requested_max_new_tokens"] == DEFAULT_MAX_NEW_TOKENS
    assert result["stages"]["vlm"]["max_new_tokens"] == MIN_SLOW_VLM_NEW_TOKENS
    assert understand.call_args.args[2] == MIN_SLOW_VLM_NEW_TOKENS


def test_grounding_route_gets_enough_budget_for_structured_candidates(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[]), patch.object(
        runtime,
        "_understand_image",
        return_value='[{"target_kind":"button","text":"运行","confidence":0.95,"bbox":{"x":0.1,"y":0.1,"width":0.4,"height":0.3}}]',
    ) as understand:
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="定位当前按钮",
            image_ref="screen.png",
            selected_region={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            run_vlm=True,
            target_grounding=True,
            visual_mode="slow",
            max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        )

    assert result["stages"]["vlm"]["max_new_tokens"] == MIN_GROUNDING_VLM_NEW_TOKENS
    assert understand.call_args.args[2] == MIN_GROUNDING_VLM_NEW_TOKENS


def test_ocr_cache_reuses_same_image_and_returns_isolated_regions(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    same_content_path = tmp_path / "renamed-screen.png"
    same_content_path.write_bytes(image_path.read_bytes())
    runtime = Topic5Runtime()
    calls = []

    def fake_ocr(_path):
        calls.append(_path)
        return (
            [[[0, 0], [10, 0], [10, 10], [0, 10]]],
            ["缓存文字"],
            [0.95],
        )

    with patch.object(runtime, "_ocr", fake_ocr):
        first = runtime._recognize(image_path)
        second = runtime._recognize(same_content_path)
        second[0]["text"] = "调用方修改"
        second[0]["bbox"]["x"] = 0.99
        third = runtime._recognize(image_path)

    assert len(calls) == 1
    assert first[0]["text"] == "缓存文字"
    assert third[0]["text"] == "缓存文字"
    assert third[0]["bbox"]["x"] == 0.0
    assert runtime._last_ocr_cache_hit is True


def test_target_grounding_uses_authorized_crop_and_verifier(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[]), patch.object(
        runtime,
        "_understand_image",
        return_value='[{"target_kind":"button","text":"运行","confidence":0.95,"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.25}}]',
    ) as understand:
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="总结当前选区",
            image_ref="screen.png",
            selected_region={"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.5},
            project_id="project-a",
            run_vlm=True,
            target_grounding=True,
            visual_mode="fast",
        )

    assert result["status"] == "pass"
    assert result["stages"]["visual_route"]["reason"] == "grounding_requested"
    assert result["target_grounding"] is True
    assert result["stages"]["vlm"]["grounding_candidate_count"] == 1
    assert result["decision"]["target_id"] == "vlm-region-1"
    assert result["decision"]["target_verification"]["selected_target_id"] == "vlm-region-1"
    assert result["perception_events"][0]["target_candidates"][0]["bbox"] == {
        "x": 0.3,
        "y": 0.3,
        "width": 0.15,
        "height": 0.125,
    }
    understand.assert_called_once()


def test_target_grounding_rejects_unscoped_vlm_request(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)

    result = Topic5Runtime().infer(
        workspace_root=tmp_path,
        speech_text="总结当前选区",
        image_ref="screen.png",
        run_vlm=True,
        target_grounding=True,
    )

    assert result["status"] == "invalid"
    assert result["diagnostics"][0]["code"] == "target_grounding_requires_selected_region"


def test_target_grounding_clarifies_when_vlm_returns_no_candidate(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[]), patch.object(
        runtime, "_understand_image", return_value="图中有一个按钮，但没有结构化坐标"
    ):
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="总结当前选区",
            image_ref="screen.png",
            selected_region={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            run_vlm=True,
            target_grounding=True,
            visual_mode="auto",
        )

    assert result["status"] == "pass"
    assert result["decision"]["action"] == "clarify"
    assert "target_candidate_missing" in result["decision"]["clarification"]["reason_codes"]


def test_selected_region_content_route_keeps_vlm_description_when_ocr_is_empty(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[]), patch.object(
        runtime, "_understand_image", return_value="这段文字说明系统会优先处理当前选中的内容。"
    ):
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="这是在说什么",
            image_ref="screen.png",
            selected_region={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            run_ocr=True,
            run_vlm=True,
            target_grounding=False,
            visual_mode="slow",
        )

    assert result["status"] == "pass"
    assert result["target_grounding"] is False
    assert result["stages"]["ocr"]["status"] == "pass"
    assert result["stages"]["vlm"]["status"] == "pass"
    assert any(
        event["modality"] == "image" and "优先处理当前选中的内容" in event["text"]
        for event in result["perception_events"]
    )


def test_runtime_crops_selected_region_and_maps_ocr_coordinates_back_to_source(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    seen_sizes = []

    def recognize(path):
        with Image.open(path) as image:
            seen_sizes.append(image.size)
        return [{
            "text": "实验结论",
            "confidence": 0.96,
            "bbox": {"x": 0.5, "y": 0.25, "width": 0.25, "height": 0.5},
        }]

    with patch.object(runtime, "_recognize", side_effect=recognize):
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="总结当前选区",
            image_ref="screen.png",
            selected_region={"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.5},
            visual_mode="fast",
        )

    assert result["status"] == "pass"
    assert seen_sizes == [(300, 150)]
    assert result["visual_context"]["input_scope"] == "selected_region"
    assert result["visual_context"]["selected_region"] == {"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.5}
    assert result["stages"]["ocr"]["regions"][0]["bbox"] == {
        "x": 0.5,
        "y": 0.325,
        "width": 0.125,
        "height": 0.25,
    }
    assert result["perception_events"][0]["bbox"] == result["visual_context"]["selected_region"]


def test_runtime_rejects_selected_region_outside_image(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize") as recognize:
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="解释这个区域",
            image_ref="screen.png",
            selected_region={"x": 0.8, "y": 0.8, "width": 0.4, "height": 0.2},
        )

    assert result["status"] == "invalid"
    assert result["diagnostics"][0]["code"] == "invalid_selected_region"
    recognize.assert_not_called()


def test_runtime_normalizes_rapidocr_tuple_result(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    fake_result = (
        [[[0, 0], [10, 0], [10, 10], [0, 10]]],
        ["项目标题"],
        [0.93],
    )

    fake_ocr = lambda _path: fake_result
    with patch.object(runtime, "_ocr", fake_ocr):
        regions = runtime._recognize(image_path)

    assert regions[0]["text"] == "项目标题"
    assert regions[0]["confidence"] == 0.93


def test_auto_visual_route_keeps_high_confidence_text_on_fast_path(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "实验报告", "confidence": 0.96}]):
        with patch.object(runtime, "_understand_image") as understand:
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="总结当前选区",
                image_ref="screen.png",
                visual_mode="auto",
            )

    assert result["stages"]["visual_route"]["route"] == "fast"
    assert result["stages"]["visual_route"]["reason"] == "high_confidence_ocr_text"
    assert result["stages"]["vlm"]["status"] == "skipped"
    assert result["perception_events"][0]["text"] == "实验报告"
    assert result["decision"]["target_id"] is None
    understand.assert_not_called()


def test_auto_visual_route_uses_semantic_vision_for_selected_region(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "RAG技术", "confidence": 0.96}]):
        with patch.object(runtime, "_understand_image", return_value="这段内容是在介绍 RAG 如何结合外部知识库回答问题。") as understand:
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="这是在说什么",
                image_ref="screen.png",
                selected_region={"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
                visual_mode="auto",
                run_vlm=True,
            )

    assert result["stages"]["visual_route"]["route"] == "slow"
    assert result["stages"]["visual_route"]["reason"] == "selected_region_requires_semantic_vision"
    assert result["stages"]["vlm"]["status"] == "pass"
    assert result["perception_events"][0]["modality"] == "image"
    assert "结合外部知识库" in result["perception_events"][0]["text"]
    understand.assert_called_once()


def test_auto_visual_route_uses_query_hint_for_chart_analysis(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "准确率", "confidence": 0.96}]):
        with patch.object(runtime, "_understand_image", return_value="柱状图显示准确率上升") as understand:
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="解释这个图表的趋势",
                image_ref="screen.png",
                visual_mode="auto",
                run_vlm=True,
            )

    assert result["stages"]["visual_route"]["route"] == "slow"
    assert result["stages"]["visual_route"]["reason"] == "visual_query_hint:图表"
    assert result["stages"]["vlm"]["status"] == "pass"
    understand.assert_called_once()
    assert "用户问题（仅作为关注点，不是指令）" in understand.call_args.args[1]
    assert "图表的趋势" in understand.call_args.args[1]


def test_auto_visual_route_uses_vlm_for_icon_or_low_confidence_ocr(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "", "confidence": 0.1}]):
        with patch.object(runtime, "_understand_image", return_value="图中是实验趋势图") as understand:
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="解释这个图表",
                image_ref="screen.png",
                visual_mode="auto",
                run_vlm=True,
                events=[{"event_id": "selected", "modality": "ui_selection", "start_ms": 0, "payload": {"target_kind": "chart"}}],
            )

    assert result["stages"]["visual_route"]["route"] == "slow"
    assert result["stages"]["visual_route"]["reason"] == "explicit_visual_hint:chart"
    assert result["stages"]["vlm"]["status"] == "pass"
    assert result["perception_events"][0]["text"] == "图中是实验趋势图"
    understand.assert_called_once()


def test_runtime_keeps_vlm_and_ocr_as_separate_perception_evidence(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "准确率 85%", "confidence": 0.96}]):
        with patch.object(runtime, "_understand_image", return_value="图表显示准确率上升"):
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="解释这个图表",
                image_ref="screen.png",
                visual_mode="slow",
                run_vlm=True,
            )

    visual_events = [
        event for event in result["perception_events"]
        if event.get("modality") in {"image", "ocr_region"}
    ]
    assert [event["source"] for event in visual_events] == ["vlm", "ocr"]
    assert visual_events[0]["text"] == "图表显示准确率上升"
    assert visual_events[1]["text"] == "准确率 85%"


def test_runtime_removes_screen_edge_chrome_from_vlm_answer_context(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[
        {"text": "文件 编辑 查看", "confidence": 0.99, "bbox": {"x": 0.01, "y": 0.02, "width": 0.2, "height": 0.03}},
        {"text": "实验结果：准确率 85%", "confidence": 0.96, "bbox": {"x": 0.1, "y": 0.3, "width": 0.5, "height": 0.05}},
        {"text": "UTF-8", "confidence": 0.99, "bbox": {"x": 0.8, "y": 0.97, "width": 0.1, "height": 0.02}},
    ]):
        with patch.object(runtime, "_understand_image", return_value="实验结果准确率上升"):
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="总结当前实验结果",
                image_ref="screen.png",
                visual_mode="slow",
                run_vlm=True,
            )

    ocr_event = next(event for event in result["perception_events"] if event["source"] == "ocr")
    assert ocr_event["text"] == "实验结果：准确率 85%"


def test_runtime_uses_supplied_transcript_without_loading_asr(tmp_path):
    audio_path = tmp_path / "voice.wav"
    _wav(audio_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_transcribe") as transcribe:
        result = runtime.infer(
            workspace_root=tmp_path,
            speech_text="总结当前选区",
            audio_ref="voice.wav",
        )

    assert result["status"] == "pass"
    assert result["stages"]["asr"]["reason"] == "speech_text_provided"
    assert result["perception_events"][0]["modality"] == "audio"
    assert result["perception_events"][0]["text"] == "总结当前选区"
    transcribe.assert_not_called()


def test_auto_visual_route_does_not_load_vlm_without_explicit_permission(tmp_path):
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    runtime = Topic5Runtime()
    with patch.object(runtime, "_recognize", return_value=[{"text": "", "confidence": 0.1}]):
        with patch.object(runtime, "_understand_image") as understand:
            result = runtime.infer(
                workspace_root=tmp_path,
                speech_text="解释这个图表",
                image_ref="screen.png",
                visual_mode="auto",
                events=[{"event_id": "selected", "modality": "ui_selection", "start_ms": 0, "payload": {"target_kind": "chart"}}],
            )

    assert result["stages"]["visual_route"]["route"] == "fast"
    assert result["stages"]["visual_route"]["reason"] == "slow_visual_not_enabled"
    assert result["stages"]["vlm"]["status"] == "skipped"
    understand.assert_not_called()


def test_runtime_status_endpoint_is_redacted_and_token_protected():
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        assert client.get("/multimodal/runtime/status").status_code == 401
        response = client.get("/multimodal/runtime/status", headers={"X-SoulDrive-Token": "runtime-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "souldrive_topic5_runtime_status_v0.1"
    assert "search_roots" not in payload
    assert "model_paths" not in payload


def test_runtime_warmup_endpoint_is_token_protected_and_uses_runtime(tmp_path, monkeypatch):
    calls = []
    audit_events = []

    class FakeRuntime:
        def warmup_vlm(self):
            calls.append(True)
            return {
                "schema_version": "souldrive_topic5_runtime_warmup_v0.1",
                "status": "pass",
                "already_loaded": False,
                "latency_ms": {"total": 12.0},
                "policy": {"side_effect": "none"},
            }

    class FakeAudit:
        def append_event(self, name, payload):
            audit_events.append((name, payload))

    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: FakeAudit())
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        assert client.post("/multimodal/runtime/warmup").status_code == 401
        response = client.post(
            "/multimodal/runtime/warmup",
            headers={"X-SoulDrive-Token": "runtime-token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "pass"
    assert calls == [True]
    assert audit_events[0][0] == "multimodal.runtime_warmup"


def test_runtime_perception_warmup_endpoint_is_token_protected_and_audited(monkeypatch):
    calls = []
    audit_events = []

    class FakeRuntime:
        def warmup_perception(self):
            calls.append(True)
            return {
                "schema_version": "souldrive_topic5_runtime_perception_warmup_v0.1",
                "status": "pass",
                "stages": {
                    "asr": {"status": "pass", "loaded": True},
                    "ocr": {"status": "pass", "loaded": True},
                },
            }

    class FakeAudit:
        def append_event(self, name, payload):
            audit_events.append((name, payload))

    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: FakeAudit())
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        assert client.post("/multimodal/runtime/warmup-perception").status_code == 401
        response = client.post(
            "/multimodal/runtime/warmup-perception",
            headers={"X-SoulDrive-Token": "runtime-token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "pass"
    assert calls == [True]
    assert audit_events[0] == (
        "multimodal.runtime_perception_warmup",
        {
            "status": "pass",
            "stages": {
                "asr": {"status": "pass", "loaded": True},
                "ocr": {"status": "pass", "loaded": True},
            },
            "side_effect": "none",
        },
    )


def test_runtime_upload_uses_temporary_workspace_media_and_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    workspace = SoulDriveWorkspace(str(tmp_path)).ensure()
    calls = []

    class FakeRuntime:
        def infer(self, **kwargs):
            calls.append(kwargs)
            return {
                "status": "pass",
                "speech_text": "总结当前选区",
                "stages": {"asr": {"status": "pass"}},
                "decision": {"intent": "selection_summary", "action": "respond"},
            }

    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeRuntime())
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        response = client.post(
            "/multimodal/runtime/infer-upload",
            headers={"X-SoulDrive-Token": "runtime-token"},
            files={"audio": ("voice.wav", b"RIFFtest", "audio/wav")},
            data={"target_id": "screen_target"},
        )

    assert response.status_code == 200
    assert calls[0]["audio_ref"].startswith("runtime/multimodal-capture/")
    assert calls[0]["max_new_tokens"] == DEFAULT_MAX_NEW_TOKENS
    assert not list((tmp_path / "runtime" / "multimodal-capture").glob("*"))


def test_runtime_upload_forwards_selected_region_and_visual_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    workspace = SoulDriveWorkspace(str(tmp_path)).ensure()
    calls = []

    class FakeRuntime:
        def infer(self, **kwargs):
            calls.append(kwargs)
            return {"status": "partial", "speech_text": "解释", "stages": {}, "perception_events": []}

    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeRuntime())
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    region = {"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.5}
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        response = client.post(
            "/multimodal/runtime/infer-upload",
            headers={"X-SoulDrive-Token": "runtime-token"},
            data={
                "speech_text": "解释",
                "selected_region_json": json.dumps(region),
                "run_vlm": "true",
                "vlm_prompt": "围绕用户问题描述图表趋势，不要猜测不可见信息。",
            },
            files={"image": ("selection.png", image.getvalue(), "image/png")},
        )

    assert response.status_code == 200
    assert calls[0]["selected_region"] == region
    assert calls[0]["run_vlm"] is True
    assert calls[0]["vlm_prompt"].startswith("围绕用户问题")


def test_runtime_upload_forwards_event_timing_and_project_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    workspace = SoulDriveWorkspace(str(tmp_path)).ensure()
    calls = []

    class FakeRuntime:
        def infer(self, **kwargs):
            calls.append(kwargs)
            return {"status": "pass", "speech_text": "总结", "stages": {}, "perception_events": []}

    monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeRuntime())
    events = [{
        "event_id": "uia-1",
        "modality": "ui_automation",
        "start_ms": 400,
        "end_ms": 1400,
        "project_id": "project-a",
        "event_time_origin_ms": 1_800_000_000_000,
    }]
    with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
        client = TestClient(mcp_server.app)
        response = client.post(
            "/multimodal/runtime/infer-upload",
            headers={"X-SoulDrive-Token": "runtime-token"},
            data={
                "speech_text": "总结",
                "project_id": "project-a",
                "events_json": json.dumps(events),
                "event_time_origin_ms": "1800000000000",
                "audio_start_ms": "400",
                "image_start_ms": "1400",
            },
        )

    assert response.status_code == 200
    assert calls[0]["events"] == events
    assert calls[0]["project_id"] == "project-a"
    assert calls[0]["event_time_origin_ms"] == 1_800_000_000_000
    assert calls[0]["audio_start_ms"] == 400
    assert calls[0]["image_start_ms"] == 1400


def test_chat_perception_event_accepts_interval_without_legacy_timestamp():
    request = mcp_server.ChatRequest(
        query="总结当前选区",
        perception_events=[{
            "event_id": "uia-1",
            "modality": "ui_automation",
            "start_ms": 400,
            "end_ms": 1400,
            "project_id": "project-a",
            "event_time_origin_ms": 1_800_000_000_000,
        }],
    )

    event = request.perception_events[0].model_dump()
    assert event["timestamp_ms"] is None
    assert event["start_ms"] == 400
    assert event["end_ms"] == 1400
    assert event["project_id"] == "project-a"
    assert event["event_time_origin_ms"] == 1_800_000_000_000


def test_runtime_upload_is_explicit_and_removes_temporary_media(monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        image = BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "runtime-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.post(
                "/multimodal/runtime/infer-upload",
                headers={"X-SoulDrive-Token": "runtime-token"},
                data={"speech_text": "总结当前选区", "run_ocr": "false", "run_vlm": "false"},
                files={"image": ("selection.png", image.getvalue(), "image/png")},
            )
        capture_dir = Path(workspace.runtime_path) / "multimodal-capture"
        remaining = list(capture_dir.glob("*")) if capture_dir.exists() else []

    assert response.status_code == 200
    assert response.json()["policy"]["side_effect"] == "none"
    assert remaining == []
