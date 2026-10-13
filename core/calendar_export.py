"""Export confirmed local schedule drafts as standard iCalendar files."""

import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def export_ics(
    workspace,
    artifact_id: str,
    plan: dict,
    *,
    stamp_at: object | None = None,
) -> str:
    start = _parse_start_at(plan["start_at"])
    end = start + timedelta(minutes=int(plan["duration_minutes"]))
    participant = plan.get("participant") or {}
    uid = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest() + "@souldrive.local"
    stamp = _parse_start_at(stamp_at) if stamp_at is not None else datetime.now().astimezone()
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SoulDrive//Local Schedule//CN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{_utc(stamp)}", f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}",
        f"SUMMARY:{_escape(plan['title'])}", f"DESCRIPTION:{_escape(plan.get('notes') or '')}",
    ]
    if participant.get("email"):
        email = _safe_participant_email(participant["email"])
        display_name = _escape_parameter(participant.get("display_name") or email)
        common_name = f";CN={display_name}" if display_name else ""
        lines.append(f"ATTENDEE{common_name}:MAILTO:{email}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    output = Path(workspace.calendars_path) / f"{artifact_id}.ics"
    payload = "\r\n".join(_fold_ics_line(line) for line in lines).encode("utf-8")
    _atomic_write_bytes(output, payload)
    return str(output)


def normalize_start_at(value: object) -> str:
    return _parse_start_at(value).isoformat()


def _parse_start_at(value: object) -> datetime:
    try:
        start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("schedule start time is invalid") from exc
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return start


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(value: object) -> str:
    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _escape_parameter(value: object) -> str | None:
    """Encode a parameter value; reserved separators require quoted-string."""
    text = str(value or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if '"' in text:
        # RFC 5545 does not define an escape for DQUOTE in parameter values.
        # Keep the attendee address and omit only the optional display name.
        return None
    if any(separator in text for separator in (":", ";", ",")):
        return f'"{text}"'
    return text


def _safe_participant_email(value: object) -> str:
    """Keep a local email from becoming an invalid or injected content line."""
    text = str(value or "").strip()
    if "@" not in text or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in text
    ):
        raise ValueError("participant email is invalid")
    return text


def _fold_ics_line(line: str) -> str:
    """Fold one content line without splitting a UTF-8 code point."""
    chunks: list[str] = []
    current: list[str] = []
    current_octets = 0
    limit = 75
    for character in str(line):
        octets = len(character.encode("utf-8"))
        if current and current_octets + octets > limit:
            chunks.append("".join(current))
            current = [" "]
            current_octets = 1
            limit = 74
        current.append(character)
        current_octets += octets
    chunks.append("".join(current))
    return "\r\n".join(chunks)


def _atomic_write_bytes(output: Path, payload: bytes) -> None:
    """Publish a complete local artifact, never a partially written target."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        # The temporary file lives beside the target, so replace does not cross
        # filesystems. On Windows the source handle is already closed here.
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
