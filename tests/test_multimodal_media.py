import wave

from core.multimodal_media import validate_episode_media


def test_media_validator_rejects_network_and_traversal_refs(tmp_path):
    episode = {"episode_id": "E1", "events": [{"payload_ref": "https://example.com/a.wav"}, {"payload_ref": "../secret.wav"}]}
    errors = validate_episode_media(episode, tmp_path)
    assert len(errors) == 2


def test_media_validator_accepts_mono_16khz_wav(tmp_path):
    path = tmp_path / "audio" / "e1.wav"
    path.parent.mkdir()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)
    episode = {"episode_id": "E1", "events": [{"payload_ref": "audio/e1.wav"}]}
    assert validate_episode_media(episode, tmp_path) == []
