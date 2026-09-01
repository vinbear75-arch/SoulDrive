"""Export confirmed local schedule drafts as standard iCalendar files."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path


def export_ics(workspace, artifact_id: str, plan: dict) -> str:
    start = _parse_start_at(plan["start_at"])
    end = start + timedelta(minutes=int(plan["duration_minutes"]))
    participant = plan.get("participant") or {}
    uid = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest() + "@souldrive.local"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SoulDrive//Local Schedule//CN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{_utc(datetime.now().astimezone())}", f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}",
        f"SUMMARY:{_escape(plan['title'])}", f"DESCRIPTION:{_escape(plan.get('notes') or '')}",
    ]
    if participant.get("email"):
        lines.append(f"ATTENDEE;CN={_escape(participant.get('display_name') or participant['email'])}:MAILTO:{participant['email']}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    output = Path(workspace.calendars_path) / f"{artifact_id}.ics"
    output.write_text("\r\n".join(lines), encoding="utf-8", newline="")
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
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
