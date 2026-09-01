from scripts.eval_public_gui_speech_augmentation import _distance, _normalize


def test_character_error_helpers_are_deterministic():
    assert _normalize("打开 文件！") == "打开文件"
    assert _distance("打开文件", "打开文档") == 1
