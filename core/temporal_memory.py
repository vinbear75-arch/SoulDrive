"""Small, explicit temporal resolver for point-in-time memory lookup."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_MEMORY_TIMEZONE = "Asia/Shanghai"
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?"),
)


def resolve_memory_time_scope(
    query: str,
    *,
    reference_timestamp_ms: int | None = None,
    timezone_name: str = DEFAULT_MEMORY_TIMEZONE,
) -> dict[str, Any]:
    """Resolve only unambiguous historical expressions for memory retrieval.

    The returned ``as_of`` is the end of the referenced local day or calendar
    period.  Multiple explicit dates are left unresolved so comparison flows
    can require their existing explicit run-selection contract.
    """

    timezone = _timezone(timezone_name)
    reference = _reference_datetime(reference_timestamp_ms, timezone)
    text = str(query or "")
    matches = [match for pattern in _DATE_PATTERNS for match in pattern.finditer(text)]
    relative_expressions = [
        expression
        for expression in ("前天", "昨天", "上个月", "上周", "去年")
        if expression in text
    ]
    if len(matches) > 1 or len(relative_expressions) > 1 or (matches and relative_expressions):
        return _unresolved_scope(timezone, "ambiguous_multiple_dates")
    if matches:
        match = matches[0]
        try:
            target = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone,
            )
        except ValueError:
            return _unresolved_scope(timezone, "invalid_explicit_date")
        return _resolved_scope(
            target + timedelta(days=1) - timedelta(microseconds=1),
            match.group(0),
            "explicit_date_end",
            timezone,
        )

    if relative_expressions:
        expression = relative_expressions[0]
        if expression in {"昨天", "前天"}:
            days_ago = 1 if expression == "昨天" else 2
            day_start = reference.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(days=days_ago)
            return _resolved_scope(
                day_start + timedelta(days=1) - timedelta(microseconds=1),
                expression,
                "previous_calendar_day_end" if days_ago == 1 else "two_days_ago_calendar_day_end",
                timezone,
            )

    if "上个月" in text:
        month_start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return _resolved_scope(
            month_start - timedelta(microseconds=1),
            "上个月",
            "previous_calendar_month_end",
            timezone,
        )
    if "上周" in text:
        week_start = (reference - timedelta(days=reference.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return _resolved_scope(
            week_start - timedelta(microseconds=1),
            "上周",
            "previous_iso_week_end",
            timezone,
        )
    if "去年" in text:
        year_start = reference.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return _resolved_scope(
            year_start - timedelta(microseconds=1),
            "去年",
            "previous_calendar_year_end",
            timezone,
        )
    return _unresolved_scope(timezone, "no_explicit_historical_scope")


def _reference_datetime(reference_timestamp_ms: int | None, timezone: ZoneInfo) -> datetime:
    if reference_timestamp_ms is None:
        return datetime.fromtimestamp(time.time(), tz=timezone)
    return datetime.fromtimestamp(float(reference_timestamp_ms) / 1000.0, tz=timezone)


def _timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(timezone_name or DEFAULT_MEMORY_TIMEZONE))
    except Exception:
        return ZoneInfo(DEFAULT_MEMORY_TIMEZONE)


def _resolved_scope(
    value: datetime,
    expression: str,
    resolution: str,
    timezone: ZoneInfo,
) -> dict[str, Any]:
    return {
        "applied": True,
        "as_of": value.timestamp(),
        "as_of_local": value.isoformat(),
        "expression": expression,
        "resolution": resolution,
        "timezone": getattr(timezone, "key", str(timezone)),
    }


def _unresolved_scope(timezone: ZoneInfo, resolution: str) -> dict[str, Any]:
    return {
        "applied": False,
        "as_of": None,
        "as_of_local": None,
        "expression": None,
        "resolution": resolution,
        "timezone": getattr(timezone, "key", str(timezone)),
    }
