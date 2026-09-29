from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select, text
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
    NotificationJob,
    Room,
    StudyGroup,
    Subject,
)


class NamedPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    active: bool = True


class SubjectPayload(NamedPayload):
    color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")


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
    if payload.group_id is not None and (group is None or not group.active):
        raise HTTPException(422, "Группа недоступна")
    return subject, teacher, room, group


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
    query = select(Lesson).where(
        Lesson.status != "cancelled",
        Lesson.start_at < end_at,
        Lesson.end_at > start_at,
    )
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
        if item.room_id == room_id:
            result.append(
                {
                    "kind": "room",
                    "lesson_id": item.id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Кабинет уже занят",
                }
            )
        if item.teacher_id == teacher_id:
            result.append(
                {
                    "kind": "teacher",
                    "lesson_id": item.id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Учитель уже занят",
                }
            )
        if item.teacher_id in participant_ids:
            result.append(
                {
                    "kind": "person",
                    "lesson_id": item.id,
                    "person_id": item.teacher_id,
                    "start_at": _db_utc(item.start_at).isoformat(),
                    "message": "Участник занят как преподаватель",
                }
            )
        if teacher_id in busy_students.get(item.id, set()):
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
    return result


def _lesson_view(item: Lesson, participants: list[LessonParticipant]) -> dict[str, Any]:
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
            "teacher_name_snapshot",
            "room_name_snapshot",
            "subject_name_snapshot",
            "notes",
            "created_at",
            "updated_at",
        ),
        "computed_status": computed,
        "ready": ready,
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
    active_participants = [item for item in participants if item.attendance_status != "excused"]
    participant_ids = {item.person_id for item in active_participants}
    if not participant_ids:
        return
    people = {
        item.id: item
        for item in (
            await session.scalars(select(Person).where(Person.id.in_(participant_ids)))
        ).all()
    }
    guardian_rows = (
        await session.execute(
            select(StudentGuardian.student_id, StudentGuardian.guardian_id).where(
                StudentGuardian.student_id.in_(participant_ids)
            )
        )
    ).all()
    guardians_by_student: dict[int, set[int]] = {}
    for student_id, guardian_id in guardian_rows:
        if guardian_id != student_id:
            guardians_by_student.setdefault(student_id, set()).add(guardian_id)
    local_start = _db_utc(lesson.start_at).astimezone(_display_timezone(center_timezone))
    local_end = _db_utc(lesson.end_at).astimezone(_display_timezone(center_timezone))
    for hours in (24, 3, 1):
        scheduled_at = _db_utc(lesson.start_at) - timedelta(hours=hours)
        if scheduled_at <= utcnow():
            continue
        jobs: list[tuple[str, int, dict[str, Any]]] = []
        for participant in active_participants:
            student = people.get(participant.person_id)
            if student is None:
                continue
            jobs.append(
                (
                    f"student:{student.id}",
                    student.id,
                    {
                        "template": "student_reminder",
                        "hours": hours,
                        "text": (
                            "Напоминание о занятии\n\n"
                            f"{local_start:%d.%m.%Y в %H:%M}\n"
                            f"{lesson.subject_name_snapshot}\n"
                            f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                            f"Кабинет: {lesson.room_name_snapshot}"
                        ),
                    },
                )
            )
            name_parts = student.full_name.split()
            first_name = name_parts[1] if len(name_parts) > 1 else student.full_name
            for guardian_id in guardians_by_student.get(student.id, set()):
                jobs.append(
                    (
                        f"guardian:{guardian_id}:student:{student.id}",
                        guardian_id,
                        {
                            "template": "guardian_reminder",
                            "hours": hours,
                            "subject_person_id": student.id,
                            "text": (
                                f"Напоминание о занятии {first_name}\n\n"
                                f"{local_start:%d.%m.%Y в %H:%M}\n"
                                f"{lesson.subject_name_snapshot}\n"
                                f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                                f"Кабинет: {lesson.room_name_snapshot}"
                            ),
                        },
                    )
                )
        jobs.append(
            (
                f"teacher:{lesson.teacher_id}",
                lesson.teacher_id,
                {
                    "template": "teacher_reminder",
                    "hours": hours,
                    "text": (
                        f"Занятие через {hours} ч.\n\n"
                        f"{lesson.subject_name_snapshot}\n"
                        f"{local_start:%H:%M}–{local_end:%H:%M}\n"
                        f"Кабинет: {lesson.room_name_snapshot}"
                    ),
                },
            )
        )
        for key_suffix, recipient_id, payload in jobs:
            key = f"lesson:{lesson.id}:reminder:{hours}h:{key_suffix}"
            existing = await session.scalar(
                select(NotificationJob).where(NotificationJob.dedupe_key == key)
            )
            if existing is None:
                session.add(
                    NotificationJob(
                        dedupe_key=key,
                        event_type=f"lesson_reminder_{hours}h",
                        lesson_id=lesson.id,
                        recipient_person_id=recipient_id,
                        scheduled_at=scheduled_at,
                        payload=payload,
                    )
                )
            elif existing.status in {"pending", "retry"}:
                existing.scheduled_at = scheduled_at
                existing.payload = payload
            elif existing.status == "cancelled":
                existing.status = "pending"
                existing.scheduled_at = scheduled_at
                existing.payload = payload


async def _queue_lesson_state_notifications(
    session: AsyncSession,
    lesson: Lesson,
    participants: list[LessonParticipant],
    *,
    event: Literal["started", "finished"],
    center_timezone: str,
) -> None:
    active = [item for item in participants if item.attendance_status != "excused"]
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
        if event == "started":
            student_text = (
                "Занятие началось\n\n"
                f"Предмет: {lesson.subject_name_snapshot}\n"
                f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                f"Кабинет: {lesson.room_name_snapshot}"
            )
        else:
            student_text = (
                "Занятие завершено\n\n"
                f"{lesson.subject_name_snapshot}\n"
                f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                f"Фактически: {actual_start:%H:%M}–{actual_end:%H:%M}"
            )
        jobs = [(f"student:{person.id}", person.id, student_text)]
        name_parts = person.full_name.split()
        first_name = name_parts[1] if len(name_parts) > 1 else person.full_name
        for guardian_id in guardians.get(person.id, set()):
            if event == "started":
                text_value = (
                    f"{first_name} приступил(а) к занятию.\n\n"
                    f"Предмет: {lesson.subject_name_snapshot}\n"
                    f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                    f"Кабинет: {lesson.room_name_snapshot}"
                )
            else:
                text_value = (
                    f"Занятие {first_name} завершено.\n\n"
                    f"{lesson.subject_name_snapshot}\n"
                    f"Преподаватель: {lesson.teacher_name_snapshot}\n"
                    f"Фактически: {actual_start:%H:%M}–{actual_end:%H:%M}"
                )
            jobs.append((f"guardian:{guardian_id}:student:{person.id}", guardian_id, text_value))
        for suffix, recipient_id, text_value in jobs:
            key = f"lesson:{lesson.id}:{event}:{suffix}"
            existing_id = await session.scalar(
                select(NotificationJob.id).where(NotificationJob.dedupe_key == key)
            )
            if existing_id is None:
                session.add(
                    NotificationJob(
                        dedupe_key=key,
                        event_type=f"lesson_{event}",
                        lesson_id=lesson.id,
                        recipient_person_id=recipient_id,
                        scheduled_at=utcnow(),
                        payload={"text": text_value, "subject_person_id": person.id},
                    )
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
    verb = "пришёл в КРиТ" if event_type == "arrival" else "ушёл из КРиТ"
    for guardian_id in guardian_ids:
        key = f"presence:{event_type}:{person.id}:{occurred_at.isoformat()}:person:{guardian_id}"
        if (
            await session.scalar(
                select(NotificationJob.id).where(NotificationJob.dedupe_key == key)
            )
            is None
        ):
            session.add(
                NotificationJob(
                    dedupe_key=key,
                    event_type=f"presence_{event_type}",
                    recipient_person_id=guardian_id,
                    scheduled_at=occurred_at,
                    payload={
                        "text": (
                            f"{person.full_name} {verb} в "
                            f"{occurred_at.astimezone(_display_timezone(center_timezone)):%H:%M}."
                        )
                    },
                )
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
        elif lesson.status in {"planned", "scheduled"} and start_at + timedelta(
            minutes=5
        ) <= now:
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
        exists = await session.scalar(
            select(AdminNotification.id).where(
                AdminNotification.kind == kind,
                AdminNotification.lesson_id == lesson.id,
            )
        )
        if exists is None:
            session.add(
                AdminNotification(
                    kind=kind,
                    title=title,
                    message=message,
                    lesson_id=lesson.id,
                )
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
        return {
            "subjects": [_model(x, "id", "name", "color", "active") for x in subjects],
            "rooms": [_model(x, "id", "name", "capacity", "active") for x in rooms],
            "groups": [
                _model(
                    x,
                    "id",
                    "name",
                    "subject_id",
                    "default_teacher_id",
                    "default_duration_minutes",
                    "active",
                )
                for x in groups
            ],
            "teachers": [_model(x, "id", "full_name", "phone", "active") for x in teachers],
            "students": [_model(x, "id", "full_name", "phone", "active") for x in students],
        }

    @router.post("/subjects", status_code=status.HTTP_201_CREATED)
    async def create_subject(payload: SubjectPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = Subject(
                name=" ".join(payload.name.split()), color=payload.color, active=payload.active
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(409, "Предмет с таким названием уже существует") from exc
            return _model(item, "id", "name", "color", "active")

    @router.put("/subjects/{item_id}")
    async def update_subject(item_id: int, payload: SubjectPayload) -> dict[str, Any]:
        async with sessions() as session:
            item = await session.get(Subject, item_id)
            if item is None:
                raise HTTPException(404)
            item.name, item.color, item.active, item.updated_at = (
                " ".join(payload.name.split()),
                payload.color,
                payload.active,
                utcnow(),
            )
            await session.commit()
            return _model(item, "id", "name", "color", "active")

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
            item.name, item.capacity, item.active, item.updated_at = (
                " ".join(payload.name.split()),
                payload.capacity,
                payload.active,
                utcnow(),
            )
            await session.commit()
            return _model(item, "id", "name", "capacity", "active")

    @router.post("/groups", status_code=status.HTTP_201_CREATED)
    async def create_group(payload: GroupPayload) -> dict[str, Any]:
        async with sessions() as session:
            if payload.subject_id and await session.get(Subject, payload.subject_id) is None:
                raise HTTPException(422, "Предмет не найден")
            if payload.default_teacher_id is not None:
                teacher_role = await session.scalar(
                    select(PersonRole.person_id).where(
                        PersonRole.person_id == payload.default_teacher_id,
                        PersonRole.role == "teacher",
                    )
                )
                if teacher_role is None:
                    raise HTTPException(422, "Преподаватель группы не найден")
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
            if payload.default_teacher_id is not None:
                teacher_role = await session.scalar(
                    select(PersonRole.person_id).where(
                        PersonRole.person_id == payload.default_teacher_id,
                        PersonRole.role == "teacher",
                    )
                )
                if teacher_role is None:
                    raise HTTPException(422, "Преподаватель группы не найден")
            item.name, item.subject_id, item.default_teacher_id = (
                " ".join(payload.name.split()),
                payload.subject_id,
                payload.default_teacher_id,
            )
            item.default_duration_minutes, item.active, item.updated_at = (
                payload.default_duration_minutes,
                payload.active,
                utcnow(),
            )
            await session.commit()
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
            if (
                await session.get(StudyGroup, group_id) is None
                or await session.get(Person, payload.person_id) is None
            ):
                raise HTTPException(404)
            overlap = await session.scalar(
                select(GroupMembership.id).where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.person_id == payload.person_id,
                    GroupMembership.start_at < (payload.end_at or datetime.max.replace(tzinfo=UTC)),
                    or_(
                        GroupMembership.end_at.is_(None), GroupMembership.end_at > payload.start_at
                    ),
                )
            )
            if overlap is not None:
                raise HTTPException(409, "Период участия пересекается с существующим")
            item = GroupMembership(
                group_id=group_id,
                person_id=payload.person_id,
                start_at=_aware(payload.start_at),
                end_at=_aware(payload.end_at) if payload.end_at else None,
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
                item.end_at = _aware(payload.end_at) if payload.end_at else utcnow()
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
            items = list(
                (await session.scalars(query.order_by(Lesson.start_at))).all()
            )
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
                    .where(ClubPresenceSession.left_at.is_(None))
                    .order_by(ClubPresenceSession.arrived_at)
                )
            ).all()
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
                        select(LessonParticipant).where(
                            LessonParticipant.lesson_id == lesson_id
                        )
                    )
                ).all()
            )
            return _lesson_view(item, participants)

    async def save_lesson(
        session: AsyncSession,
        payload: LessonPayload,
        *,
        series_id: int | None = None,
        exclude_id: int | None = None,
        conflict_exclude_ids: set[int] | None = None,
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
        else:
            item = await session.get(Lesson, exclude_id)
            if item is None or item.status in {"completed", "cancelled"}:
                raise HTTPException(409, "Завершённое или отменённое занятие изменять нельзя")
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
            await session.execute(
                LessonParticipant.__table__.delete().where(LessonParticipant.lesson_id == item.id)
            )
            await session.execute(
                NotificationJob.__table__.update()
                .where(
                    NotificationJob.lesson_id == item.id,
                    NotificationJob.status.in_(["pending", "retry"]),
                )
                .values(status="cancelled")
            )
        participant_links = [
            LessonParticipant(
                lesson_id=item.id,
                person_id=person.id,
                person_name_snapshot=person.full_name,
            )
            for person in participants
        ]
        session.add_all(participant_links)
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
            item = await save_lesson(session, payload)
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
                        select(LessonParticipant).where(
                            LessonParticipant.lesson_id == lesson_id
                        )
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
            item = await save_lesson(session, payload, exclude_id=lesson_id)
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
                item = await save_lesson(session, lesson_payload, series_id=series.id)
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
                        .order_by(Lesson.start_at)
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
                lessons = [item for item in lessons if item.start_at >= anchor.start_at]
            lessons = [item for item in lessons if item.status not in {"completed", "cancelled"}]
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
            for index, existing in enumerate(lessons):
                start = base_start + timedelta(weeks=index * payload.interval_weeks)
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
                    exclude_id=existing.id,
                    conflict_exclude_ids=target_ids,
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
            present_ids = set(
                (
                    await session.scalars(
                        select(ClubPresenceSession.person_id).where(
                            ClubPresenceSession.left_at.is_(None)
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
            for participant in participants:
                if (
                    participant.attendance_status == "expected"
                    and participant.person_id in present_ids
                ):
                    participant.attendance_status = "present"
                    participant.arrived_at = item.actual_start_at
                    participant.late_minutes = 0
            if missing:
                session.add(
                    AdminNotification(
                        kind="lesson_missing_participants",
                        title="Не все участники пришли",
                        message=(
                            f"{item.subject_name_snapshot}: отсутствуют " + ", ".join(missing)
                        ),
                        lesson_id=item.id,
                    )
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
            return {
                **_lesson_view(item, participants),
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
            if item.status != "in_progress":
                raise HTTPException(409, "Занятие не идёт")
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(LessonParticipant.lesson_id == lesson_id)
                    )
                ).all()
            )
            actual_end = utcnow()
            for participant in participants:
                if participant.attendance_status == "expected":
                    participant.attendance_status = "absent"
                elif participant.attendance_status in {"present", "late"}:
                    participant.left_at = actual_end
            item.status, item.actual_end_at, item.updated_at = "completed", actual_end, actual_end
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="lesson.finished",
                    entity_type="lesson",
                    entity_id=item.id,
                )
            )
            await _queue_lesson_state_notifications(
                session,
                item,
                participants,
                event="finished",
                center_timezone=center_timezone,
            )
            await session.commit()
            return _lesson_view(item, participants)

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
            if item.status == "completed":
                raise HTTPException(409, "Завершённое занятие отменить нельзя")
            item.status, item.cancelled_reason, item.updated_at = (
                "cancelled",
                payload.reason,
                utcnow(),
            )
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
            guardians = set(
                (
                    await session.scalars(
                        select(StudentGuardian.guardian_id).where(
                            StudentGuardian.student_id.in_(participant_ids),
                            StudentGuardian.guardian_id != StudentGuardian.student_id,
                        )
                    )
                ).all()
            )
            recipients = participant_ids | guardians | {item.teacher_id}
            local_start = _db_utc(item.start_at).astimezone(_display_timezone(center_timezone))
            for recipient_id in recipients:
                session.add(
                    NotificationJob(
                        dedupe_key=f"lesson:{item.id}:cancelled:person:{recipient_id}",
                        event_type="lesson_cancelled",
                        lesson_id=item.id,
                        recipient_person_id=recipient_id,
                        scheduled_at=utcnow(),
                        payload={
                            "text": (
                                f"Занятие «{item.subject_name_snapshot}» "
                                f"{local_start:%d.%m в %H:%M} "
                                f"отменено. Причина: {payload.reason}"
                            )
                        },
                    )
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
        allowed = {"expected", "present", "late", "absent", "left_early", "excused"}
        if payload.status not in allowed:
            raise HTTPException(422, "Неизвестный статус посещения")
        async with sessions() as session:
            item = await session.scalar(
                select(LessonParticipant).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if item is None:
                raise HTTPException(404)
            before = {"status": item.attendance_status, "note": item.note}
            item.attendance_status, item.note = payload.status, payload.note
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
            await session.flush()
            await _queue_lesson_notifications(session, lesson, [participant], center_timezone)
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
            participant = await session.scalar(
                select(LessonParticipant).where(
                    LessonParticipant.lesson_id == lesson_id,
                    LessonParticipant.person_id == person_id,
                )
            )
            if participant is None:
                raise HTTPException(404)
            participant.attendance_status = state
            lesson = await session.get(Lesson, lesson_id)
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
                participant.cancelled_at = utcnow()
                participant.cancelled_by = cancellation.cancelled_by
                participant.cancelled_by_person_id = actor_person_id
                participant.cancelled_by_admin_id = (
                    admin_id if cancellation.cancelled_by == "administrator" else None
                )
                participant.cancellation_reason = cancellation.reason.strip()
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
                participant.cancelled_at = None
                participant.cancelled_by = None
                participant.cancelled_by_person_id = None
                participant.cancelled_by_admin_id = None
                participant.cancellation_reason = None
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
                await _queue_lesson_notifications(session, lesson, [participant], center_timezone)
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
            participant.attendance_status = payload.attendance_status
            participant.arrived_at = _aware(payload.arrived_at) if payload.arrived_at else None
            participant.left_at = _aware(payload.left_at) if payload.left_at else None
            if payload.attendance_status == "excused":
                participant.cancelled_at = utcnow()
                participant.cancelled_by = "administrator"
                participant.cancelled_by_person_id = None
                participant.cancelled_by_admin_id = admin_id
                participant.cancellation_reason = payload.reason.strip()
            else:
                participant.cancelled_at = None
                participant.cancelled_by = None
                participant.cancelled_by_person_id = None
                participant.cancelled_by_admin_id = None
                participant.cancellation_reason = None
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
            before = {
                "actual_start_at": lesson.actual_start_at.isoformat()
                if lesson.actual_start_at
                else None,
                "actual_end_at": lesson.actual_end_at.isoformat()
                if lesson.actual_end_at
                else None,
            }
            lesson.actual_start_at = _aware(payload.actual_start_at)
            lesson.actual_end_at = _aware(payload.actual_end_at)
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
                    details={"before": before, "after": after, "reason": payload.reason},
                )
            )
            await session.commit()
            participants = list(
                (
                    await session.scalars(
                        select(LessonParticipant).where(
                            LessonParticipant.lesson_id == lesson_id
                        )
                    )
                ).all()
            )
            return _lesson_view(lesson, participants)

    @router.post("/presence/{person_id}/arrival", status_code=status.HTTP_201_CREATED)
    async def arrival(
        person_id: int,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
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
                participant.arrived_at = now
                participant.late_minutes = max(
                    0, int((now - _db_utc(lesson.start_at)).total_seconds() // 60)
                )
                participant.attendance_status = "late" if participant.late_minutes else "present"
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
            await session.commit()
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
                participant.left_at, participant.attendance_status = now, "left_early"
                warnings.append(
                    {
                        "kind": "active_lesson",
                        "lesson_id": lesson.id,
                        "message": "Участник ушёл во время занятия",
                    }
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
            lessons = list(
                (
                    await session.scalars(
                        select(Lesson)
                        .where(Lesson.teacher_id == person_id)
                        .order_by(Lesson.start_at.desc())
                    )
                ).all()
            )
            ids = [item.id for item in lessons]
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
            return {
                "lessons": [_lesson_view(item, by_lesson.get(item.id, [])) for item in lessons],
                "summary": {
                    "total": len(lessons),
                    "completed": sum(item.status == "completed" for item in lessons),
                    "cancelled": sum(item.status == "cancelled" for item in lessons),
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
            lessons = list(
                (await session.scalars(query.order_by(Lesson.start_at))).all()
            )
            counts = (
                dict(
                    (
                        await session.execute(
                            select(
                                LessonParticipant.lesson_id,
                                func.count(LessonParticipant.id),
                            )
                            .where(LessonParticipant.lesson_id.in_([item.id for item in lessons]))
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
                            Lesson.start_at < end,
                            Lesson.end_at > start,
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
                    if not (
                        _db_utc(lesson.start_at) < candidate_end
                        and _db_utc(lesson.end_at) > cursor
                    ):
                        continue
                    lesson_people = participants_by_lesson.get(lesson.id, set())
                    if lesson.room_id == room.id:
                        conflict = True
                    if teacher_id is not None and (
                        lesson.teacher_id == teacher_id or teacher_id in lesson_people
                    ):
                        conflict = True
                    if lesson.teacher_id in selected_students or lesson_people & selected_students:
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
