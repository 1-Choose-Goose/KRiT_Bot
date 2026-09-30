from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CENTER_TIMEZONE_NAME = "Asia/Yekaterinburg"
_CENTER_TIMEZONE = ZoneInfo(_CENTER_TIMEZONE_NAME)


def configure_center_timezone(name: str) -> None:
    global _CENTER_TIMEZONE_NAME, _CENTER_TIMEZONE
    try:
        timezone = ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс центра: {name}") from exc
    _CENTER_TIMEZONE_NAME = name
    _CENTER_TIMEZONE = timezone


def center_timezone() -> ZoneInfo:
    return _CENTER_TIMEZONE


def center_timezone_name() -> str:
    return _CENTER_TIMEZONE_NAME


def parse_center(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_CENTER_TIMEZONE)
    return parsed.astimezone(_CENTER_TIMEZONE)


def now_center() -> datetime:
    return datetime.now(_CENTER_TIMEZONE)


def center_wall_time(value: datetime) -> datetime:
    """Interpret a desktop date/time editor's visible fields in the center timezone."""
    return value.replace(tzinfo=_CENTER_TIMEZONE)
