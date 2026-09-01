"""Validation gates for local audio and screenshot assets in MMI episodes."""

from __future__ import annotations

import mimetypes
import wave
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_SUFFIXES = {".wav"}
MAX_AUDIO_SECONDS = 15
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def validate_episode_media(episode: dict[str, Any], media_root: Path, *, require_media: bool = False) -> list[str]:
    errors: list[str] = []
    audio_refs = []
    image_refs = []
    for event in episode.get("events", []):
        ref = event.get("payload_ref") or (event.get("payload") or {}).get("media_ref")
        if not ref:
            continue
        path = _safe_media_path(media_root, str(ref))
        if path is None:
            errors.append(f"{episode.get('episode_id')}: invalid media path {ref!r}")
            continue
        if path.suffix.lower() in AUDIO_SUFFIXES:
            audio_refs.append(path)
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            image_refs.append(path)
        else:
            errors.append(f"{episode.get('episode_id')}: unsupported media suffix {path.suffix}")
    if require_media and not audio_refs:
        errors.append(f"{episode.get('episode_id')}: missing audio waveform")
    if require_media and not image_refs:
        errors.append(f"{episode.get('episode_id')}: missing screenshot/UI image")
    for path in audio_refs:
        errors.extend(_validate_audio(episode, path))
    for path in image_refs:
        errors.extend(_validate_image(episode, path))
    return errors


def _safe_media_path(root: Path, ref: str) -> Path | None:
    if ref.startswith(("http://", "https://", "file://")):
        return None
    candidate = Path(ref)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def _validate_audio(episode: dict[str, Any], path: Path) -> list[str]:
    prefix = f"{episode.get('episode_id')}: {path.name}"
    if not path.is_file():
        return [f"{prefix}: file does not exist"]
    try:
        with wave.open(str(path), "rb") as handle:
            channels, width, rate, frames = handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()
    except (wave.Error, OSError) as exc:
        return [f"{prefix}: invalid WAV ({exc})"]
    errors = []
    if channels != 1 or width != 2 or rate != 16000:
        errors.append(f"{prefix}: expected mono 16-bit 16kHz PCM, got channels={channels} width={width} rate={rate}")
    if frames > rate * MAX_AUDIO_SECONDS:
        errors.append(f"{prefix}: duration exceeds {MAX_AUDIO_SECONDS}s")
    return errors


def _validate_image(episode: dict[str, Any], path: Path) -> list[str]:
    prefix = f"{episode.get('episode_id')}: {path.name}"
    if not path.is_file():
        return [f"{prefix}: file does not exist"]
    errors = []
    if path.stat().st_size > MAX_IMAGE_BYTES:
        errors.append(f"{prefix}: image exceeds {MAX_IMAGE_BYTES} bytes")
    mime, _ = mimetypes.guess_type(path.name)
    if not mime or not mime.startswith("image/"):
        errors.append(f"{prefix}: invalid image MIME")
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        errors.append(f"{prefix}: invalid image ({exc})")
    return errors
