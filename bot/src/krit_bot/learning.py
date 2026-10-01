from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import Person, PersonRole, StudentGuardian, utcnow
from .learning_models import (
    AdminNotification,
    AuditEvent,
    ClubPresenceSession,
    GroupMembership,
    Lesson,
    LessonParticipant,
    LessonSeries,
    LessonTeacherSegment,
    NotificationJob,
    Room,
    StudyGroup,
    Subject,
    SubjectTeacher,
)
from .participant_state import apply_attendance_state


class NamedPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    active: bool = True


class SubjectPayload(NamedPayload):
    color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    teacher_ids: list[int] | None = None


class RoomPayload(NamedPayload):
    capacity: int = Field(ge=1, le=1000)


class GroupPayload(NamedPayload):
    subject_id: int | None = None
    default_teacher_id: int | None = None
    default_duration_minutes: int = Field(default=60, ge=5, le=1440)


class MembershipPayload(BaseModel):
    person_id: int
    start_at: datetime
    end_at: datetime | None = None

    @model_validator(mode="after")
    def validate_period(self) -> MembershipPayload:
        if self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("Дата окончания должна быть позже даты начала")
        return self


class MembershipEndPayload(BaseModel):
    end_at: datetime | None = None


class LessonPayload(BaseModel):
    subject_id: int
    teacher_id: int
    room_id: int
    group_id: int | None = None
    start_at: datetime
    end_at: datetime
    participant_ids: list[int] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_period(self) -> LessonPayload:
        if self.end_at <= self.start_at:
            raise ValueError("Окончание должно быть позже начала")
        return self


class SeriesPayload(BaseModel):
    subject_id: int
    teacher_id: int
    room_id: int
    group_id: int | None = None
    starts_at: datetime
    duration_minutes: int = Field(ge=5, le=1440)
    interval_weeks: int = Field(default=1, ge=1, le=52)
    occurrences: int = Field(ge=1, le=104)
    participant_ids: list[int] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=4000)


class SeriesUpdatePayload(SeriesPayload):
    scope: Literal["future", "all"] = "future"
    anchor_lesson_id: int | None = None


class CancelPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EarlyCompletionPayload(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    public_comment: str | None = Field(default=None, max_length=500)


class EarlyLeavePayload(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class TeacherTransitionPayload(BaseModel):
    action: Literal["substitute", "finish_early"]
    reason: str = Field(min_length=3, max_length=500)
    replacement_teacher_id: int | None = None
    expected_end_at: datetime | None = None
    public_comment: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_replacement(self) -> TeacherTransitionPayload:
        if self.action == "substitute" and self.replacement_teacher_id is None:
            raise ValueError("Для замены выберите нового преподавателя")
        if self.action == "finish_early" and self.replacement_teacher_id is not None:
            raise ValueError("При завершении занятия замена не назначается")
        if self.action == "finish_early" and self.expected_end_at is not None:
            raise ValueError("При завершении занятия ожидаемое окончание не задаётся")
        return self


class ParticipantCancelPayload(BaseModel):
    cancelled_by: Literal["student", "guardian", "administrator"] = "administrator"
    cancelled_by_person_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)


class AttendancePayload(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=500)


class CorrectionPayload(BaseModel):
    attendance_status: str
    arrived_at: datetime | None = None
    left_at: datetime | None = None
    reason: str = Field(min_length=3, max_length=500)


class ActualTimeCorrectionPayload(BaseModel):
    actual_start_at: datetime
    actual_end_at: datetime
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def validate_period(self) -> ActualTimeCorrectionPayload:
        if self.actual_end_at <= self.actual_start_at:
            raise ValueError("Фактическое окончание должно быть позже начала")
        return self


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise HTTPException(422, "Дата и время должны содержать часовой пояс")
    return value.astimezone(UTC)


def _db_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _display_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=5))


def _model(item: Any, *fields: str) -> dict[str, Any]:
    return {field: getattr(item, field) for field in fields}


def _unique_admin_notifications(
    items: list[AdminNotification],
) -> list[AdminNotification]:
    result: list[AdminNotification] = []
    seen: set[tuple[object, ...]] = set()
    for item in items:
        key = ("key", item.dedupe_key)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


async def _teacher_segments(session: AsyncSession, lesson_id: int) -> list[LessonTeacherSegment]:
    return list(
        (
            await session.scalars(
                select(LessonTeacherSegment)
                .where(LessonTeacherSegment.lesson_id == lesson_id)
                .order_by(LessonTeacherSegment.started_at, LessonTeacherSegment.id)
            )
        ).all()
    )


async def _add_admin_notification(
    session: AsyncSession,
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    message: str,
    lesson_id: int | None,
    condition_key: str | None = None,
) -> bool:
    values = {
        "dedupe_key": dedupe_key,
        "kind": kind,
        "title": title,
        "message": message,
        "lesson_id": lesson_id,
        "condition_key": condition_key,
        "created_at": utcnow(),
    }
    existing = await session.execute(
        AdminNotification.__table__.update()
        .where(AdminNotification.dedupe_key == dedupe_key)
        .values(title=title, message=message, lesson_id=lesson_id)
    )
    if existing.rowcount:
        return False
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        result = await session.execute(
            pg_insert(AdminNotification).values(**values).on_conflict_do_nothing()
        )
        return bool(result.rowcount)
    if dialect == "sqlite":
        result = await session.execute(
            sqlite_insert(AdminNotification).values(**values).on_conflict_do_nothing()
        )
        return bool(result.rowcount)
    try:
        async with session.begin_nested():
            session.add(AdminNotification(**values))
            await session.flush()
        return True
    except IntegrityError:
        return False


async def _raise_admin_condition(
    session: AsyncSession,
    *,
    condition_key: str,
    kind: str,
    title: str,
    message: str,
    lesson_id: int | None,
) -> bool:
    existing = await session.scalar(
        select(AdminNotification.id).where(
            AdminNotification.condition_key == condition_key,
            AdminNotification.resolved_at.is_(None),
        )
    )
    if existing is not None:
        return False
    occurred_at = utcnow()
    return await _add_admin_notification(
        session,
        dedupe_key=f"{condition_key}:occurrence:{occurred_at.isoformat()}",
        condition_key=condition_key,
        kind=kind,
        title=title,
        message=message,
        lesson_id=lesson_id,
    )


async def _resolve_admin_condition(session: AsyncSession, condition_key: str) -> None:
    now = utcnow()
    await session.execute(
        AdminNotification.__table__.update()
        .where(
            AdminNotification.condition_key == condition_key,
            AdminNotification.resolved_at.is_(None),
        )
        .values(resolved_at=now)
    )


async def _participants_for(
    session: AsyncSession, payload: LessonPayload | SeriesPayload, at: datetime
) -> list[Person]:
    ids = set(payload.participant_ids)
    if payload.group_id is not None:
        ids.update(
            (
                await session.scalars(
                    select(GroupMembership.person_id).where(
                        GroupMembership.group_id == payload.group_id,
                        GroupMembership.start_at <= at,
                        or_(GroupMembership.end_at.is_(None), GroupMembership.end_at > at),
                    )
                )
            ).all()
        )
    if not ids:
        return []
    people = list(
        (
            await session.scalars(
                select(Person).where(
                    Person.id.in_(ids),
                    Person.active.is_(True),
                    Person.archived_at.is_(None),
                )
            )
        ).all()
    )
    if len(people) != len(ids):
        raise HTTPException(422, "Один или несколько участников недоступны")
    student_ids = set(
        (
            await session.scalars(
                select(PersonRole.person_id).where(
                    PersonRole.person_id.in_(ids), PersonRole.role == "student"
                )
            )
        ).all()
    )
    if student_ids != ids:
        raise HTTPException(422, "Участники занятия должны иметь роль ученика")
    return people


async def _references(
    session: AsyncSession, payload: LessonPayload | SeriesPayload
) -> tuple[Subject, Person, Room, StudyGroup | None]:
    subject = await session.get(Subject, payload.subject_id)
    teacher = await session.get(Person, payload.teacher_id)
    room = await session.get(Room, payload.room_id)
    group = await session.get(StudyGroup, payload.group_id) if payload.group_id else None
    if subject is None or not subject.active:
        raise HTTPException(422, "Предмет недоступен")
    if room is None or not room.active:
        raise HTTPException(422, "Кабинет недоступен")
    if teacher is None or not teacher.active or teacher.archived_at is not None:
        raise HTTPException(422, "Преподаватель недоступен")
    is_teacher = await session.scalar(
        select(PersonRole.person_id).where(
            PersonRole.person_id == teacher.id, PersonRole.role == "teacher"
        )
    )
    if is_teacher is None:
        raise HTTPException(422, "У выбранного человека нет роли учителя")
    qualification = await session.get(
        SubjectTeacher,
        {"subject_id": subject.id, "teacher_id": teacher.id},
    )
    if qualification is None:
        raise HTTPException(422, "Преподаватель не закреплён за выбранным предметом")
    if payload.group_id is not None and (group is None or not group.active):
        raise HTTPException(422, "Группа недоступна")
    return subject, teacher, room, group


async def _replace_subject_teachers(
    session: AsyncSession, subject_id: int, teacher_ids: list[int]
) -> list[int]:
    unique_ids = sorted(set(teacher_ids))
    if unique_ids:
        valid_ids = set(
            (
                await session.scalars(
                    select(PersonRole.person_id)
                    .join(Person, Person.id == PersonRole.person_id)
                    .where(
                        PersonRole.person_id.in_(unique_ids),
                        PersonRole.role == "teacher",
                        Person.active.is_(True),
                        Person.archived_at.is_(None),
                    )
                )
            ).all()
        )
        if valid_ids != set(unique_ids):
            raise HTTPException(422, "Один или несколько преподавателей недоступны")
    await session.execute(delete(SubjectTeacher).where(SubjectTeacher.subject_id == subject_id))
    session.add_all(
        [SubjectTeacher(subject_id=subject_id, teacher_id=teacher_id) for teacher_id in unique_ids]
    )
    return unique_ids


async def _conflicts(
    session: AsyncSession,
    *,
    start_at: datetime,
    end_at: datetime,
    teacher_id: int,
    room_id: int,
    participant_ids: set[int],
    exclude_lesson_id: int | None = None,
    exclude_lesson_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    # Exact half-open interval rule: A.start < B.end and B.start < A.end.
    segment_query = (
        select(LessonTeacherSegment, Lesson)
        .join(Lesson, Lesson.id == LessonTeacherSegment.lesson_id)
        .where(
            Lesson.status == "in_progress",
            LessonTeacherSegment.started_at < end_at,
            or_(
                LessonTeacherSegment.ended_at.is_(None),
                LessonTeacherSegment.ended_at > start_at,
            ),
        )
    )
    if exclude_lesson_id is not None:
        segment_query = segment_query.where(Lesson.id != exclude_lesson_id)
    if exclude_lesson_ids:
        segment_query = segment_query.where(Lesson.id.not_in(exclude_lesson_ids))
    segment_rows = (await session.execute(segment_query)).all()
    active_lesson_ids = {lesson.id for _, lesson in segment_rows}

    planned_overlap = and_(Lesson.start_at < end_at, Lesson.end_at > start_at)
    overlap = (
        or_(planned_overlap, Lesson.id.in_(active_lesson_ids))
        if active_lesson_ids
        else planned_overlap
    )
    query = select(Lesson).where(Lesson.status != "cancelled", overlap)
    if exclude_lesson_id is not None:
        query = query.where(Lesson.id != exclude_lesson_id)
    if exclude_lesson_ids:
        query = query.where(Lesson.id.not_in(exclude_lesson_ids))
    existing = list((await session.scalars(query.with_for_update())).all())
    result: list[dict[str, Any]] = []
    lesson_ids = [item.id for item in existing]
    busy_students: dict[int, set[int]] = {}
    checked_people = participant_ids | {teacher_id}
    if lesson_ids and checked_people:
        rows = (
            await session.execute(
                select(LessonParticipant.lesson_id, LessonParticipant.person_id).where(
                    LessonParticipant.lesson_id.in_(lesson_ids),
                    LessonParticipant.person_id.in_(checked_people),
                    LessonParticipant.attendance_status != "excused",
                )
            )
        ).all()
        for lesson_id, person_id in rows:
            busy_students.setdefault(lesson_id, set()).add(person_id)
    for item in existing:
        overlaps_plan = (
            _db_utc(item.start_at) < end_at and _db_utc(item.end_at) > start_at
        )
        overlaps_fact = item.id in active_lesson_ids
        if item.room_id == room_id:
            result.append(
                {
                    "kind": "room",
                    "lesson_id": item.id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Кабинет уже занят",
                }
            )
        if overlaps_plan and item.teacher_id == teacher_id:
            result.append(
                {
                    "kind": "teacher",
                    "lesson_id": item.id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Учитель уже занят",
                }
            )
        if overlaps_plan and item.teacher_id in participant_ids:
            result.append(
                {
                    "kind": "person",
                    "lesson_id": item.id,
                    "person_id": item.teacher_id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Участник занят как преподаватель",
                }
            )
        if (overlaps_plan or overlaps_fact) and teacher_id in busy_students.get(item.id, set()):
            result.append(
                {
                    "kind": "person",
                    "lesson_id": item.id,
                    "person_id": teacher_id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Преподаватель занят как участник",
                }
            )
        for person_id in sorted(busy_students.get(item.id, set())):
            if person_id == teacher_id:
                continue
            result.append(
                {
                    "kind": "student",
                    "lesson_id": item.id,
                    "person_id": person_id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Ученик уже занят",
                }
            )
    existing_keys = {
        (entry.get("kind"), entry.get("lesson_id"), entry.get("person_id")) for entry in result
    }
    for segment, lesson in segment_rows:
        if segment.teacher_person_id not in checked_people:
            continue
        kind = "teacher" if segment.teacher_person_id == teacher_id else "person"
        key = (kind, lesson.id, None if kind == "teacher" else segment.teacher_person_id)
        if key in existing_keys:
            continue
        result.append(
            {
                "kind": kind,
                "lesson_id": lesson.id,
                "person_id": segment.teacher_person_id,
                "start_at": _db_utc(segment.started_at).isoformat(),
                "message": "Человек уже занят как фактический преподаватель",
            }
        )
    return result


def _lesson_view(
    item: Lesson,
    participants: list[LessonParticipant],
    teacher_segments: list[LessonTeacherSegment] | None = None,
) -> dict[str, Any]:
    now = utcnow()
    ready = bool(participants) and all(p.attendance_status != "expected" for p in participants)
    computed = item.status
    if item.status in {"planned", "scheduled"} and _db_utc(item.start_at) <= now < _db_utc(
        item.end_at
    ):
        computed = "scheduled"
    return {
        **_model(
            item,
            "id",
            "series_id",
            "series_occurrence_index",
            "series_exception",
            "subject_id",
            "teacher_id",
            "room_id",
            "group_id",
            "start_at",
            "end_at",
            "actual_start_at",
            "actual_end_at",
            "status",
            "cancelled_reason",
            "completion_type",
            "completion_reason",
            "completion_public_comment",
            "teacher_name_snapshot",
            "room_name_snapshot",
            "subject_name_snapshot",
            "notes",
            "created_at",
            "updated_at",
        ),
        "computed_status": computed,
        "ready": ready,
        "active_participant_count": sum(
            participant.attendance_status != "excused" for participant in participants
        ),
        "excused_participant_count": sum(
            participant.attendance_status == "excused" for participant in participants
        ),
        "teacher_segments": [
            _model(
                segment,
                "id",
                "teacher_person_id",
                "teacher_name_snapshot",
                "started_at",
                "ended_at",
                "segment_type",
                "reason",
            )
            for segment in (teacher_segments or [])
        ],
        "participants": [
            _model(
                p,
                "id",
                "person_id",
                "person_name_snapshot",
                "attendance_status",
                "arrived_at",
                "left_at",
                "late_minutes",
                "note",
                "cancelled_at",
                "cancelled_by",
                "cancelled_by_person_id",
                "cancelled_by_admin_id",
                "cancellation_reason",
                "early_leave_reason",
            )
            for p in participants
        ],
    }


async def _queue_lesson_notifications(
    session: AsyncSession,
    lesson: Lesson,
    participants: list[LessonParticipant],
    center_timezone: str = "Asia/Yekaterinburg",
) -> None:
    # All reminders are reconciled as daily bundles in the same persistent
    # outbox.  Keeping the former per-lesson producer would send duplicates.
    from .communications import (
        reconcile_confirmation_requests,
        reconcile_daily_reminders,
    )

    timezone = _display_timezone(center_timezone)
    await reconcile_daily_reminders(session, now=utcnow(), timezone=timezone)
    await reconcile_confirmation_requests(session, now=utcnow(), timezone=timezone)


async def _queue_communication_event(
    session: AsyncSession,
    *,
    dedupe_key: str,
    event_type: str,
    recipient_person_id: int,
    recipient_context: str,
    subject_person_id: int | None,
    text_value: str,
    center_timezone: str,
    lesson_id: int | None = None,
    scheduled_at: datetime | None = None,
) -> None:
    from .communications import (
        PRIORITY_VALUE,
        NotificationPolicyResolver,
        apply_quiet_hours,
        enqueue_job,
    )

    policy = await NotificationPolicyResolver(session).resolve(
        recipient_person_id=recipient_person_id,
        recipient_context=recipient_context,
        subject_person_id=subject_person_id,
        event_code=event_type,
    )
    if not policy.enabled:
        return
    due = apply_quiet_hours(
        scheduled_at or utcnow(),
        policy=policy,
        timezone=_display_timezone(center_timezone),
    )
    if due is None:
        return
    await enqueue_job(
        session,
        dedupe_key=dedupe_key,
        event_type=event_type,
        lesson_id=lesson_id,
        recipient_person_id=recipient_person_id,
        recipient_context=recipient_context,
        subject_person_id=subject_person_id,
        priority=PRIORITY_VALUE[policy.priority],
        scheduled_at=due,
        payload={
            "text": text_value,
            "recipient_context": recipient_context,
            "subject_person_id": subject_person_id,
        },
    )


async def _queue_lesson_state_notifications(
    session: AsyncSession,
    lesson: Lesson,
    participants: list[LessonParticipant],
    *,
    event: Literal["started", "participant_started", "finished"],
    center_timezone: str,
) -> None:
    if event in {"started", "participant_started"}:
        active = [item for item in participants if item.attendance_status in {"present", "late"}]
    else:
        active = [
            item
            for item in participants
            if item.attendance_status in {"present", "late", "left_early"}
            or item.arrived_at is not None
        ]
    ids = {item.person_id for item in active}
    if not ids:
        return
    people = {
        item.id: item
        for item in (await session.scalars(select(Person).where(Person.id.in_(ids)))).all()
    }
    links = (
        await session.execute(
            select(StudentGuardian.student_id, StudentGuardian.guardian_id).where(
                StudentGuardian.student_id.in_(ids)
            )
        )
    ).all()
    guardians: dict[int, set[int]] = {}
    for student_id, guardian_id in links:
        if guardian_id != student_id:
            guardians.setdefault(student_id, set()).add(guardian_id)
    actual_start = _db_utc(lesson.actual_start_at or utcnow()).astimezone(
        _display_timezone(center_timezone)
    )
    actual_end = _db_utc(lesson.actual_end_at or utcnow()).astimezone(
        _display_timezone(center_timezone)
    )
    for participant in active:
        person = people.get(participant.person_id)
        if person is None:
            continue
        if event in {"started", "participant_started"}:
            student_text = (
                (
                    "Вы приступили к занятию\n\n"
                    if event == "participant_started"
                    else "Занятие началось\n\n"
                )
                + f"Предмет: {lesson.subject_name_snapshot}\n"
                f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                f"Кабинет: {lesson.room_name_snapshot}"
            )
        else:
            heading = (
                "Занятие завершено досрочно"
                if lesson.completion_type == "early"
                else "Занятие завершено"
            )
            public_comment = (
                f"\nКомментарий: {lesson.completion_public_comment}"
                if lesson.completion_public_comment
                else ""
            )
            student_text = (
                f"{heading}\n\n"
                f"{lesson.subject_name_snapshot}\n"
                f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                f"Фактически: {actual_start:%H:%M}–{actual_end:%H:%M}"
                f"{public_comment}"
            )
        jobs = [(f"student:{person.id}", person.id, "student", student_text)]
        name_parts = person.full_name.split()
        first_name = name_parts[1] if len(name_parts) > 1 else person.full_name
        for guardian_id in guardians.get(person.id, set()):
            if event in {"started", "participant_started"}:
                text_value = (
                    f"{first_name} приступил(а) к занятию.\n\n"
                    f"Предмет: {lesson.subject_name_snapshot}\n"
                    f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                    f"Кабинет: {lesson.room_name_snapshot}"
                )
            else:
                heading = (
                    f"Занятие {first_name} завершено досрочно."
                    if lesson.completion_type == "early"
                    else f"Занятие {first_name} завершено."
                )
                public_comment = (
                    f"\nКомментарий: {lesson.completion_public_comment}"
                    if lesson.completion_public_comment
                    else ""
                )
                text_value = (
                    f"{heading}\n\n"
                    f"{lesson.subject_name_snapshot}\n"
                    f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                    f"Фактически: {actual_start:%H:%M}–{actual_end:%H:%M}"
                    f"{public_comment}"
                )
            jobs.append(
                (
                    f"guardian:{guardian_id}:student:{person.id}",
                    guardian_id,
                    "guardian",
                    text_value,
                )
            )
        normalized_event = (
            "lesson_participant_started" if event == "participant_started" else f"lesson_{event}"
        )
        for suffix, recipient_id, recipient_context, text_value in jobs:
            key = f"lesson:{lesson.id}:{event}:{suffix}"
            await _queue_communication_event(
                session,
                dedupe_key=key,
                event_type=normalized_event,
                lesson_id=lesson.id,
                recipient_person_id=recipient_id,
                recipient_context=recipient_context,
                subject_person_id=person.id,
                text_value=text_value,
                center_timezone=center_timezone,
            )


async def _queue_guardian_event(
    session: AsyncSession,
    *,
    person: Person,
    event_type: str,
    occurred_at: datetime,
    center_timezone: str = "Asia/Yekaterinburg",
) -> None:
    guardian_ids = set(
        (
            await session.scalars(
                select(StudentGuardian.guardian_id).where(
                    StudentGuardian.student_id == person.id,
                    StudentGuardian.guardian_id != person.id,
                )
            )
        ).all()
    )
    verb = "прибыл(а) в КРиТ" if event_type == "arrival" else "покинул(а) КРиТ"
    local_occurred = occurred_at.astimezone(_display_timezone(center_timezone))
    for guardian_id in guardian_ids:
        key = f"presence:{event_type}:{person.id}:{occurred_at.isoformat()}:person:{guardian_id}"
        normalized_event = (
            "student_arrived_club" if event_type == "arrival" else "student_left_club"
        )
        await _queue_communication_event(
            session,
            dedupe_key=key,
            event_type=normalized_event,
            recipient_person_id=guardian_id,
            recipient_context="guardian",
            subject_person_id=person.id,
            scheduled_at=occurred_at,
            text_value=(
                f"{person.full_name} {verb}\n\n"
                f"{local_occurred:%d.%m.%Y}\n{local_occurred:%H:%M}"
            ),
            center_timezone=center_timezone,
        )


async def _ensure_admin_notifications(session: AsyncSession, now: datetime) -> None:
    window_start = now - timedelta(minutes=10)
    window_end = now + timedelta(minutes=10)
    lessons = list(
        (
            await session.scalars(
                select(Lesson).where(
                    Lesson.status.in_(["planned", "scheduled", "in_progress"]),
                    Lesson.start_at < window_end,
                    Lesson.end_at > window_start - timedelta(hours=12),
                )
            )
        ).all()
    )
    for lesson in lessons:
        start_at, end_at = _db_utc(lesson.start_at), _db_utc(lesson.end_at)
        kind: str | None = None
        title = ""
        message = ""
        if lesson.status in {"planned", "scheduled"} and now < start_at <= now + timedelta(
            minutes=10
        ):
            kind = "lesson_starts_soon"
            arrived = await session.scalar(
                select(func.count(LessonParticipant.id))
                .join(
                    ClubPresenceSession,
                    ClubPresenceSession.person_id == LessonParticipant.person_id,
                )
                .where(
                    LessonParticipant.lesson_id == lesson.id,
                    LessonParticipant.attendance_status != "excused",
                    ClubPresenceSession.left_at.is_(None),
                )
            )
            expected = await session.scalar(
                select(func.count(LessonParticipant.id)).where(
                    LessonParticipant.lesson_id == lesson.id,
                    LessonParticipant.attendance_status != "excused",
                )
            )
            title = "Занятие через 10 минут"
            message = (
                f"{lesson.subject_name_snapshot}\n"
                f"Кабинет {lesson.room_name_snapshot}\n"
                f"Прибыли {int(arrived or 0)} из {int(expected or 0)}"
            )
        elif lesson.status in {"planned", "scheduled"} and start_at + timedelta(minutes=5) <= now:
            kind = "lesson_start_overdue"
            title = "Занятие не начато"
            message = (
                f"{lesson.subject_name_snapshot}\n"
                f"Кабинет {lesson.room_name_snapshot}\n"
                "Плановое время начала прошло"
            )
        elif lesson.status == "in_progress" and end_at + timedelta(minutes=5) <= now:
            kind = "lesson_finish_overdue"
            title = "Занятие не завершено"
            message = (
                f"{lesson.subject_name_snapshot}\n"
                f"Кабинет {lesson.room_name_snapshot}\n"
                "Плановое время окончания прошло"
            )
        if kind is None:
            continue
        suffix = {
            "lesson_starts_soon": "starting_soon",
            "lesson_start_overdue": "not_started",
            "lesson_finish_overdue": "not_finished",
        }[kind]
        if kind in {"lesson_start_overdue", "lesson_finish_overdue"}:
            await _raise_admin_condition(
                session,
                condition_key=f"lesson:{lesson.id}:{suffix}",
                kind=kind,
                title=title,
                message=message,
                lesson_id=lesson.id,
            )
        else:
            await _add_admin_notification(
                session,
                dedupe_key=f"lesson:{lesson.id}:{suffix}",
                kind=kind,
                title=title,
                message=message,
                lesson_id=lesson.id,
            )


def create_learning_router(
    sessions: async_sessionmaker[AsyncSession],
    require_management_token: Callable[..., Any],
    center_timezone: str = "Asia/Yekaterinburg",
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/learning",
        tags=["learning"],
        dependencies=[Depends(require_management_token)],
    )

    @router.get("/reference-data")
    async def reference_data() -> dict[str, Any]:
        async with sessions() as session:
            subjects = list((await session.scalars(select(Subject).order_by(Subject.name))).all())
            rooms = list((await session.scalars(select(Room).order_by(Room.name))).all())
            groups = list(
                (await session.scalars(select(StudyGroup).order_by(StudyGroup.name))).all()
            )
            teachers = list(
                (
                    await session.scalars(
                        select(Person)
                        .join(PersonRole)
                        .where(PersonRole.role == "teacher", Person.archived_at.is_(None))
                        .order_by(Person.full_name)
                    )
                ).all()
            )
            students = list(
                (
                    await session.scalars(
                        select(Person)
                        .join(PersonRole)
                        .where(PersonRole.role == "student", Person.archived_at.is_(None))
                        .order_by(Person.full_name)
                    )
                ).all()
            )
            subject_teacher_rows = (
                await session.execute(select(SubjectTeacher.subject_id, SubjectTeacher.teacher_id))
            ).all()
            guardian_rows = (
                await session.execute(
                    select(
                        StudentGuardian.student_id,
                        Person.id,
                        Person.full_name,
                    )
                    .join(Person, Person.id == StudentGuardian.guardian_id)
                    .where(Person.archived_at.is_(None))
                )
            ).all()
            membership_rows = (
                await session.execute(
                    select(
                        GroupMembership.group_id,
                        GroupMembership.person_id,
                        GroupMembership.start_at,
                        GroupMembership.end_at,
                    ).order_by(GroupMembership.group_id, GroupMembership.start_at)
                )
            ).all()
        teachers_by_subject: dict[int, list[int]] = {}
        for subject_id, teacher_id in subject_teacher_rows:
            teachers_by_subject.setdefault(int(subject_id), []).append(int(teacher_id))
        guardians_by_student: dict[int, list[dict[str, Any]]] = {}
        for student_id, guardian_id, full_name in guardian_rows:
            guardians_by_student.setdefault(int(student_id), []).append(
                {"id": int(guardian_id), "full_name": str(full_name)}
            )
        memberships_by_group: dict[int, list[dict[str, Any]]] = {}
        for group_id, person_id, start_at, end_at in membership_rows:
            memberships_by_group.setdefault(int(group_id), []).append(
                {
                    "person_id": int(person_id),
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat() if end_at else None,
                }
            )
        return {
            "subjects": [
                {
                    **_model(x, "id", "name", "color", "active"),
                    "teacher_ids": sorted(teachers_by_subject.get(x.id, [])),
                }
                for x in subjects
            ],
            "rooms": [_model(x, "id", "name", "capacity", "active") for x in rooms],
            "groups": [
                {
                    **_model(
                        x,
                        "id",
                        "name",
                        "subject_id",
                        "default_teacher_id",
                        "default_duration_minutes",
                        "active",
                    ),
                    "memberships": memberships_by_group.get(x.id, []),
                }
                for x in groups
            ],
            "teachers": [_model(x, "id", "full_name", "phone", "active") for x in teachers],
            "students": [
                {
                    **_model(x, "id", "full_name", "phone", "active"),
                    "guardians": guardians_by_student.get(x.id, []),
                }
                for x in students
            ],
        }

    @router.post("/subjects", status_code=status.HTTP_201_CREATED)
    async def create_subject(payload: SubjectPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = Subject(
                name=" ".join(payload.name.split()), color=payload.color, active=payload.active
            )
            session.add(item)
            try:
                await session.flush()
                teacher_ids = await _replace_subject_teachers(
                    session, item.id, payload.teacher_ids or []
                )
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(409, "Предмет с таким названием уже существует") from exc
            return {
                **_model(item, "id", "name", "color", "active"),
                "teacher_ids": teacher_ids,
            }

    @router.put("/subjects/{item_id}")
    async def update_subject(item_id: int, payload: SubjectPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Subject, item_id)
            if item is None:
                raise HTTPException(404)
            normalized_name = " ".join(payload.name.split())
            duplicate = await session.scalar(
                select(Subject.id).where(
                    Subject.id != item_id,
                    Subject.name == normalized_name,
                )
            )
            if duplicate is not None:
                raise HTTPException(409, "Предмет с таким названием уже существует")
            item.name, item.color, item.active, item.updated_at = (
                normalized_name,
                payload.color,
                payload.active,
                utcnow(),
            )
            if payload.teacher_ids is None:
                teacher_ids = sorted(
                    (
                        await session.scalars(
                            select(SubjectTeacher.teacher_id).where(
                                SubjectTeacher.subject_id == item.id
                            )
                        )
                    ).all()
                )
            else:
                requested_ids = set(payload.teacher_ids)
                invalid_group = await session.scalar(
                    select(StudyGroup.id).where(
                        StudyGroup.subject_id == item.id,
                        StudyGroup.default_teacher_id.is_not(None),
                        StudyGroup.default_teacher_id.not_in(requested_ids)
                        if requested_ids
                        else StudyGroup.default_teacher_id.is_not(None),
                    )
                )
                if invalid_group is not None:
                    raise HTTPException(
                        409,
                        "Сначала измените преподавателя по умолчанию в связанных группах",
                    )
                teacher_ids = await _replace_subject_teachers(session, item.id, payload.teacher_ids)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Предмет с таким названием уже существует") from exc
            return {
                **_model(item, "id", "name", "color", "active"),
                "teacher_ids": teacher_ids,
            }

    @router.post("/rooms", status_code=status.HTTP_201_CREATED)
    async def create_room(payload: RoomPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = Room(
                name=" ".join(payload.name.split()),
                capacity=payload.capacity,
                active=payload.active,
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(409, "Кабинет с таким названием уже существует") from exc
            return _model(item, "id", "name", "capacity", "active")

    @router.put("/rooms/{item_id}")
    async def update_room(item_id: int, payload: RoomPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Room, item_id)
            if item is None:
                raise HTTPException(404)
            normalized_name = " ".join(payload.name.split())
            duplicate = await session.scalar(
                select(Room.id).where(Room.id != item_id, Room.name == normalized_name)
            )
            if duplicate is not None:
                raise HTTPException(409, "Кабинет с таким названием уже существует")
            item.name, item.capacity, item.active, item.updated_at = (
                normalized_name,
                payload.capacity,
                payload.active,
                utcnow(),
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Кабинет с таким названием уже существует") from exc
            return _model(item, "id", "name", "capacity", "active")

    @router.post("/groups", status_code=status.HTTP_201_CREATED)
    async def create_group(payload: GroupPayload) -> dict[str, Any]:
        async with sessions() as session:
            if payload.subject_id and await session.get(Subject, payload.subject_id) is None:
                raise HTTPException(422, "Предмет не найден")
            if payload.default_teacher_id is not None:
                if payload.subject_id is None:
                    raise HTTPException(422, "Для преподавателя группы выберите предмет")
                teacher = await session.get(Person, payload.default_teacher_id)
                teacher_role = await session.scalar(
                    select(PersonRole.person_id).where(
                        PersonRole.person_id == payload.default_teacher_id,
                        PersonRole.role == "teacher",
                    )
                )
                if (
                    teacher_role is None
                    or teacher is None
                    or not teacher.active
                    or teacher.archived_at is not None
                ):
                    raise HTTPException(422, "Преподаватель группы не найден")
                if (
                    await session.get(
                        SubjectTeacher,
                        {
                            "subject_id": payload.subject_id,
                            "teacher_id": payload.default_teacher_id,
                        },
                    )
                    is None
                ):
                    raise HTTPException(422, "Преподаватель не закреплён за предметом группы")
            item = StudyGroup(
                name=" ".join(payload.name.split()),
                subject_id=payload.subject_id,
                default_teacher_id=payload.default_teacher_id,
                default_duration_minutes=payload.default_duration_minutes,
                active=payload.active,
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(409, "Группа с таким названием уже существует") from exc
            return _model(
                item,
                "id",
                "name",
                "subject_id",
                "default_teacher_id",
                "default_duration_minutes",
                "active",
            )

    @router.put("/groups/{item_id}")
    async def update_group(item_id: int, payload: GroupPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(StudyGroup, item_id)
            if item is None:
                raise HTTPException(404)
            normalized_name = " ".join(payload.name.split())
            duplicate = await session.scalar(
                select(StudyGroup.id).where(
                    StudyGroup.id != item_id,
                    StudyGroup.name == normalized_name,
                )
            )
            if duplicate is not None:
                raise HTTPException(409, "Группа с таким названием уже существует")
            if payload.default_teacher_id is not None:
                if payload.subject_id is None:
                    raise HTTPException(422, "Для преподавателя группы выберите предмет")
                teacher = await session.get(Person, payload.default_teacher_id)
                teacher_role = await session.scalar(
                    select(PersonRole.person_id).where(
                        PersonRole.person_id == payload.default_teacher_id,
                        PersonRole.role == "teacher",
                    )
                )
                if (
                    teacher_role is None
                    or teacher is None
                    or not teacher.active
                    or teacher.archived_at is not None
                ):
                    raise HTTPException(422, "Преподаватель группы не найден")
                if (
                    await session.get(
                        SubjectTeacher,
                        {
                            "subject_id": payload.subject_id,
                            "teacher_id": payload.default_teacher_id,
                        },
                    )
                    is None
                ):
                    raise HTTPException(422, "Преподаватель не закреплён за предметом группы")
            item.name, item.subject_id, item.default_teacher_id = (
                normalized_name,
                payload.subject_id,
                payload.default_teacher_id,
            )
            item.default_duration_minutes, item.active, item.updated_at = (
                payload.default_duration_minutes,
                payload.active,
                utcnow(),
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Группа с таким названием уже существует") from exc
            return _model(
                item,
                "id",
                "name",
                "subject_id",
                "default_teacher_id",
                "default_duration_minutes",
                "active",
            )

    @router.get("/groups/{group_id}/memberships")
    async def group_memberships(group_id: int) -> list[dict[str, Any]]:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(GroupMembership, Person)
                    .join(Person, Person.id == GroupMembership.person_id)
                    .where(GroupMembership.group_id == group_id)
                    .order_by(GroupMembership.start_at.desc())
                )
            ).all()
            return [
                {
                    **_model(link, "id", "person_id", "start_at", "end_at"),
                    "person_name": person.full_name,
                }
                for link, person in rows
            ]

    @router.post("/groups/{group_id}/memberships", status_code=status.HTTP_201_CREATED)
    async def add_membership(group_id: int, payload: MembershipPayload) -> dict[str, Any]:
        async with sessions() as session:
            start_at = _aware(payload.start_at)
            end_at = _aware(payload.end_at) if payload.end_at else None
            if end_at is not None and end_at <= start_at:
                raise HTTPException(422, "Дата окончания должна быть позже даты начала")
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                lock_key = (group_id << 32) ^ payload.person_id
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )
            group = await session.get(StudyGroup, group_id)
            person = await session.get(Person, payload.person_id)
            if group is None or person is None:
                raise HTTPException(404)
            student_role = await session.scalar(
                select(PersonRole.person_id).where(
                    PersonRole.person_id == payload.person_id,
                    PersonRole.role == "student",
                )
            )
            if student_role is None or not person.active or person.archived_at is not None:
                raise HTTPException(422, "В группу можно добавить только активного ученика")
            overlap = await session.scalar(
                select(GroupMembership.id).where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.person_id == payload.person_id,
                    GroupMembership.start_at < (end_at or datetime.max.replace(tzinfo=UTC)),
                    or_(
                        GroupMembership.end_at.is_(None), GroupMembership.end_at > start_at
                    ),
                )
            )
            if overlap is not None:
                raise HTTPException(409, "Период участия пересекается с существующим")
            item = GroupMembership(
                group_id=group_id,
                person_id=payload.person_id,
                start_at=start_at,
                end_at=end_at,
            )
            session.add(item)
            await session.commit()
            return _model(item, "id", "group_id", "person_id", "start_at", "end_at")

    @router.post("/groups/{group_id}/memberships/{membership_id}/end")
    async def end_membership(
        group_id: int,
        membership_id: int,
        payload: MembershipEndPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(GroupMembership, membership_id, with_for_update=True)
            if item is None or item.group_id != group_id:
                raise HTTPException(404)
            if item.end_at is None:
                end_at = _aware(payload.end_at) if payload.end_at else utcnow()
                if end_at <= _db_utc(item.start_at):
                    raise HTTPException(422, "Дата окончания должна быть позже даты начала")
                item.end_at = end_at
                session.add(
                    AuditEvent(
                        actor_admin_id=admin_id,
                        action="group_membership.ended",
                        entity_type="group_membership",
                        entity_id=item.id,
                    )
                )
                await session.commit()
            return _model(item, "id", "group_id", "person_id", "start_at", "end_at")

    @router.get("/lessons")
    async def list_lessons(
        date_from: datetime,
        date_to: datetime,
        teacher_id: int | None = None,
        student_id: int | None = None,
        room_id: int | None = None,
        group_id: int | None = None,
    ) -> list[dict[str, Any]]:
        start, end = _aware(date_from), _aware(date_to)
        async with sessions() as session:
            query = select(Lesson).where(Lesson.start_at < end, Lesson.end_at > start)
            if teacher_id is not None:
                query = query.where(Lesson.teacher_id == teacher_id)
            if room_id is not None:
                query = query.where(Lesson.room_id == room_id)
            if group_id is not None:
                query = query.where(Lesson.group_id == group_id)
            if student_id is not None:
                query = query.where(
                    Lesson.id.in_(
                        select(LessonParticipant.lesson_id).where(
                            LessonParticipant.person_id == student_id,
                            LessonParticipant.attendance_status != "excused",
                        )
                    )
                )
            items = list((await session.scalars(query.order_by(Lesson.start_at))).all())
            ids = [x.id for x in items]
            participants = (
                list(
                    (
                        await session.scalars(
                            select(LessonParticipant).where(LessonParticipant.lesson_id.in_(ids))
                        )
                    ).all()
                )
                if ids
                else []
            )
            by_lesson: dict[int, list[LessonParticipant]] = {}
            for participant in participants:
                by_lesson.setdefault(participant.lesson_id, []).append(participant)
            return [_lesson_view(item, by_lesson.get(item.id, [])) for item in items]

    @router.get("/today")
    async def today(
        day: date | None = None, timezone_offset_minutes: int = Query(default=300, ge=-720, le=840)
    ) -> dict[str, Any]:
        try:
            tz = ZoneInfo(center_timezone)
        except ZoneInfoNotFoundError:
            tz = timezone(timedelta(minutes=timezone_offset_minutes))
        selected = day or datetime.now(tz).date()
        start = datetime.combine(selected, time.min, tzinfo=tz).astimezone(UTC)
        end = start + timedelta(days=1)
        async with sessions() as session:
            await _ensure_admin_notifications(session, utcnow())
            await session.flush()
            lessons = list(
                (
                    await session.scalars(
                        select(Lesson)
                        .where(Lesson.start_at < end, Lesson.end_at > start)
                        .order_by(Lesson.start_at)
                    )
                ).all()
            )
            ids = [x.id for x in lessons]
            participants = (
                list(
                    (
                        await session.scalars(
                            select(LessonParticipant).where(LessonParticipant.lesson_id.in_(ids))
                        )
                    ).all()
                )
                if ids
                else []
            )
            present = (
                await session.execute(
                    select(ClubPresenceSession, Person)
                    .join(Person)
                    .where(
                        ClubPresenceSession.left_at.is_(None),
                        ClubPresenceSession.arrived_at >= start,
                    )
                    .order_by(ClubPresenceSession.arrived_at)
                )
            ).all()
            stale_presence = (
                await session.execute(
                    select(ClubPresenceSession, Person)
                    .join(Person)
                    .where(
                        ClubPresenceSession.left_at.is_(None),
                        ClubPresenceSession.arrived_at < start,
                    )
                    .order_by(ClubPresenceSession.arrived_at)
                )
            ).all()
            for presence, person in stale_presence:
                await _raise_admin_condition(
                    session,
                    condition_key=f"presence:{presence.id}:stale",
                    kind="stale_presence",
                    title="Не отмечен уход",
                    message=(
                        f"{person.full_name}\n"
                        f"Приход: {_db_utc(presence.arrived_at).astimezone(tz):%d.%m.%Y %H:%M}"
                    ),
                    lesson_id=None,
                )
            alerts = list(
                (
                    await session.scalars(
                        select(AdminNotification)
                        .where(AdminNotification.read_at.is_(None))
                        .order_by(AdminNotification.created_at.desc())
                        .limit(50)
                    )
                ).all()
            )
            alerts = _unique_admin_notifications(alerts)
            await session.commit()
        by_lesson: dict[int, list[LessonParticipant]] = {}
        for participant in participants:
            by_lesson.setdefault(participant.lesson_id, []).append(participant)
        return {
            "date": selected.isoformat(),
            "lessons": [_lesson_view(x, by_lesson.get(x.id, [])) for x in lessons],
            "present": [
                {
                    **_model(presence, "id", "person_id", "arrived_at"),
                    "person_name": person.full_name,
                }
                for presence, person in present
            ],
            "stale_presence": [
                {
                    **_model(presence, "id", "person_id", "arrived_at"),
                    "person_name": person.full_name,
                }
                for presence, person in stale_presence
            ],
            "alerts": [
                _model(x, "id", "kind", "title", "message", "lesson_id", "created_at")
                for x in alerts
            ],
        }

    @router.get("/lesson/{lesson_id}")
    async def get_lesson(lesson_id: int) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Lesson, lesson_id)
            if item is None:
                raise HTTPException(404)
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            return _lesson_view(item, participants, await _teacher_segments(session, lesson_id))

    async def save_lesson(
        session: AsyncSession,
        payload: LessonPayload,
        *,
        admin_id: int | None = None,
        series_id: int | None = None,
        series_occurrence_index: int | None = None,
        exclude_id: int | None = None,
        conflict_exclude_ids: set[int] | None = None,
        series_edit: bool = False,
    ) -> Lesson:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            # Serializes schedule writes only. This closes the empty-result race
            # that SELECT FOR UPDATE cannot prevent between concurrent admins.
            await session.execute(text("SELECT pg_advisory_xact_lock(12636884)"))
        start_at, end_at = _aware(payload.start_at), _aware(payload.end_at)
        subject, teacher, room, _ = await _references(session, payload)
        participants = await _participants_for(session, payload, start_at)
        if len(participants) > room.capacity:
            raise HTTPException(
                409,
                {
                    "message": "Вместимость кабинета недостаточна",
                    "kind": "capacity",
                    "capacity": room.capacity,
                    "participants": len(participants),
                },
            )
        conflicts = await _conflicts(
            session,
            start_at=start_at,
            end_at=end_at,
            teacher_id=teacher.id,
            room_id=room.id,
            participant_ids={p.id for p in participants},
            exclude_lesson_id=exclude_id,
            exclude_lesson_ids=conflict_exclude_ids,
        )
        if conflicts:
            raise HTTPException(
                409, {"message": "Обнаружены конфликты расписания", "conflicts": conflicts}
            )
        if exclude_id is None:
            item = Lesson(
                series_id=series_id,
                series_occurrence_index=series_occurrence_index,
                subject_id=subject.id,
                teacher_id=teacher.id,
                room_id=room.id,
                group_id=payload.group_id,
                start_at=start_at,
                end_at=end_at,
                status="planned",
                teacher_name_snapshot=teacher.full_name,
                room_name_snapshot=room.name,
                subject_name_snapshot=subject.name,
                notes=payload.notes,
            )
            session.add(item)
            await session.flush()
            participant_links = [
                LessonParticipant(
                    lesson_id=item.id,
                    person_id=person.id,
                    person_name_snapshot=person.full_name,
                )
                for person in participants
            ]
            session.add_all(participant_links)
        else:
            item = await session.get(Lesson, exclude_id)
            if item is None:
                raise HTTPException(404)
            if item.status not in {"planned", "scheduled"}:
                raise HTTPException(
                    409,
                    {
                        "code": "lesson_structural_update_forbidden",
                        "message": "План занятия можно изменять только до его начала",
                        "status": item.status,
                    },
                )
            if item.series_id is not None and not series_edit:
                item.series_exception = True
            current_participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == item.id)
                    )
                ).all()
            )
            previous_participant_ids = {
                entry.person_id
                for entry in current_participants
                if entry.attendance_status != "excused"
            }
            semantic_change = any(
                (
                    item.subject_id != subject.id,
                    item.teacher_id != teacher.id,
                    item.room_id != room.id,
                    item.group_id != payload.group_id,
                    _db_utc(item.start_at) != start_at,
                    _db_utc(item.end_at) != end_at,
                    previous_participant_ids != {person.id for person in participants},
                )
            )
            if semantic_change:
                item.notification_revision += 1
                from .communication_models import LessonAttendanceIntent

                await session.execute(
                    LessonAttendanceIntent.__table__.update()
                    .where(
                        LessonAttendanceIntent.lesson_id == item.id,
                        LessonAttendanceIntent.status.in_(
                            ["pending", "confirmed", "declined", "conflict"]
                        ),
                    )
                    .values(status="needs_reconfirmation", updated_at=utcnow())
                )
            item.subject_id, item.teacher_id, item.room_id, item.group_id = (
                subject.id,
                teacher.id,
                room.id,
                payload.group_id,
            )
            item.start_at, item.end_at, item.notes, item.updated_at = (
                start_at,
                end_at,
                payload.notes,
                utcnow(),
            )
            item.teacher_name_snapshot, item.room_name_snapshot, item.subject_name_snapshot = (
                teacher.full_name,
                room.name,
                subject.name,
            )
            current_by_person = {entry.person_id: entry for entry in current_participants}
            desired_by_person = {person.id: person for person in participants}
            for person_id, participant in current_by_person.items():
                if person_id in desired_by_person:
                    continue
                has_history = any(
                    (
                        participant.attendance_status != "expected",
                        participant.arrived_at is not None,
                        participant.left_at is not None,
                        participant.cancelled_at is not None,
                        participant.cancellation_reason is not None,
                        participant.early_leave_reason is not None,
                    )
                )
                if has_history:
                    continue
                session.add(
                    AuditEvent(
                        actor_admin_id=admin_id,
                        action="participant.plan_removed",
                        entity_type="lesson_participant",
                        entity_id=participant.id,
                        details={
                            "lesson_id": item.id,
                            "person_id": participant.person_id,
                            "before": {"attendance_status": participant.attendance_status},
                        },
                    )
                )
                await session.delete(participant)
            participant_links = [
                entry
                for entry in current_participants
                if entry.person_id in desired_by_person
                or entry.attendance_status != "expected"
                or entry.arrived_at is not None
                or entry.left_at is not None
                or entry.cancelled_at is not None
            ]
            for person_id, person in desired_by_person.items():
                if person_id in current_by_person:
                    continue
                link = LessonParticipant(
                    lesson_id=item.id,
                    person_id=person.id,
                    person_name_snapshot=person.full_name,
                )
                session.add(link)
                participant_links.append(link)
            await session.execute(
                NotificationJob.__table__.update()
                .where(
                    NotificationJob.lesson_id == item.id,
                    NotificationJob.status.in_(["pending", "retry"]),
                )
                .values(status="cancelled")
            )
        await session.flush()
        await _queue_lesson_notifications(session, item, participant_links, center_timezone)
        return item

    @router.post("/lessons/conflicts")
    async def check_conflicts(payload: LessonPayload) -> dict[str, Any]:
        async with sessions() as session:
            _, teacher, room, _ = await _references(session, payload)
            people = await _participants_for(session, payload, _aware(payload.start_at))
            conflicts = await _conflicts(
                session,
                start_at=_aware(payload.start_at),
                end_at=_aware(payload.end_at),
                teacher_id=teacher.id,
                room_id=room.id,
                participant_ids={x.id for x in people},
            )
            if len(people) > room.capacity:
                conflicts.append(
                    {"kind": "capacity", "message": "Вместимость кабинета недостаточна"}
                )
            return {"available": not conflicts, "conflicts": conflicts}

    @router.post("/lessons", status_code=status.HTTP_201_CREATED)
    async def create_lesson(
        payload: LessonPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await save_lesson(session, payload, admin_id=admin_id)
            item.created_by_admin_id = admin_id
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.created",
                    entity_type="lesson",
                    entity_id=item.id,
                )
            )
            await session.commit()
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == item.id)
                    )
                ).all()
            )
            return _lesson_view(item, participants)

    @router.put("/lessons/{lesson_id}")
    async def update_lesson(
        lesson_id: int,
        payload: LessonPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            existing = await session.get(Lesson, lesson_id)
            if existing is None:
                raise HTTPException(404)
            existing_participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            before = {
                "subject_id": existing.subject_id,
                "teacher_id": existing.teacher_id,
                "room_id": existing.room_id,
                "group_id": existing.group_id,
                "start_at": existing.start_at.isoformat(),
                "end_at": existing.end_at.isoformat(),
                "participant_ids": [item.person_id for item in existing_participants],
                "notes": existing.notes,
            }
            item = await save_lesson(session, payload, admin_id=admin_id, exclude_id=lesson_id)
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.updated",
                    entity_type="lesson",
                    entity_id=item.id,
                    details={
                        "before": before,
                        "after": {
                            "subject_id": payload.subject_id,
                            "teacher_id": payload.teacher_id,
                            "room_id": payload.room_id,
                            "group_id": payload.group_id,
                            "start_at": _aware(payload.start_at).isoformat(),
                            "end_at": _aware(payload.end_at).isoformat(),
                            "participant_ids": payload.participant_ids,
                            "notes": payload.notes,
                        },
                    },
                )
            )
            await session.commit()
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == item.id)
                    )
                ).all()
            )
            return _lesson_view(item, participants)

    @router.post("/series", status_code=status.HTTP_201_CREATED)
    async def create_series(
        payload: SeriesPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            await _references(session, payload)
            series = LessonSeries(
                subject_id=payload.subject_id,
                teacher_id=payload.teacher_id,
                room_id=payload.room_id,
                group_id=payload.group_id,
                starts_at=_aware(payload.starts_at),
                duration_minutes=payload.duration_minutes,
                interval_weeks=payload.interval_weeks,
                occurrences=payload.occurrences,
            )
            session.add(series)
            await session.flush()
            created: list[int] = []
            for index in range(payload.occurrences):
                start = _aware(payload.starts_at) + timedelta(weeks=index * payload.interval_weeks)
                lesson_payload = LessonPayload(
                    subject_id=payload.subject_id,
                    teacher_id=payload.teacher_id,
                    room_id=payload.room_id,
                    group_id=payload.group_id,
                    start_at=start,
                    end_at=start + timedelta(minutes=payload.duration_minutes),
                    participant_ids=payload.participant_ids,
                    notes=payload.notes,
                )
                item = await save_lesson(
                    session,
                    lesson_payload,
                    admin_id=admin_id,
                    series_id=series.id,
                    series_occurrence_index=index,
                )
                item.created_by_admin_id = admin_id
                created.append(item.id)
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="series.created",
                    entity_type="lesson_series",
                    entity_id=series.id,
                    details={"lessons": created},
                )
            )
            await session.commit()
            return {"id": series.id, "lesson_ids": created}

    @router.put("/series/{series_id}")
    async def update_series(
        series_id: int,
        payload: SeriesUpdatePayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            series = await session.get(LessonSeries, series_id, with_for_update=True)
            if series is None:
                raise HTTPException(404)
            all_lessons = list(
                (
                    await session.scalars(
                        select(Lesson)
                        .where(Lesson.series_id == series_id)
                        .order_by(Lesson.series_occurrence_index, Lesson.id)
                    )
                ).all()
            )
            lessons = list(all_lessons)
            requested_start = _aware(payload.starts_at)
            anchor = next(
                (item for item in all_lessons if item.id == payload.anchor_lesson_id), None
            )
            if payload.scope == "future":
                if payload.anchor_lesson_id is None:
                    raise HTTPException(422, "Для будущих занятий требуется исходное занятие")
                if anchor is None:
                    raise HTTPException(404, "Исходное занятие не входит в серию")
                anchor_position = anchor.series_occurrence_index
                if anchor_position is None:
                    anchor_position = all_lessons.index(anchor)
                lessons = [
                    item
                    for item in lessons
                    if (item.series_occurrence_index or 0) >= anchor_position
                ]
            occurrence_positions = {
                item.id: (
                    item.series_occurrence_index
                    if item.series_occurrence_index is not None
                    else index
                )
                for index, item in enumerate(all_lessons)
            }
            lessons = [
                item
                for item in lessons
                if item.status in {"planned", "scheduled"} and not item.series_exception
            ]
            if not lessons:
                raise HTTPException(409, "В выбранной части серии нет изменяемых занятий")
            series.subject_id = payload.subject_id
            series.teacher_id = payload.teacher_id
            series.room_id = payload.room_id
            series.group_id = payload.group_id
            base_start = requested_start
            if payload.scope == "all" and anchor is not None and all_lessons:
                base_start = _db_utc(all_lessons[0].start_at) + (
                    requested_start - _db_utc(anchor.start_at)
                )
            series.starts_at = base_start
            series.duration_minutes = payload.duration_minutes
            series.interval_weeks = payload.interval_weeks
            series.occurrences = len(all_lessons)
            changed = []
            target_ids = {item.id for item in lessons}
            anchor_position = occurrence_positions.get(anchor.id, 0) if anchor is not None else 0
            for existing in lessons:
                occurrence_position = occurrence_positions[existing.id]
                relative_position = (
                    occurrence_position - anchor_position
                    if payload.scope == "future"
                    else occurrence_position
                )
                start = base_start + timedelta(
                    weeks=relative_position * payload.interval_weeks
                )
                lesson_payload = LessonPayload(
                    subject_id=payload.subject_id,
                    teacher_id=payload.teacher_id,
                    room_id=payload.room_id,
                    group_id=payload.group_id,
                    start_at=start,
                    end_at=start + timedelta(minutes=payload.duration_minutes),
                    participant_ids=payload.participant_ids,
                    notes=payload.notes,
                )
                await save_lesson(
                    session,
                    lesson_payload,
                    admin_id=admin_id,
                    exclude_id=existing.id,
                    conflict_exclude_ids=target_ids,
                    series_edit=True,
                )
                changed.append(existing.id)
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action=f"series.updated.{payload.scope}",
                    entity_type="lesson_series",
                    entity_id=series.id,
                    details={"lessons": changed},
                )
            )
            await session.commit()
            return {"id": series.id, "lesson_ids": changed}

    async def complete_lesson(
        session: AsyncSession,
        item: Lesson,
        participants: list[LessonParticipant],
        *,
        admin_id: int,
        completion_type: Literal["normal", "early"],
        reason: str | None = None,
        public_comment: str | None = None,
    ) -> None:
        if item.status != "in_progress":
            raise HTTPException(409, "Занятие не идёт")
        actual_end = utcnow()
        for participant in participants:
            if participant.attendance_status == "expected":
                apply_attendance_state(participant, "absent")
            elif participant.attendance_status in {"present", "late"}:
                participant.left_at = actual_end
        for segment in await _teacher_segments(session, item.id):
            if segment.ended_at is None:
                segment.ended_at = actual_end
        item.status = "completed"
        item.actual_end_at = actual_end
        item.updated_at = actual_end
        item.completion_type = completion_type
        item.completion_reason = reason.strip() if reason else None
        item.completion_public_comment = (
            public_comment.strip() if public_comment and public_comment.strip() else None
        )
        await _resolve_admin_condition(session, f"lesson:{item.id}:not_finished")
        await _resolve_admin_condition(session, f"lesson:{item.id}:no_active_students")
        session.add(
            AuditEvent(
                actor_admin_id=admin_id,
                action=(
                    "lesson.finished_early" if completion_type == "early" else "lesson.finished"
                ),
                entity_type="lesson",
                entity_id=item.id,
                details={
                    "completion_type": completion_type,
                    "reason": item.completion_reason,
                    "public_comment": item.completion_public_comment,
                },
            )
        )
        await _queue_lesson_state_notifications(
            session,
            item,
            participants,
            event="finished",
            center_timezone=center_timezone,
        )

    @router.post("/lessons/{lesson_id}/start")
    async def start_lesson(
        lesson_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Lesson, lesson_id, with_for_update=True)
            if item is None:
                raise HTTPException(404)
            if item.status == "in_progress":
                participants = list(
                    (
                        await session.scalars(
                            select(LessonParticipant).where(
                                LessonParticipant.lesson_id == lesson_id
                            )
                        )
                    ).all()
                )
                return {**_lesson_view(item, participants), "warnings": []}
            if item.status not in {"planned", "scheduled"}:
                raise HTTPException(409, "Занятие уже начато, завершено или отменено")
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            center_tz = _display_timezone(center_timezone)
            operational_day = datetime.now(center_tz).date()
            operational_day_start = datetime.combine(
                operational_day,
                time.min,
                tzinfo=center_tz,
            ).astimezone(UTC)
            present_ids = set(
                (
                    await session.scalars(
                        select(ClubPresenceSession.person_id).where(
                            ClubPresenceSession.left_at.is_(None),
                            ClubPresenceSession.arrived_at >= operational_day_start,
                        )
                    )
                ).all()
            )
            missing = [
                p.person_name_snapshot
                for p in participants
                if p.attendance_status != "excused" and p.person_id not in present_ids
            ]
            item.status, item.actual_start_at, item.updated_at = "in_progress", utcnow(), utcnow()
            await _resolve_admin_condition(session, f"lesson:{item.id}:not_started")
            for participant in participants:
                if (
                    participant.attendance_status == "expected"
                    and participant.person_id in present_ids
                ):
                    apply_attendance_state(
                        participant,
                        "present",
                        arrived_at=item.actual_start_at,
                    )
            if not await _teacher_segments(session, item.id):
                session.add(
                    LessonTeacherSegment(
                        lesson_id=item.id,
                        teacher_person_id=item.teacher_id,
                        teacher_name_snapshot=item.teacher_name_snapshot,
                        started_at=item.actual_start_at,
                        segment_type="primary",
                        created_by_admin_id=admin_id,
                    )
                )
            if missing:
                await _add_admin_notification(
                    session,
                    dedupe_key=f"lesson:{item.id}:missing_participants",
                    kind="lesson_missing_participants",
                    title="Не все участники пришли",
                    message=(f"{item.subject_name_snapshot}: отсутствуют " + ", ".join(missing)),
                    lesson_id=item.id,
                )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.started",
                    entity_type="lesson",
                    entity_id=item.id,
                    details={"missing": missing},
                )
            )
            await _queue_lesson_state_notifications(
                session,
                item,
                participants,
                event="started",
                center_timezone=center_timezone,
            )
            await session.commit()
            segments = await _teacher_segments(session, item.id)
            return {
                **_lesson_view(item, participants, segments),
                "warnings": (
                    [
                        {
                            "kind": "missing",
                            "message": "Не все участники находятся в клубе",
                            "people": missing,
                        }
                    ]
                    if missing
                    else []
                ),
            }

    @router.post("/lessons/{lesson_id}/finish")
    async def finish_lesson(
        lesson_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Lesson, lesson_id, with_for_update=True)
            if item is None:
                raise HTTPException(404)
            if item.status == "completed":
                participants = list(
                    (
                        await session.scalars(
                            select(LessonParticipant).where(
                                LessonParticipant.lesson_id == lesson_id
                            )
                        )
                    ).all()
                )
                return _lesson_view(item, participants)
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            await complete_lesson(
                session,
                item,
                participants,
                admin_id=admin_id,
                completion_type="normal",
            )
            await session.commit()
            return _lesson_view(item, participants, await _teacher_segments(session, item.id))

    @router.post("/lessons/{lesson_id}/finish-early")
    async def finish_lesson_early(
        lesson_id: int,
        payload: EarlyCompletionPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Lesson, lesson_id, with_for_update=True)
            if item is None:
                raise HTTPException(404)
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            await complete_lesson(
                session,
                item,
                participants,
                admin_id=admin_id,
                completion_type="early",
                reason=payload.reason,
                public_comment=payload.public_comment,
            )
            await session.commit()
            return _lesson_view(item, participants, await _teacher_segments(session, item.id))

    @router.post("/lessons/{lesson_id}/teacher-transition")
    async def teacher_transition(
        lesson_id: int,
        payload: TeacherTransitionPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id, with_for_update=True)
            if lesson is None:
                raise HTTPException(404)
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            if lesson.status != "in_progress":
                raise HTTPException(409, "Смена преподавателя доступна только во время занятия")
            if payload.action == "finish_early":
                await complete_lesson(
                    session,
                    lesson,
                    participants,
                    admin_id=admin_id,
                    completion_type="early",
                    reason=payload.reason,
                    public_comment=payload.public_comment,
                )
                await session.commit()
                return _lesson_view(
                    lesson, participants, await _teacher_segments(session, lesson.id)
                )

            replacement_id = int(payload.replacement_teacher_id or 0)
            replacement = await session.get(Person, replacement_id)
            teacher_role = await session.scalar(
                select(PersonRole.person_id).where(
                    PersonRole.person_id == replacement_id,
                    PersonRole.role == "teacher",
                )
            )
            if (
                replacement is None
                or teacher_role is None
                or not replacement.active
                or replacement.archived_at is not None
            ):
                raise HTTPException(422, "Новый преподаватель недоступен")
            if any(entry.person_id == replacement_id for entry in participants):
                raise HTTPException(409, "Участник занятия не может быть его преподавателем")
            qualification = await session.get(
                SubjectTeacher,
                {"subject_id": lesson.subject_id, "teacher_id": replacement_id},
            )
            if qualification is None:
                raise HTTPException(422, "Преподаватель не закреплён за этим предметом")
            replacement_at = utcnow()
            if payload.expected_end_at is not None:
                expected_end = _aware(payload.expected_end_at)
            elif replacement_at < _db_utc(lesson.end_at):
                expected_end = _db_utc(lesson.end_at)
            else:
                raise HTTPException(
                    422,
                    "Плановое время уже прошло. Укажите ожидаемое окончание работы замены.",
                )
            if expected_end <= replacement_at:
                raise HTTPException(422, "Ожидаемое окончание должно быть позже времени замены")
            conflicts = await _conflicts(
                session,
                start_at=replacement_at,
                end_at=expected_end,
                teacher_id=replacement_id,
                room_id=-1,
                participant_ids=set(),
                exclude_lesson_id=lesson.id,
            )
            if conflicts:
                raise HTTPException(
                    409,
                    {
                        "message": "Новый преподаватель занят",
                        "conflicts": conflicts,
                    },
                )
            segments = await _teacher_segments(session, lesson.id)
            current = next(
                (segment for segment in reversed(segments) if segment.ended_at is None), None
            )
            if current is None:
                raise HTTPException(409, "У занятия отсутствует активный преподаватель")
            if current.teacher_person_id == replacement_id:
                raise HTTPException(409, "Этот преподаватель уже ведёт занятие")
            current.ended_at = replacement_at
            current.reason = payload.reason.strip()
            replacement_segment = LessonTeacherSegment(
                lesson_id=lesson.id,
                teacher_person_id=replacement.id,
                teacher_name_snapshot=replacement.full_name,
                started_at=replacement_at,
                segment_type="substitute",
                reason=payload.reason.strip(),
                created_by_admin_id=admin_id,
            )
            session.add(replacement_segment)
            active_names = [
                entry.person_name_snapshot
                for entry in participants
                if entry.attendance_status not in {"excused", "absent", "left_early"}
            ]
            local_end = expected_end.astimezone(_display_timezone(center_timezone))
            students_text = (
                "\n".join(f"• {name}" for name in active_names)
                if active_names
                else "пока нет участников"
            )
            await _queue_communication_event(
                session,
                dedupe_key=(
                    f"lesson:{lesson.id}:teacher_replacement:"
                    f"{replacement.id}:{replacement_at.isoformat()}"
                ),
                event_type="teacher_replaced",
                lesson_id=lesson.id,
                recipient_person_id=replacement.id,
                recipient_context="teacher",
                subject_person_id=replacement.id,
                scheduled_at=replacement_at,
                text_value=(
                    "Вам назначена замена.\n\n"
                    f"{lesson.subject_name_snapshot}\n"
                    f"Сегодня до {local_end:%H:%M}\n"
                    f"Кабинет: {lesson.room_name_snapshot}\n\n"
                    f"Ученики:\n{students_text}"
                ),
                center_timezone=center_timezone,
            )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.teacher_replaced",
                    entity_type="lesson",
                    entity_id=lesson.id,
                    details={
                        "before_teacher_id": current.teacher_person_id,
                        "replacement_teacher_id": replacement.id,
                        "replacement_at": replacement_at.isoformat(),
                        "reason": payload.reason.strip(),
                    },
                )
            )
            await session.commit()
            return _lesson_view(lesson, participants, await _teacher_segments(session, lesson.id))

    @router.post("/lessons/{lesson_id}/cancel")
    async def cancel_lesson(
        lesson_id: int,
        payload: CancelPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Lesson, lesson_id, with_for_update=True)
            if item is None:
                raise HTTPException(404)
            if item.status == "cancelled":
                participants = list(
                    (
                        await session.scalars(
                            select(LessonParticipant).where(
                                LessonParticipant.lesson_id == lesson_id
                            )
                        )
                    ).all()
                )
                return _lesson_view(item, participants)
            if item.status not in {"planned", "scheduled"}:
                raise HTTPException(
                    409,
                    "Отменить можно только занятие, которое фактически не начиналось",
                )
            item.status, item.cancelled_reason, item.updated_at = (
                "cancelled",
                payload.reason,
                utcnow(),
            )
            item.notification_revision += 1
            from .communication_models import LessonAttendanceIntent

            await session.execute(
                LessonAttendanceIntent.__table__.update()
                .where(LessonAttendanceIntent.lesson_id == item.id)
                .values(status="needs_reconfirmation", updated_at=utcnow())
            )
            await _resolve_admin_condition(session, f"lesson:{item.id}:not_started")
            await _resolve_admin_condition(session, f"lesson:{item.id}:not_finished")
            await _resolve_admin_condition(session, f"lesson:{item.id}:no_active_students")
            await session.execute(
                NotificationJob.__table__.update()
                .where(
                    NotificationJob.lesson_id == lesson_id,
                    NotificationJob.status.in_(["pending", "retry"]),
                )
                .values(status="cancelled")
            )
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            participant_ids = {entry.person_id for entry in participants}
            guardian_rows = (
                await session.execute(
                    select(StudentGuardian.student_id, StudentGuardian.guardian_id).where(
                        StudentGuardian.student_id.in_(participant_ids),
                        StudentGuardian.guardian_id != StudentGuardian.student_id,
                    )
                )
            ).all()
            recipients = [(student_id, "student", student_id) for student_id in participant_ids]
            recipients.extend(
                (guardian_id, "guardian", student_id) for student_id, guardian_id in guardian_rows
            )
            recipients.append((item.teacher_id, "teacher", item.teacher_id))
            local_start = _db_utc(item.start_at).astimezone(_display_timezone(center_timezone))
            for recipient_id, recipient_context, subject_id in recipients:
                await _queue_communication_event(
                    session,
                    dedupe_key=(
                        f"lesson:{item.id}:cancelled:person:{recipient_id}:subject:{subject_id}"
                    ),
                    event_type="lesson_cancelled",
                    lesson_id=item.id,
                    recipient_person_id=recipient_id,
                    recipient_context=recipient_context,
                    subject_person_id=subject_id,
                    text_value=(
                        f"Занятие «{item.subject_name_snapshot}» "
                        f"{local_start:%d.%m в %H:%M} "
                        f"отменено. Причина: {payload.reason}"
                    ),
                    center_timezone=center_timezone,
                )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.cancelled",
                    entity_type="lesson",
                    entity_id=item.id,
                    details={"reason": payload.reason},
                )
            )
            await session.commit()
            return _lesson_view(item, participants)

    @router.put("/lessons/{lesson_id}/participants/{person_id}/attendance")
    async def set_attendance(
        lesson_id: int,
        person_id: int,
        payload: AttendancePayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        allowed = {"expected", "present", "late", "absent"}
        if payload.status not in allowed:
            raise HTTPException(422, "Неизвестный статус посещения")
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id)
            item = await session.scalar(
                select(LessonParticipant).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if lesson is None or item is None:
                raise HTTPException(404)
            if lesson.status in {"completed", "cancelled"}:
                raise HTTPException(
                    409,
                    "После завершения занятия используйте специальную корректировку",
                )
            if item.attendance_status == "excused":
                raise HTTPException(409, "Сначала явно восстановите отменённое участие")
            if item.attendance_status == "left_early":
                raise HTTPException(
                    409, "Досрочно завершённое участие изменяется только корректировкой"
                )
            before = {"status": item.attendance_status, "note": item.note}
            became_active = (
                lesson.status == "in_progress"
                and item.attendance_status not in {"present", "late"}
                and payload.status in {"present", "late"}
            )
            occurred_at = item.arrived_at or utcnow()
            try:
                if payload.status == "present":
                    apply_attendance_state(item, "present", arrived_at=occurred_at)
                elif payload.status == "late":
                    late_minutes = max(
                        1,
                        int(
                            (occurred_at - _db_utc(lesson.start_at)).total_seconds()
                            // 60
                        ),
                    )
                    apply_attendance_state(
                        item,
                        "late",
                        arrived_at=occurred_at,
                        late_minutes=late_minutes,
                    )
                else:
                    apply_attendance_state(item, payload.status)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            item.note = payload.note
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="attendance.updated",
                    entity_type="lesson_participant",
                    entity_id=item.id,
                    details={
                        "before": before,
                        "after": {"status": payload.status, "note": payload.note},
                    },
                )
            )
            if became_active:
                await _resolve_admin_condition(
                    session, f"lesson:{lesson.id}:no_active_students"
                )
                await _queue_lesson_state_notifications(
                    session,
                    lesson,
                    [item],
                    event="participant_started",
                    center_timezone=center_timezone,
                )
            await session.commit()
            return _model(
                item,
                "id",
                "person_id",
                "attendance_status",
                "arrived_at",
                "left_at",
                "late_minutes",
                "note",
            )

    @router.post("/lessons/{lesson_id}/participants/{person_id}/leave-early")
    async def participant_leave_early(
        lesson_id: int,
        person_id: int,
        payload: EarlyLeavePayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id, with_for_update=True)
            participant = await session.scalar(
                select(LessonParticipant)
                .where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
                .with_for_update()
            )
            if lesson is None or participant is None:
                raise HTTPException(404)
            if lesson.status != "in_progress":
                raise HTTPException(409, "Досрочный уход фиксируется только во время занятия")
            if participant.attendance_status not in {"present", "late"}:
                raise HTTPException(409, "Ученик фактически не участвует в занятии")
            before = {
                "attendance_status": participant.attendance_status,
                "left_at": participant.left_at.isoformat() if participant.left_at else None,
            }
            try:
                apply_attendance_state(
                    participant,
                    "left_early",
                    arrived_at=participant.arrived_at,
                    left_at=utcnow(),
                    late_minutes=participant.late_minutes,
                    early_leave_reason=payload.reason.strip(),
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="participant.left_early",
                    entity_type="lesson_participant",
                    entity_id=participant.id,
                    details={
                        "before": before,
                        "after": {
                            "attendance_status": "left_early",
                            "left_at": participant.left_at.isoformat(),
                        },
                        "reason": participant.early_leave_reason,
                    },
                )
            )
            active_count = await session.scalar(
                select(func.count(LessonParticipant.id)).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.attendance_status.in_(["present", "late"]),
                )
            )
            no_active_students = int(active_count or 0) == 0
            if no_active_students:
                await _raise_admin_condition(
                    session,
                    condition_key=f"lesson:{lesson.id}:no_active_students",
                    kind="lesson_no_active_students",
                    title="В занятии больше нет участвующих учеников",
                    message=(
                        f"{lesson.subject_name_snapshot}\n"
                        f"{lesson.room_name_snapshot}\n"
                        f"Преподаватель: {lesson.teacher_name_snapshot}"
                    ),
                    lesson_id=lesson.id,
                )
            await session.commit()
            return {
                **_model(
                    participant,
                    "id",
                    "person_id",
                    "attendance_status",
                    "arrived_at",
                    "left_at",
                    "early_leave_reason",
                ),
                "lesson_status": lesson.status,
                "warning": "no_active_students" if no_active_students else None,
            }

    @router.post(
        "/lessons/{lesson_id}/participants/{person_id}",
        status_code=status.HTTP_201_CREATED,
    )
    async def add_participant(
        lesson_id: int,
        person_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id, with_for_update=True)
            person = await session.get(Person, person_id)
            if lesson is None or person is None:
                raise HTTPException(404)
            student_role = await session.scalar(
                select(PersonRole.person_id).where(
                    PersonRole.person_id == person_id,
                    PersonRole.role == "student",
                )
            )
            if student_role is None or not person.active or person.archived_at is not None:
                raise HTTPException(422, "Участник должен быть активным учеником")
            if lesson.status not in {"planned", "scheduled"}:
                raise HTTPException(409, "Состав этого занятия уже нельзя менять")
            exists = await session.scalar(
                select(LessonParticipant.id).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if exists is not None:
                raise HTTPException(409, "Участник уже добавлен")
            count = await session.scalar(
                select(func.count(LessonParticipant.id)).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.attendance_status != "excused",
                )
            )
            room = await session.get(Room, lesson.room_id)
            if room is None or int(count or 0) >= room.capacity:
                raise HTTPException(409, "Вместимость кабинета недостаточна")
            conflicts = await _conflicts(
                session,
                start_at=_db_utc(lesson.start_at),
                end_at=_db_utc(lesson.end_at),
                teacher_id=-1,
                room_id=-1,
                participant_ids={person_id},
                exclude_lesson_id=lesson_id,
            )
            if conflicts:
                raise HTTPException(
                    409,
                    {
                        "message": "Ученик занят в это время",
                        "conflicts": conflicts,
                    },
                )
            participant = LessonParticipant(
                lesson_id=lesson_id,
                person_id=person_id,
                person_name_snapshot=person.full_name,
            )
            session.add(participant)
            lesson.notification_revision += 1
            from .communication_models import LessonAttendanceIntent

            await session.execute(
                LessonAttendanceIntent.__table__.update()
                .where(
                    LessonAttendanceIntent.lesson_id == lesson.id,
                    LessonAttendanceIntent.student_person_id == person_id,
                )
                .values(status="needs_reconfirmation", updated_at=utcnow())
            )
            await session.flush()
            await _queue_lesson_notifications(session, lesson, [participant], center_timezone)
            local_start = _db_utc(lesson.start_at).astimezone(_display_timezone(center_timezone))
            event_text = (
                f"Вы добавлены на занятие «{lesson.subject_name_snapshot}» "
                f"{local_start:%d.%m в %H:%M}."
            )
            await _queue_communication_event(
                session,
                dedupe_key=f"lesson:{lesson.id}:participant_added:student:{person.id}",
                event_type="participant_added",
                lesson_id=lesson.id,
                recipient_person_id=person.id,
                recipient_context="student",
                subject_person_id=person.id,
                text_value=event_text,
                center_timezone=center_timezone,
            )
            guardian_ids = list(
                (
                    await session.scalars(
                        select(StudentGuardian.guardian_id).where(
                            StudentGuardian.student_id == person.id,
                            StudentGuardian.guardian_id != person.id,
                        )
                    )
                ).all()
            )
            for guardian_id in guardian_ids:
                await _queue_communication_event(
                    session,
                    dedupe_key=(
                        f"lesson:{lesson.id}:participant_added:guardian:"
                        f"{guardian_id}:student:{person.id}"
                    ),
                    event_type="participant_added",
                    lesson_id=lesson.id,
                    recipient_person_id=guardian_id,
                    recipient_context="guardian",
                    subject_person_id=person.id,
                    text_value=(
                        f"{person.full_name} добавлен(а) на занятие "
                        f"«{lesson.subject_name_snapshot}»."
                    ),
                    center_timezone=center_timezone,
                )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="participant.added",
                    entity_type="lesson_participant",
                    entity_id=participant.id,
                )
            )
            await session.commit()
            return _model(
                participant,
                "id",
                "person_id",
                "person_name_snapshot",
                "attendance_status",
            )

    @router.post("/lessons/{lesson_id}/participants/{person_id}/cancel")
    async def cancel_participant(
        lesson_id: int,
        person_id: int,
        payload: ParticipantCancelPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        return await _set_participant_state(
            lesson_id, person_id, "excused", admin_id, cancellation=payload
        )

    @router.post("/lessons/{lesson_id}/participants/{person_id}/restore")
    async def restore_participant(
        lesson_id: int,
        person_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        return await _set_participant_state(lesson_id, person_id, "expected", admin_id)

    async def _set_participant_state(
        lesson_id: int,
        person_id: int,
        state: str,
        admin_id: int,
        cancellation: ParticipantCancelPayload | None = None,
    ) -> dict[str, Any]:
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id)
            participant = await session.scalar(
                select(LessonParticipant).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if participant is None or lesson is None:
                raise HTTPException(404)
            if lesson.status in {"completed", "cancelled"}:
                raise HTTPException(409, "Состав завершённого занятия изменять нельзя")
            if state == "excused" and (
                participant.attendance_status != "expected" or participant.arrived_at is not None
            ):
                raise HTTPException(409, "Нельзя отменить уже начавшееся участие")
            if state == "expected" and participant.attendance_status != "excused":
                raise HTTPException(409, "Восстановить можно только отменённое участие")
            if state == "excused":
                if cancellation is None:
                    raise HTTPException(422, "Укажите причину отмены")
                actor_person_id = cancellation.cancelled_by_person_id
                if cancellation.cancelled_by == "student":
                    if actor_person_id not in {None, person_id}:
                        raise HTTPException(422, "Ученик может отменить только своё участие")
                    actor_person_id = person_id
                elif cancellation.cancelled_by == "guardian":
                    if actor_person_id is None:
                        raise HTTPException(422, "Не указан родитель")
                    guardian_link = await session.scalar(
                        select(StudentGuardian.guardian_id).where(
                            StudentGuardian.student_id == person_id,
                            StudentGuardian.guardian_id == actor_person_id,
                        )
                    )
                    if guardian_link is None:
                        raise HTTPException(422, "Человек не связан с учеником как родитель")
                else:
                    actor_person_id = None
                apply_attendance_state(
                    participant,
                    "excused",
                    cancelled_at=utcnow(),
                    cancelled_by=cancellation.cancelled_by,
                    cancelled_by_person_id=actor_person_id,
                    cancelled_by_admin_id=(
                        admin_id if cancellation.cancelled_by == "administrator" else None
                    ),
                    cancellation_reason=cancellation.reason.strip(),
                )
                await session.execute(
                    NotificationJob.__table__.update()
                    .where(
                        NotificationJob.lesson_id == lesson_id,
                        NotificationJob.dedupe_key.like(
                            f"lesson:{lesson_id}:reminder:%student:{person_id}"
                        ),
                        NotificationJob.status.in_(["pending", "retry"]),
                    )
                    .values(status="cancelled")
                )
            elif state == "expected" and lesson is not None:
                apply_attendance_state(participant, "expected")
                room = await session.get(Room, lesson.room_id)
                active_count = await session.scalar(
                    select(func.count(LessonParticipant.id)).where(
                        LessonParticipant.lesson_id == lesson_id,
                        LessonParticipant.attendance_status != "excused",
                    )
                )
                if room is None or int(active_count or 0) >= room.capacity:
                    raise HTTPException(409, "Вместимость кабинета недостаточна")
                conflicts = await _conflicts(
                    session,
                    start_at=_db_utc(lesson.start_at),
                    end_at=_db_utc(lesson.end_at),
                    teacher_id=-1,
                    room_id=-1,
                    participant_ids={person_id},
                    exclude_lesson_id=lesson_id,
                )
                if conflicts:
                    raise HTTPException(
                        409,
                        {
                            "message": "Ученик занят в это время",
                            "conflicts": conflicts,
                        },
                    )
            lesson.notification_revision += 1
            from .communication_models import LessonAttendanceIntent

            await session.execute(
                LessonAttendanceIntent.__table__.update()
                .where(
                    LessonAttendanceIntent.lesson_id == lesson.id,
                    LessonAttendanceIntent.student_person_id == person_id,
                )
                .values(status="needs_reconfirmation", updated_at=utcnow())
            )
            await session.flush()
            await _queue_lesson_notifications(session, lesson, [participant], center_timezone)
            notification_event = (
                "participant_removed" if state == "excused" else "participant_added"
            )
            state_text = f"Участие в занятии «{lesson.subject_name_snapshot}» " + (
                "отменено." if state == "excused" else "восстановлено."
            )
            await _queue_communication_event(
                session,
                dedupe_key=(
                    f"lesson:{lesson.id}:{notification_event}:student:{person_id}:"
                    f"revision:{lesson.notification_revision}"
                ),
                event_type=notification_event,
                lesson_id=lesson.id,
                recipient_person_id=person_id,
                recipient_context="student",
                subject_person_id=person_id,
                text_value=state_text,
                center_timezone=center_timezone,
            )
            guardian_ids = list(
                (
                    await session.scalars(
                        select(StudentGuardian.guardian_id).where(
                            StudentGuardian.student_id == person_id,
                            StudentGuardian.guardian_id != person_id,
                        )
                    )
                ).all()
            )
            for guardian_id in guardian_ids:
                await _queue_communication_event(
                    session,
                    dedupe_key=(
                        f"lesson:{lesson.id}:{notification_event}:guardian:{guardian_id}:"
                        f"student:{person_id}:revision:{lesson.notification_revision}"
                    ),
                    event_type=notification_event,
                    lesson_id=lesson.id,
                    recipient_person_id=guardian_id,
                    recipient_context="guardian",
                    subject_person_id=person_id,
                    text_value=state_text,
                    center_timezone=center_timezone,
                )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action=f"participant.{state}",
                    entity_type="lesson_participant",
                    entity_id=participant.id,
                    details={
                        "before": {
                            "attendance_status": "expected" if state == "excused" else "excused"
                        },
                        "after": {
                            "attendance_status": state,
                            "cancelled_by": participant.cancelled_by,
                            "cancelled_by_person_id": participant.cancelled_by_person_id,
                            "cancelled_by_admin_id": participant.cancelled_by_admin_id,
                            "cancelled_at": participant.cancelled_at.isoformat()
                            if participant.cancelled_at
                            else None,
                        },
                        "reason": participant.cancellation_reason,
                    },
                )
            )
            await session.commit()
            return _model(
                participant,
                "id",
                "person_id",
                "attendance_status",
                "cancelled_at",
                "cancelled_by",
                "cancelled_by_person_id",
                "cancelled_by_admin_id",
                "cancellation_reason",
            )

    @router.post("/lessons/{lesson_id}/participants/{person_id}/correct")
    async def correct_attendance(
        lesson_id: int,
        person_id: int,
        payload: CorrectionPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        allowed = {"present", "late", "absent", "left_early", "excused"}
        if payload.attendance_status not in allowed:
            raise HTTPException(422, "Неизвестный статус посещения")
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id)
            participant = await session.scalar(
                select(LessonParticipant).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if lesson is None or participant is None:
                raise HTTPException(404)
            if lesson.status != "completed":
                raise HTTPException(409, "Корректировка доступна после завершения занятия")
            old = {
                "status": participant.attendance_status,
                "arrived_at": participant.arrived_at.isoformat()
                if participant.arrived_at
                else None,
                "left_at": participant.left_at.isoformat() if participant.left_at else None,
            }
            arrived_at = _aware(payload.arrived_at) if payload.arrived_at else None
            left_at = _aware(payload.left_at) if payload.left_at else None
            late_minutes = (
                max(1, int((arrived_at - _db_utc(lesson.start_at)).total_seconds() // 60))
                if arrived_at is not None and payload.attendance_status == "late"
                else 0
            )
            try:
                apply_attendance_state(
                    participant,
                    payload.attendance_status,
                    arrived_at=arrived_at,
                    left_at=left_at,
                    late_minutes=late_minutes,
                    early_leave_reason=(
                        payload.reason.strip()
                        if payload.attendance_status == "left_early"
                        else None
                    ),
                    cancelled_at=(
                        utcnow() if payload.attendance_status == "excused" else None
                    ),
                    cancelled_by=(
                        "administrator" if payload.attendance_status == "excused" else None
                    ),
                    cancelled_by_admin_id=(
                        admin_id if payload.attendance_status == "excused" else None
                    ),
                    cancellation_reason=(
                        payload.reason.strip()
                        if payload.attendance_status == "excused"
                        else None
                    ),
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="attendance.corrected",
                    entity_type="lesson_participant",
                    entity_id=participant.id,
                    details={
                        "reason": payload.reason,
                        "before": old,
                        "after": {
                            "status": participant.attendance_status,
                            "arrived_at": participant.arrived_at.isoformat()
                            if participant.arrived_at
                            else None,
                            "left_at": participant.left_at.isoformat()
                            if participant.left_at
                            else None,
                        },
                    },
                )
            )
            await session.commit()
            return _model(
                participant,
                "id",
                "person_id",
                "attendance_status",
                "arrived_at",
                "left_at",
                "late_minutes",
                "note",
            )

    @router.post("/lessons/{lesson_id}/correct-time")
    async def correct_actual_time(
        lesson_id: int,
        payload: ActualTimeCorrectionPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id, with_for_update=True)
            if lesson is None:
                raise HTTPException(404)
            if lesson.status != "completed":
                raise HTTPException(409, "Фактическое время исправляется после завершения")
            actual_start = _aware(payload.actual_start_at)
            actual_end = _aware(payload.actual_end_at)
            segments = await _teacher_segments(session, lesson.id)
            if segments:
                first_boundary = segments[0].ended_at if len(segments) > 1 else None
                last_boundary = segments[-1].started_at if len(segments) > 1 else None
                if first_boundary is not None and actual_start >= _db_utc(first_boundary):
                    raise HTTPException(
                        422,
                        "Новое начало должно быть раньше первой смены преподавателя",
                    )
                if last_boundary is not None and actual_end <= _db_utc(last_boundary):
                    raise HTTPException(
                        422,
                        "Новое окончание должно быть позже последней смены преподавателя",
                    )
            before = {
                "actual_start_at": lesson.actual_start_at.isoformat()
                if lesson.actual_start_at
                else None,
                "actual_end_at": lesson.actual_end_at.isoformat() if lesson.actual_end_at else None,
            }
            lesson.actual_start_at = actual_start
            lesson.actual_end_at = actual_end
            if segments:
                segments[0].started_at = actual_start
                segments[-1].ended_at = actual_end
            lesson.updated_at = utcnow()
            after = {
                "actual_start_at": lesson.actual_start_at.isoformat(),
                "actual_end_at": lesson.actual_end_at.isoformat(),
            }
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.actual_time_corrected",
                    entity_type="lesson",
                    entity_id=lesson.id,
                    details={
                        "before": before,
                        "after": after,
                        "reason": payload.reason,
                        "teacher_segment_ids": [segment.id for segment in segments],
                    },
                )
            )
            await session.commit()
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            return _lesson_view(lesson, participants, segments)

    @router.post("/presence/{person_id}/arrival", status_code=status.HTTP_201_CREATED)
    async def arrival(
        person_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(12636885, :person_id)"),
                    {"person_id": person_id},
                )
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is not None:
                raise HTTPException(404)
            existing = await session.scalar(
                select(ClubPresenceSession)
                .where(
                    ClubPresenceSession.person_id == person_id,
                    ClubPresenceSession.left_at.is_(None),
                )
                .with_for_update()
            )
            if existing is not None:
                center_tz = _display_timezone(center_timezone)
                operational_day = datetime.now(center_tz).date()
                if _db_utc(existing.arrived_at).astimezone(center_tz).date() < operational_day:
                    raise HTTPException(
                        409,
                        {
                            "code": "stale_presence",
                            "message": (
                                "У клиента не отмечен уход за предыдущий день. "
                                "Сначала закройте старое посещение, затем отметьте новый приход."
                            ),
                            "presence_id": existing.id,
                            "arrived_at": _db_utc(existing.arrived_at).isoformat(),
                        },
                    )
                return {
                    **_model(existing, "id", "person_id", "arrived_at", "left_at"),
                    "already_present": True,
                }
            now = utcnow()
            presence = ClubPresenceSession(
                person_id=person_id,
                arrived_at=now,
                source="management",
                arrived_by_admin_id=admin_id,
            )
            session.add(presence)
            active_participants = (
                await session.execute(
                    select(LessonParticipant, Lesson)
                    .join(Lesson, Lesson.id == LessonParticipant.lesson_id)
                    .where(LessonParticipant.person_id == person_id, Lesson.status == "in_progress")
                )
            ).all()
            for participant, lesson in active_participants:
                if participant.attendance_status != "expected":
                    continue
                late_minutes = max(
                    0, int((now - _db_utc(lesson.start_at)).total_seconds() // 60)
                )
                apply_attendance_state(
                    participant,
                    "late" if late_minutes else "present",
                    arrived_at=now,
                    late_minutes=late_minutes,
                )
                await _resolve_admin_condition(
                    session, f"lesson:{lesson.id}:no_active_students"
                )
                await _queue_lesson_state_notifications(
                    session,
                    lesson,
                    [participant],
                    event="participant_started",
                    center_timezone=center_timezone,
                )
            await _queue_guardian_event(
                session,
                person=person,
                event_type="arrival",
                occurred_at=now,
                center_timezone=center_timezone,
            )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="presence.arrival",
                    entity_type="person",
                    entity_id=person_id,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                current = await session.scalar(
                    select(ClubPresenceSession).where(
                        ClubPresenceSession.person_id == person_id,
                        ClubPresenceSession.left_at.is_(None),
                    )
                )
                if current is None:
                    raise
                return {
                    **_model(current, "id", "person_id", "arrived_at", "left_at"),
                    "already_present": True,
                }
            return {
                **_model(presence, "id", "person_id", "arrived_at", "left_at"),
                "already_present": False,
            }

    @router.post("/presence/{person_id}/departure")
    async def departure(
        person_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            presence = await session.scalar(
                select(ClubPresenceSession)
                .where(
                    ClubPresenceSession.person_id == person_id,
                    ClubPresenceSession.left_at.is_(None),
                )
                .with_for_update()
            )
            if presence is None:
                latest = await session.scalar(
                    select(ClubPresenceSession)
                    .where(ClubPresenceSession.person_id == person_id)
                    .order_by(ClubPresenceSession.arrived_at.desc())
                    .limit(1)
                )
                if latest is None:
                    raise HTTPException(409, "Человек ещё не отмечался в клубе")
                return {
                    **_model(latest, "id", "person_id", "arrived_at", "left_at"),
                    "warnings": [],
                    "already_departed": True,
                }
            now = utcnow()
            presence.left_at = now
            presence.left_by_admin_id = admin_id
            person = await session.get(Person, person_id)
            active = (
                await session.execute(
                    select(LessonParticipant, Lesson)
                    .join(Lesson)
                    .where(LessonParticipant.person_id == person_id, Lesson.status == "in_progress")
                )
            ).all()
            warnings = []
            for participant, lesson in active:
                if participant.attendance_status not in {"present", "late"}:
                    continue
                apply_attendance_state(
                    participant,
                    "left_early",
                    arrived_at=participant.arrived_at,
                    left_at=now,
                    late_minutes=participant.late_minutes,
                    early_leave_reason=(
                        participant.early_leave_reason
                        or "Уход из клуба во время занятия"
                    ),
                )
                warnings.append(
                    {
                        "kind": "active_lesson",
                        "lesson_id": lesson.id,
                        "message": "Участник ушёл во время занятия",
                    }
                )
                active_count = await session.scalar(
                    select(func.count(LessonParticipant.id)).where(
                        LessonParticipant.lesson_id == lesson.id,
                        LessonParticipant.attendance_status.in_(["present", "late"]),
                    )
                )
                if int(active_count or 0) == 0:
                    await _raise_admin_condition(
                        session,
                        condition_key=f"lesson:{lesson.id}:no_active_students",
                        kind="lesson_no_active_students",
                        title="В занятии больше нет участвующих учеников",
                        message=(
                            f"{lesson.subject_name_snapshot}\n"
                            f"{lesson.room_name_snapshot}\n"
                            f"Преподаватель: {lesson.teacher_name_snapshot}"
                        ),
                        lesson_id=lesson.id,
                    )
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="presence.departure",
                    entity_type="person",
                    entity_id=person_id,
                )
            )
            if person is not None:
                await _queue_guardian_event(
                    session,
                    person=person,
                    event_type="departure",
                    occurred_at=now,
                    center_timezone=center_timezone,
                )
            await _resolve_admin_condition(session, f"presence:{presence.id}:stale")
            await session.commit()
            return {
                **_model(presence, "id", "person_id", "arrived_at", "left_at"),
                "warnings": warnings,
            }

    @router.get("/history/person/{person_id}")
    async def person_history(person_id: int) -> dict[str, Any]:
        async with sessions() as session:
            attendance = (
                await session.execute(
                    select(LessonParticipant, Lesson)
                    .join(Lesson)
                    .where(LessonParticipant.person_id == person_id)
                    .order_by(Lesson.start_at.desc())
                )
            ).all()
            presence = list(
                (
                    await session.scalars(
                        select(ClubPresenceSession)
                        .where(ClubPresenceSession.person_id == person_id)
                        .order_by(ClubPresenceSession.arrived_at.desc())
                    )
                ).all()
            )
            return {
                "lessons": [
                    {
                        **_model(
                            participant,
                            "attendance_status",
                            "arrived_at",
                            "left_at",
                            "late_minutes",
                            "note",
                            "cancelled_at",
                            "cancelled_by",
                            "cancelled_by_person_id",
                            "cancelled_by_admin_id",
                            "cancellation_reason",
                        ),
                        **_model(
                            lesson,
                            "id",
                            "start_at",
                            "end_at",
                            "actual_start_at",
                            "actual_end_at",
                            "status",
                            "subject_name_snapshot",
                            "teacher_name_snapshot",
                            "room_name_snapshot",
                        ),
                    }
                    for participant, lesson in attendance
                ],
                "presence": [_model(x, "id", "arrived_at", "left_at") for x in presence],
            }

    @router.get("/history/teacher/{person_id}")
    async def teacher_history(person_id: int) -> dict[str, Any]:
        async with sessions() as session:
            segment_rows = (
                await session.execute(
                    select(LessonTeacherSegment, Lesson)
                    .join(Lesson, Lesson.id == LessonTeacherSegment.lesson_id)
                    .where(LessonTeacherSegment.teacher_person_id == person_id)
                    .order_by(LessonTeacherSegment.started_at.desc())
                )
            ).all()
            planned = list(
                (
                    await session.scalars(
                        select(Lesson)
                        .where(
                            Lesson.teacher_id == person_id,
                            Lesson.status.in_(["planned", "scheduled"]),
                        )
                        .order_by(Lesson.start_at.desc())
                    )
                ).all()
            )
            factual_by_lesson: dict[int, tuple[Lesson, list[LessonTeacherSegment]]] = {}
            for segment, lesson in segment_rows:
                factual_by_lesson.setdefault(lesson.id, (lesson, []))[1].append(segment)
            factual = []
            for lesson, segments in factual_by_lesson.values():
                single_segment = segments[0] if len(segments) == 1 else None
                actual_start = min(segment.started_at for segment in segments)
                segment_ends = [segment.ended_at or lesson.actual_end_at for segment in segments]
                actual_end = max(
                    (value for value in segment_ends if value is not None),
                    default=None,
                )
                durations = [
                    max(
                        0,
                        int(
                            (_db_utc(segment_end) - _db_utc(segment.started_at)).total_seconds()
                            // 60
                        ),
                    )
                    for segment, segment_end in zip(segments, segment_ends, strict=True)
                    if segment_end is not None
                ]
                factual.append(
                    {
                        **_model(
                            lesson,
                            "id",
                            "start_at",
                            "end_at",
                            "status",
                            "subject_name_snapshot",
                            "teacher_name_snapshot",
                            "room_name_snapshot",
                            "completion_type",
                        ),
                        "actual_start_at": actual_start,
                        "actual_end_at": actual_end,
                        "teacher_segment_id": single_segment.id if single_segment else None,
                        "teacher_segment_type": (
                            single_segment.segment_type if single_segment else "aggregated"
                        ),
                        "teacher_segment_reason": (
                            single_segment.reason if single_segment else None
                        ),
                        "teacher_segment_minutes": sum(durations),
                        "actual_teacher_name": segments[0].teacher_name_snapshot,
                        "teacher_segments": [
                            _model(
                                segment,
                                "id",
                                "started_at",
                                "ended_at",
                                "segment_type",
                                "reason",
                            )
                            for segment in segments
                        ],
                    }
                )
            planned_views = [
                {
                    **_model(
                        lesson,
                        "id",
                        "start_at",
                        "end_at",
                        "actual_start_at",
                        "actual_end_at",
                        "status",
                        "subject_name_snapshot",
                        "teacher_name_snapshot",
                        "room_name_snapshot",
                        "completion_type",
                    ),
                    "teacher_segment_type": "planned",
                    "teacher_segment_minutes": None,
                    "actual_teacher_name": None,
                }
                for lesson in planned
            ]
            lessons = sorted(
                [*factual, *planned_views],
                key=lambda value: _db_utc(value["start_at"]),
                reverse=True,
            )
            return {
                "lessons": lessons,
                "summary": {
                    "total": len(lessons),
                    "completed": sum(item["status"] == "completed" for item in lessons),
                    "cancelled": sum(item["status"] == "cancelled" for item in lessons),
                },
            }

    @router.get("/reports/schedule")
    async def schedule_report(
        date_from: datetime,
        date_to: datetime,
        teacher_id: int | None = None,
        student_id: int | None = None,
        room_id: int | None = None,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        start, end = _aware(date_from), _aware(date_to)
        async with sessions() as session:
            query = select(Lesson).where(Lesson.start_at < end, Lesson.end_at > start)
            if teacher_id is not None:
                query = query.where(Lesson.teacher_id == teacher_id)
            if room_id is not None:
                query = query.where(Lesson.room_id == room_id)
            if group_id is not None:
                query = query.where(Lesson.group_id == group_id)
            if student_id is not None:
                query = query.where(
                    Lesson.id.in_(
                        select(LessonParticipant.lesson_id).where(
                            LessonParticipant.person_id == student_id,
                            LessonParticipant.attendance_status != "excused",
                        )
                    )
                )
            lessons = list((await session.scalars(query.order_by(Lesson.start_at))).all())
            counts = (
                dict(
                    (
                        await session.execute(
                            select(
                                LessonParticipant.lesson_id,
                                func.count(LessonParticipant.id),
                            )
                            .where(
                                LessonParticipant.lesson_id.in_([item.id for item in lessons]),
                                LessonParticipant.attendance_status != "excused",
                            )
                            .group_by(LessonParticipant.lesson_id)
                        )
                    ).all()
                )
                if lessons
                else {}
            )
        return {
            "date_from": start,
            "date_to": end,
            "lessons": [
                {
                    **_model(
                        item,
                        "id",
                        "start_at",
                        "end_at",
                        "actual_start_at",
                        "actual_end_at",
                        "status",
                        "subject_name_snapshot",
                        "teacher_name_snapshot",
                        "room_name_snapshot",
                    ),
                    "participant_count": counts.get(item.id, 0),
                }
                for item in lessons
            ],
        }

    @router.get("/admin-notifications")
    async def admin_notifications(unread_only: bool = True) -> list[dict[str, Any]]:
        query = select(AdminNotification)
        if unread_only:
            query = query.where(AdminNotification.read_at.is_(None))
        async with sessions() as session:
            await _ensure_admin_notifications(session, utcnow())
            await session.flush()
            items = list(
                (
                    await session.scalars(
                        query.order_by(AdminNotification.created_at.desc()).limit(100)
                    )
                ).all()
            )
            items = _unique_admin_notifications(items)
            await session.commit()
        return [
            _model(
                item,
                "id",
                "kind",
                "title",
                "message",
                "lesson_id",
                "read_at",
                "created_at",
            )
            for item in items
        ]

    @router.post("/admin-notifications/{notification_id}/read")
    async def read_admin_notification(notification_id: int) -> dict[str, bool]:
        async with sessions() as session:
            item = await session.get(AdminNotification, notification_id)
            if item is None:
                raise HTTPException(404)
            item.read_at = utcnow()
            await session.commit()
        return {"read": True}

    @router.post("/admin-notifications/read-all")
    async def read_all_admin_notifications() -> dict[str, int]:
        async with sessions() as session:
            items = list(
                (
                    await session.scalars(
                        select(AdminNotification).where(AdminNotification.read_at.is_(None))
                    )
                ).all()
            )
            now = utcnow()
            for item in items:
                item.read_at = now
            await session.commit()
        return {"read": len(items)}

    @router.get("/free-slots")
    async def free_slots(
        day: date,
        duration_minutes: int = Query(60, ge=5, le=480),
        teacher_id: int | None = None,
        room_id: int | None = None,
        student_ids: Annotated[list[int] | None, Query()] = None,
        timezone_offset_minutes: int = Query(default=300, ge=-720, le=840),
    ) -> list[dict[str, Any]]:
        try:
            tz = ZoneInfo(center_timezone)
        except ZoneInfoNotFoundError:
            tz = timezone(timedelta(minutes=timezone_offset_minutes))
        start = datetime.combine(day, time(8), tzinfo=tz).astimezone(UTC)
        end = datetime.combine(day, time(21), tzinfo=tz).astimezone(UTC)
        async with sessions() as session:
            room_query = select(Room).where(Room.active.is_(True))
            if room_id is not None:
                room_query = room_query.where(Room.id == room_id)
            rooms = list((await session.scalars(room_query.order_by(Room.name))).all())
            busy = list(
                (
                    await session.scalars(
                        select(Lesson).where(
                            Lesson.status != "cancelled",
                            or_(
                                and_(Lesson.start_at < end, Lesson.end_at > start),
                                Lesson.status == "in_progress",
                            ),
                        )
                    )
                ).all()
            )
            busy_ids = [item.id for item in busy]
            participant_rows = (
                (
                    await session.execute(
                        select(LessonParticipant.lesson_id, LessonParticipant.person_id).where(
                            LessonParticipant.lesson_id.in_(busy_ids),
                            LessonParticipant.attendance_status != "excused",
                        )
                    )
                ).all()
                if busy_ids
                else []
            )
            segment_rows = (
                await session.execute(
                    select(
                        LessonTeacherSegment.lesson_id,
                        LessonTeacherSegment.teacher_person_id,
                        LessonTeacherSegment.started_at,
                        LessonTeacherSegment.ended_at,
                    )
                    .join(Lesson, Lesson.id == LessonTeacherSegment.lesson_id)
                    .where(
                        Lesson.status == "in_progress",
                        LessonTeacherSegment.started_at < end,
                        or_(
                            LessonTeacherSegment.ended_at.is_(None),
                            LessonTeacherSegment.ended_at > start,
                        ),
                    )
                )
            ).all()
        participants_by_lesson: dict[int, set[int]] = {}
        for lesson_id, person_id in participant_rows:
            participants_by_lesson.setdefault(lesson_id, set()).add(person_id)
        selected_students = set(student_ids or [])
        slots: list[dict[str, Any]] = []
        duration = timedelta(minutes=duration_minutes)
        cursor = start
        while cursor + duration <= end:
            candidate_end = cursor + duration
            for room in rooms:
                if room.capacity < len(selected_students):
                    continue
                conflict = False
                for lesson in busy:
                    planned_overlap = (
                        _db_utc(lesson.start_at) < candidate_end and _db_utc(lesson.end_at) > cursor
                    )
                    factual_teachers = {
                        person_id
                        for lesson_id, person_id, segment_start, segment_end in segment_rows
                        if lesson_id == lesson.id
                        and _db_utc(segment_start) < candidate_end
                        and (segment_end is None or _db_utc(segment_end) > cursor)
                    }
                    factual_overlap = bool(factual_teachers)
                    if not planned_overlap and not factual_overlap:
                        continue
                    lesson_people = participants_by_lesson.get(lesson.id, set())
                    if lesson.room_id == room.id:
                        conflict = True
                    if teacher_id is not None and (
                        lesson.teacher_id == teacher_id
                        or teacher_id in lesson_people
                        or teacher_id in factual_teachers
                    ):
                        conflict = True
                    if (
                        lesson.teacher_id in selected_students
                        or lesson_people & selected_students
                        or factual_teachers & selected_students
                    ):
                        conflict = True
                    if conflict:
                        break
                if not conflict:
                    slots.append(
                        {
                            "start_at": cursor,
                            "end_at": candidate_end,
                            "room_id": room.id,
                            "room_name": room.name,
                        }
                    )
            cursor += timedelta(minutes=30)
        return slots

    return router
