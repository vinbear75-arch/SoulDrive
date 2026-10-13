from core.soulalign import _bounded_confidence, decide, verify_target_candidates


def test_target_confidence_rejects_non_finite_values():
    assert _bounded_confidence(float("nan")) == 0.0
    assert _bounded_confidence(float("inf")) == 0.0


def test_soulalign_requires_clarification_for_low_confidence_target():
    result = decide("总结当前选区", [
        {"event_id": "speech-1", "modality": "speech_text", "start_ms": 1000},
        {"event_id": "ocr-1", "modality": "ocr_region", "start_ms": 1100, "confidence": 0.4, "payload": {"target_id": "report"}},
    ])
    assert result["intent"] == "selection_summary"
    assert result["action"] == "clarify"
    assert result["side_effect"] == "none"


def test_target_verifier_prefers_focused_ui_automation_candidate():
    result = verify_target_candidates(
        "总结当前指标",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "screen",
                "modality": "ui_automation",
                "start_ms": 1000,
                "confidence": 0.9,
                "payload": {
                    "target_candidates": [
                        {"target_id": "background", "confidence": 0.7},
                        {"target_id": "focused-metric", "confidence": 0.9, "is_focused": True},
                    ]
                },
            },
        ],
    )

    assert result["selected_target_id"] == "focused-metric"
    assert result["required"] is False
    assert result["candidates"][0]["target_id"] == "focused-metric"


def test_target_verifier_does_not_auto_select_close_candidates():
    result = decide(
        "总结当前指标",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "screen",
                "modality": "ui_automation",
                "start_ms": 1000,
                "payload": {
                    "target_candidates": [
                        {"target_id": "metric-a", "confidence": 0.9},
                        {"target_id": "metric-b", "confidence": 0.89},
                    ]
                },
            },
        ],
    )

    assert result["target_id"] is None
    assert result["action"] == "clarify"
    assert "ambiguous_target_candidates" in result["target_verification"]["reason_codes"]
    assert result["clarification"]["prompt"]


def test_target_verifier_rejects_unaligned_candidate():
    result = verify_target_candidates(
        "总结当前指标",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 10_000},
            {
                "event_id": "old-screen",
                "modality": "ocr_region",
                "start_ms": 0,
                "confidence": 0.99,
                "payload": {"target_id": "old-metric"},
            },
        ],
    )

    assert result["selected_target_id"] is None
    assert result["required"] is True
    assert "target_outside_time_window" in result["reason_codes"]


def test_target_verifier_rejects_candidate_from_another_active_project():
    result = verify_target_candidates(
        "总结当前指标",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "other-project",
                "modality": "ui_automation",
                "start_ms": 1000,
                "payload": {
                    "target_candidates": [
                        {"target_id": "other-metric", "project_id": "project-b", "confidence": 0.99}
                    ]
                },
            },
        ],
        active_project_id="project-a",
    )

    assert result["selected_target_id"] is None
    assert "project_mismatch" in result["reason_codes"]


def test_target_verifier_accepts_authorized_image_region_candidates():
    result = decide(
        "总结当前选区",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "visual",
                "modality": "image_region",
                "start_ms": 1000,
                "confidence": 0.95,
                "payload": {
                    "target_candidates": [
                        {
                            "target_id": "vlm-region-1",
                            "source": "vlm",
                            "confidence": 0.91,
                            "bbox": {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.2},
                        }
                    ]
                },
            },
        ],
        target_required=True,
    )

    assert result["target_id"] == "vlm-region-1"
    assert result["action"] == "respond"


def test_target_verifier_filters_candidates_outside_selected_region():
    result = verify_target_candidates(
        "总结当前选区",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "visual",
                "modality": "image_region",
                "start_ms": 1000,
                "bbox": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
                "payload": {
                    "target_candidates": [
                        {
                            "target_id": "outside",
                            "confidence": 1.0,
                            "bbox": {"x": 0.75, "y": 0.75, "width": 0.2, "height": 0.2},
                        },
                        {
                            "target_id": "inside",
                            "confidence": 0.75,
                            "bbox": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                        },
                    ]
                },
            },
        ],
    )

    assert result["selected_target_id"] == "inside"
    assert result["candidates"][0]["target_id"] == "outside"
    assert result["candidates"][0]["spatial_score"] == 0.0
    assert result["candidates"][1]["spatial_score"] == 1.0


def test_soulalign_refuses_destructive_request():
    result = decide("删除桌面上的所有文件", [])
    assert result["intent"] == "out_of_scope"
    assert result["action"] == "refuse"


def test_soulalign_covers_asr_paraphrases_for_pc_tasks():
    cases = {
        "提取被框选内容的结论": "selection_summary",
        "把屏幕选区换成严谨表述": "rewrite_selection",
        "把当前选区改成正式的项目周报口吻": "rewrite_selection",
        "对比屏幕上的两份运行数据": "project_compare",
        "列举这份记录的后续动作": "extract_tasks",
        "给项目协作生成时间建议": "schedule_draft",
        "为联系人编写项目汇报信": "email_draft",
        "定位本机资料中的支撑证据": "search_document",
        "更换当前产物的展示模板": "switch_template",
        "根据实验结果说明这个指标": "private_qa",
    }
    for text, intent in cases.items():
        assert decide(text, [])['intent'] == intent


def test_soulalign_covers_common_chinese_asr_variants_for_control_intents():
    cases = {
        "生成材料的处理事项": "extract_tasks",
        "允许继续完成预览内容": "confirm",
        "接受当前待执行操作": "confirm",
        "请结束这个预览流程": "cancel",
        "撤回该执行的工作": "cancel",
        "不要保留当前输出计划": "cancel",
        "请对屏幕中指向内容做确认": "clarify_target",
        "需要消除本轮任务的歧义": "clarify_target",
        "把系统文件全部删掉": "out_of_scope",
        "不要预览就执行外部发送": "out_of_scope",
        "完成与项目无关的桌面破坏操作": "out_of_scope",
        "分析这两组项目证据的区别": "project_compare",
        "从本地资料定位该实验结果": "private_qa",
        "检索文档后给出有依据的回答": "private_qa",
        "创建实验结论的邮件预览": "email_draft",
        "寻找屏幕任务关联的项目文档": "search_document",
        "应用新的项目文档样式": "switch_template",
    }
    for text, intent in cases.items():
        assert decide(text, [])['intent'] == intent


def test_unclassified_question_with_screenshot_is_allowed_to_reach_rag():
    result = decide(
        "RAG 的核心思想是什么？",
        [
            {
                "event_id": "speech",
                "modality": "speech_text",
                "start_ms": 1000,
                "payload": {"text": "RAG 的核心思想是什么？"},
            },
            {
                "event_id": "ocr",
                "modality": "image_region",
                "start_ms": 1000,
                "confidence": 0.92,
                "payload": {
                    "target_candidates": [
                        {"target_id": "ocr-region", "confidence": 0.92}
                    ]
                },
            },
        ],
        active_project_id="project-a",
    )

    assert result["intent"] == "private_qa"
    assert result["action"] == "respond"
    assert result["clarification_required"] is False


def test_ui_automation_target_still_requires_clarification_when_ambiguous():
    result = decide(
        "总结当前指标",
        [
            {"event_id": "speech", "modality": "speech_text", "start_ms": 1000},
            {
                "event_id": "screen",
                "modality": "ui_automation",
                "start_ms": 1000,
                "payload": {
                    "target_candidates": [
                        {"target_id": "metric-a", "confidence": 0.9},
                        {"target_id": "metric-b", "confidence": 0.89},
                    ]
                },
            },
        ],
    )

    assert result["action"] == "clarify"
    assert result["clarification_required"] is True


def test_soulalign_prioritizes_explicit_project_search_over_fact_keywords():
    assert decide("搜索项目里关于这个指标的实验记录。", [])['intent'] == "search_document"
    assert decide("检索文档后给出有依据的回答", [])['intent'] == "private_qa"
    assert decide("根据实验结果说明这个指标", [])['intent'] == "private_qa"


def test_soulalign_requires_confirmation_for_direct_email_send():
    result = decide(
        "把当前选区改成给导师的邮件并直接发送",
        [
            {
                "event_id": "selection-1",
                "modality": "ui_selection",
                "start_ms": 1000,
                "confidence": 0.96,
                "payload": {"target_id": "paper.pdf", "project_id": "rag-cn-demo"},
            }
        ],
    )

    assert result["intent"] == "email_draft"
    assert result["clarification_required"] is True
    assert result["action"] == "clarify"


def test_soulalign_returns_ambiguous_deictic_and_relative_time_requests_to_user():
    for text in ("总结这个文档", "比较上个月的实验结果", "确认哪个对象"):
        result = decide(text, [])
        assert result["intent"] == "clarify_target"
        assert result["action"] == "clarify"
        assert result["clarification_required"] is True
        assert result["clarification"]["required"] is True
        assert "ambiguous_reference" in result["clarification"]["reason_codes"]
        assert result["clarification"]["prompt"]
