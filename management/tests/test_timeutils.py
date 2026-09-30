from __future__ import annotations

from datetime import UTC, datetime

from krit_management.timeutils import (
    center_timezone_name,
    center_wall_time,
    configure_center_timezone,
    parse_center,
)


def test_center_timezone_does_not_depend_on_operating_system_timezone() -> None:
    configure_center_timezone("Asia/Yekaterinburg")
    parsed = parse_center("2026-09-30T10:00:00+00:00")
    assert parsed.strftime("%Y-%m-%d %H:%M %z") == "2026-09-30 15:00 +0500"
    assert center_timezone_name() == "Asia/Yekaterinburg"

    editor_value = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    wall_time = center_wall_time(editor_value)
    assert wall_time.hour == 15
    assert wall_time.utcoffset().total_seconds() == 5 * 60 * 60
