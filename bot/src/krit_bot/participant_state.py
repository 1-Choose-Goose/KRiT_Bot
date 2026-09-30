from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class ParticipantState(Protocol):
    attendance_status: str
    arrived_at: datetime | None
    left_at: datetime | None
    late_minutes: int | None
    cancelled_at: datetime | None
    cancelled_by: str | None
    cancelled_by_person_id: int | None
    cancelled_by_admin_id: int | None
    cancellation_reason: str | None
    early_leave_reason: str | None


def apply_attendance_state(
    participant: ParticipantState,
    status: str,
    *,
    arrived_at: datetime | None = None,
    left_at: datetime | None = None,
    late_minutes: int | None = None,
    early_leave_reason: str | None = None,
    cancelled_at: datetime | None = None,
    cancelled_by: str | None = None,
    cancelled_by_person_id: int | None = None,
    cancelled_by_admin_id: int | None = None,
    cancellation_reason: str | None = None,
) -> None:
    """Apply one participant state while clearing metadata owned by other states."""
    if status not in {"expected", "present", "late", "absent", "left_early", "excused"}:
        raise ValueError("Неизвестный статус посещения")

    participant.attendance_status = status
    if status == "expected":
        participant.arrived_at = None
        participant.left_at = None
        participant.late_minutes = None
        participant.early_leave_reason = None
        _clear_cancellation(participant)
        return
    if status == "present":
        if arrived_at is None:
            raise ValueError("Для присутствующего ученика укажите время прихода")
        participant.arrived_at = arrived_at
        participant.left_at = None
        participant.late_minutes = 0
        participant.early_leave_reason = None
        _clear_cancellation(participant)
        return
    if status == "late":
        if arrived_at is None or late_minutes is None or late_minutes <= 0:
            raise ValueError("Для опоздавшего укажите время прихода и минуты опоздания")
        participant.arrived_at = arrived_at
        participant.left_at = None
        participant.late_minutes = late_minutes
        participant.early_leave_reason = None
        _clear_cancellation(participant)
        return
    if status == "absent":
        participant.arrived_at = None
        participant.left_at = None
        participant.late_minutes = None
        participant.early_leave_reason = None
        _clear_cancellation(participant)
        return
    if status == "left_early":
        if (
            arrived_at is None
            or left_at is None
            or _comparable(left_at) < _comparable(arrived_at)
        ):
            raise ValueError("Время ухода не может быть раньше времени прихода")
        participant.arrived_at = arrived_at
        participant.left_at = left_at
        participant.late_minutes = late_minutes if late_minutes and late_minutes > 0 else 0
        participant.early_leave_reason = early_leave_reason
        _clear_cancellation(participant)
        return

    if cancelled_at is None or cancelled_by not in {"student", "guardian", "administrator"}:
        raise ValueError("Для отменённого участия укажите инициатора и время отмены")
    participant.arrived_at = None
    participant.left_at = None
    participant.late_minutes = None
    participant.early_leave_reason = None
    participant.cancelled_at = cancelled_at
    participant.cancelled_by = cancelled_by
    participant.cancelled_by_person_id = cancelled_by_person_id
    participant.cancelled_by_admin_id = cancelled_by_admin_id
    participant.cancellation_reason = cancellation_reason


def _clear_cancellation(participant: ParticipantState) -> None:
    participant.cancelled_at = None
    participant.cancelled_by = None
    participant.cancelled_by_person_id = None
    participant.cancelled_by_admin_id = None
    participant.cancellation_reason = None


def _comparable(value: datetime) -> datetime:
    """Normalize SQLite-naive and PostgreSQL-aware timestamps for validation only."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
