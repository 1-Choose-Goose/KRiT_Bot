from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .communication_models import (
    CommunicationCampaign,
    CommunicationMessage,
    CommunicationThread,
    GuardianNotificationOverride,
    InteractionRequest,
    InteractionRequestLesson,
    InteractionResponse,
    InteractionResponseHistory,
    LessonAttendanceIntent,
    NotificationGlobalRule,
    PersonNotificationOverride,
    SchedulePublication,
    ScheduleRecipientSnapshot,
)
from .db import Person, StudentGuardian, utcnow
from .learning_models import (
    AuditEvent,
    GroupMembership,
    Lesson,
    LessonParticipant,
    NotificationJob,
    PersonMaxIdentity,
    StudyGroup,
)


class NotificationEvent(StrEnum):
    SCHEDULE_PUBLISHED = "schedule_published"
    SCHEDULE_CHANGED = "schedule_changed"
    LESSON_REMINDER = "lesson_reminder"
    LESSON_CONFIRMATION_REQUEST = "lesson_confirmation_request"
    LESSON_CANCELLED = "lesson_cancelled"
    LESSON_STARTED = "lesson_started"
    LESSON_PARTICIPANT_STARTED = "lesson_participant_started"
    LESSON_FINISHED = "lesson_finished"
    STUDENT_ARRIVED_CLUB = "student_arrived_club"
    STUDENT_LEFT_CLUB = "student_left_club"
    PARTICIPANT_ADDED = "participant_added"
    PARTICIPANT_REMOVED = "participant_removed"
    TEACHER_REPLACED = "teacher_replaced"
    CUSTOM_MESSAGE = "custom_message"
    CUSTOM_YES_NO_REQUEST = "custom_yes_no_request"
    REGISTRATION_MESSAGE = "registration_message"
    SUBSCRIPTION_REQUIRED = "subscription_required"


RECIPIENT_CONTEXTS = {"student", "guardian", "teacher"}
PRIORITY_VALUE = {"low": 0, "normal": 1, "high": 2}
DEFAULT_RULES = {
    NotificationEvent.LESSON_REMINDER: (1440, 180, 60),
    NotificationEvent.LESSON_CONFIRMATION_REQUEST: (1440, 180, 60),
}


async def ensure_default_rules(session: AsyncSession) -> None:
    """Seed defaults for create_all based test databases as well as migrated databases."""
    existing = set(
        (
            await session.execute(
                select(
                    NotificationGlobalRule.event_code,
                    NotificationGlobalRule.recipient_context,
                    NotificationGlobalRule.offset_minutes,
                )
            )
        ).all()
    )
    rows: list[NotificationGlobalRule] = []
    for context in sorted(RECIPIENT_CONTEXTS):
        for offset in DEFAULT_RULES[NotificationEvent.LESSON_REMINDER]:
            key = (NotificationEvent.LESSON_REMINDER.value, context, offset)
            if key not in existing:
                rows.append(
                    NotificationGlobalRule(
                        event_code=key[0],
                        recipient_context=context,
                        offset_minutes=offset,
                        configuration={"after_confirmation": "one_hour_only"},
                    )
                )
        if context != "teacher":
            for offset in DEFAULT_RULES[NotificationEvent.LESSON_CONFIRMATION_REQUEST]:
                confirmation_key = (
                    NotificationEvent.LESSON_CONFIRMATION_REQUEST.value,
                    context,
                    offset,
                )
                if confirmation_key not in existing:
                    rows.append(
                        NotificationGlobalRule(
                            event_code=confirmation_key[0],
                            recipient_context=context,
                            offset_minutes=offset,
                            enabled=offset == 1440,
                            requires_confirmation=True,
                            configuration={
                                "deadline_minutes": 60,
                                "follow_up": "once",
                                "follow_up_offset_minutes": 180,
                            },
                        )
                    )
    for event in NotificationEvent:
        if event in {
            NotificationEvent.LESSON_REMINDER,
            NotificationEvent.LESSON_CONFIRMATION_REQUEST,
        }:
            continue
        for context in sorted(RECIPIENT_CONTEXTS):
            key = (event.value, context, -1)
            if key not in existing:
                rows.append(
                    NotificationGlobalRule(
                        event_code=event.value,
                        recipient_context=context,
                        offset_minutes=-1,
                        priority=(
                            "high"
                            if event
                            in {
                                NotificationEvent.LESSON_CANCELLED,
                                NotificationEvent.SCHEDULE_CHANGED,
                                NotificationEvent.TEACHER_REPLACED,
                            }
                            else "normal"
                        ),
                        quiet_hours_policy=(
                            "bypass"
                            if event
                            in {
                                NotificationEvent.LESSON_CANCELLED,
                                NotificationEvent.TEACHER_REPLACED,
                            }
                            else "defer"
                        ),
                    )
                )
    # More than one API/reconciliation worker can enter this bootstrap path on
    # the first start after an upgrade.  Isolate every insert in a savepoint so
    # the unique rule key resolves the race without aborting the outer work.
    for row in rows:
        async with session.begin_nested():
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                pass
    confirmation_rules = list(
        (
            await session.scalars(
                select(NotificationGlobalRule).where(
                    NotificationGlobalRule.event_code
                    == NotificationEvent.LESSON_CONFIRMATION_REQUEST
                )
            )
        ).all()
    )
    for rule in confirmation_rules:
        configuration = dict(rule.configuration or {})
        configuration.setdefault("deadline_minutes", 60)
        configuration.setdefault("follow_up", "once")
        configuration.setdefault("follow_up_offset_minutes", 180)
        if configuration != (rule.configuration or {}):
            rule.configuration = configuration


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    enabled: bool
    priority: str
    quiet_hours_policy: str
    quiet_start: str | None
    quiet_end: str | None
    configuration: dict[str, Any]
    source: str


@dataclass(frozen=True, slots=True)
class BundleLesson:
    id: int
    revision: int
    start_at: datetime
    end_at: datetime
    subject: str
    teacher: str
    room: str
    students: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyBundle:
    local_date: date
    subject_person_id: int
    recipient_person_id: int
    recipient_context: str
    subject_name: str
    lessons: tuple[BundleLesson, ...]

    @property
    def anchor_start_at(self) -> datetime:
        return self.lessons[0].start_at

    @property
    def fingerprint(self) -> str:
        canonical = [[item.id, item.revision] for item in self.lessons]
        return hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

    @property
    def base_key(self) -> str:
        return (
            f"daily:{self.local_date.isoformat()}:{self.subject_person_id}:"
            f"{self.recipient_person_id}:{self.recipient_context}"
        )


class NotificationPolicyResolver:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve(
        self,
        *,
        recipient_person_id: int,
        recipient_context: str,
        event_code: str,
        offset_minutes: int | None = None,
        subject_person_id: int | None = None,
    ) -> EffectivePolicy:
        if recipient_context not in RECIPIENT_CONTEXTS:
            raise ValueError("Unknown recipient context")
        offset = -1 if offset_minutes is None else offset_minutes
        rules = list(
            (
                await self.session.scalars(
                    select(NotificationGlobalRule).where(
                        NotificationGlobalRule.event_code == event_code,
                        NotificationGlobalRule.offset_minutes == offset,
                        NotificationGlobalRule.recipient_context.in_(["*", recipient_context]),
                    )
                )
            ).all()
        )
        rule = next((item for item in rules if item.recipient_context == recipient_context), None)
        rule = rule or next((item for item in rules if item.recipient_context == "*"), None)
        enabled = rule.enabled if rule is not None else True
        source = "context" if rule and rule.recipient_context != "*" else "global"
        configuration = dict(rule.configuration or {}) if rule else {}
        priority = rule.priority if rule else "normal"
        quiet_policy = rule.quiet_hours_policy if rule else "defer"
        quiet_start = rule.quiet_start if rule else "22:00"
        quiet_end = rule.quiet_end if rule else "08:00"

        override = await self.session.scalar(
            select(PersonNotificationOverride).where(
                PersonNotificationOverride.person_id == recipient_person_id,
                PersonNotificationOverride.recipient_context == recipient_context,
                PersonNotificationOverride.event_code == event_code,
                PersonNotificationOverride.offset_minutes == offset,
            )
        )
        if override is not None:
            configuration.update(override.configuration or {})
            if override.state != "inherit":
                enabled = override.state == "on"
                source = "person"

        if recipient_context == "guardian" and subject_person_id is not None:
            child_override = await self.session.scalar(
                select(GuardianNotificationOverride).where(
                    GuardianNotificationOverride.guardian_person_id == recipient_person_id,
                    GuardianNotificationOverride.student_person_id == subject_person_id,
                    GuardianNotificationOverride.event_code == event_code,
                    GuardianNotificationOverride.offset_minutes == offset,
                )
            )
            if child_override is not None:
                configuration.update(child_override.configuration or {})
                if child_override.state != "inherit":
                    enabled = child_override.state == "on"
                    source = "guardian_child"
        return EffectivePolicy(
            enabled=enabled,
            priority=priority,
            quiet_hours_policy=quiet_policy,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            configuration=configuration,
            source=source,
        )


def apply_quiet_hours(
    scheduled_at: datetime,
    *,
    policy: EffectivePolicy,
    timezone: ZoneInfo,
    meaningful_until: datetime | None = None,
) -> datetime | None:
    if policy.quiet_hours_policy == "bypass" or not policy.quiet_start or not policy.quiet_end:
        return scheduled_at
    start = time.fromisoformat(policy.quiet_start)
    end = time.fromisoformat(policy.quiet_end)
    local = scheduled_at.astimezone(timezone)
    inside = (
        local.time() >= start or local.time() < end if start > end else start <= local.time() < end
    )
    if not inside:
        return scheduled_at
    end_date = local.date() + (
        timedelta(days=1) if start > end and local.time() >= start else timedelta()
    )
    deferred = datetime.combine(end_date, end, tzinfo=timezone).astimezone(UTC)
    if meaningful_until is not None and deferred >= meaningful_until.astimezone(UTC):
        return None
    return deferred


def render_bundle(bundle: DailyBundle, *, confirmation: bool = False) -> str:
    if bundle.recipient_context == "teacher":
        heading = "Ваши занятия"
    elif bundle.recipient_context == "guardian":
        heading = f"Занятия {bundle.subject_name}"
    else:
        heading = "Ваши занятия КРиТ"
    day = bundle.local_date.strftime("%d.%m.%Y")
    blocks = []
    for lesson in bundle.lessons:
        lines = [
            f"{lesson.start_at:%H:%M}–{lesson.end_at:%H:%M}",
            lesson.subject,
        ]
        if bundle.recipient_context != "teacher":
            lines.append(lesson.teacher)
        lines.append(lesson.room)
        if bundle.recipient_context == "teacher":
            lines.append("Ученики: " + (", ".join(lesson.students) if lesson.students else "нет"))
        blocks.append("\n".join(lines))
    suffix = "\n\nПодтвердите присутствие." if confirmation else ""
    return f"{heading}\n{day}\n\n" + "\n\n".join(blocks) + suffix


async def daily_bundles(
    session: AsyncSession,
    *,
    date_from: datetime,
    date_to: datetime,
    timezone: ZoneInfo,
) -> list[DailyBundle]:
    rows = (
        await session.execute(
            select(Lesson, LessonParticipant, Person)
            .join(LessonParticipant, LessonParticipant.lesson_id == Lesson.id)
            .join(Person, Person.id == LessonParticipant.person_id)
            .where(
                Lesson.start_at >= date_from.astimezone(UTC),
                Lesson.start_at < date_to.astimezone(UTC),
                Lesson.status != "cancelled",
                LessonParticipant.attendance_status != "excused",
            )
            .order_by(Lesson.start_at, Lesson.id)
        )
    ).all()
    lesson_students: dict[int, list[str]] = defaultdict(list)
    lesson_by_id: dict[int, Lesson] = {}
    students: dict[int, Person] = {}
    for lesson, participant, student in rows:
        lesson_by_id[lesson.id] = lesson
        students[student.id] = student
        lesson_students[lesson.id].append(participant.person_name_snapshot)
    guardian_rows = (
        await session.execute(
            select(StudentGuardian.student_id, StudentGuardian.guardian_id, Person.full_name)
            .join(Person, Person.id == StudentGuardian.guardian_id)
            .where(StudentGuardian.student_id.in_(students) if students else False)
        )
    ).all()
    guardians: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for student_id, guardian_id, guardian_name in guardian_rows:
        if guardian_id != student_id:
            guardians[student_id].append((guardian_id, guardian_name))

    grouped: dict[tuple[date, int, int, str], list[BundleLesson]] = defaultdict(list)
    names: dict[tuple[date, int, int, str], str] = {}
    for lesson, _participant, student in rows:
        local_start = lesson.start_at.astimezone(timezone)
        local_end = lesson.end_at.astimezone(timezone)
        item = BundleLesson(
            id=lesson.id,
            revision=lesson.notification_revision,
            start_at=local_start,
            end_at=local_end,
            subject=lesson.subject_name_snapshot,
            teacher=lesson.teacher_name_snapshot,
            room=lesson.room_name_snapshot,
            students=tuple(sorted(lesson_students[lesson.id])),
        )
        student_key = (local_start.date(), student.id, student.id, "student")
        if item not in grouped[student_key]:
            grouped[student_key].append(item)
            names[student_key] = student.full_name
        for guardian_id, _guardian_name in guardians.get(student.id, []):
            key = (local_start.date(), student.id, guardian_id, "guardian")
            if item not in grouped[key]:
                grouped[key].append(item)
                names[key] = student.full_name
        teacher_key = (local_start.date(), lesson.teacher_id, lesson.teacher_id, "teacher")
        if item not in grouped[teacher_key]:
            grouped[teacher_key].append(item)
            names[teacher_key] = lesson.teacher_name_snapshot
    return [
        DailyBundle(
            local_date=key[0],
            subject_person_id=key[1],
            recipient_person_id=key[2],
            recipient_context=key[3],
            subject_name=names[key],
            lessons=tuple(sorted(items, key=lambda item: (item.start_at, item.id))),
        )
        for key, items in sorted(grouped.items(), key=lambda pair: pair[0])
    ]


async def enqueue_job(session: AsyncSession, **values: Any) -> NotificationJob | None:
    job = NotificationJob(**values)
    async with session.begin_nested():
        session.add(job)
        try:
            await session.flush()
        except IntegrityError:
            return None
    return job


async def refresh_campaign_status(session: AsyncSession, campaign_id: int | None) -> None:
    """Derive a campaign status from its single, persistent outbox."""
    if campaign_id is None:
        return
    campaign = await session.get(CommunicationCampaign, campaign_id)
    if campaign is None:
        return
    counts = {
        status: count
        for status, count in (
            await session.execute(
                select(NotificationJob.status, func.count(NotificationJob.id))
                .where(NotificationJob.campaign_id == campaign_id)
                .group_by(NotificationJob.status)
            )
        ).all()
    }
    if counts.get("processing"):
        status = "sending"
    elif counts.get("pending") or counts.get("retry"):
        status = "scheduled"
    else:
        delivered = counts.get("sent", 0)
        unsuccessful = counts.get("failed", 0) + counts.get("cancelled", 0)
        if delivered and unsuccessful:
            status = "partial"
        elif unsuccessful:
            status = "failed"
        else:
            status = "completed"
    campaign.status = status
    campaign.updated_at = utcnow()


async def reconcile_daily_reminders(
    session: AsyncSession,
    *,
    now: datetime,
    timezone: ZoneInfo,
    window_days: int = 7,
) -> int:
    await ensure_default_rules(session)
    local_now = now.astimezone(timezone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    end = start + timedelta(days=window_days + 1)
    bundles = await daily_bundles(session, date_from=start, date_to=end, timezone=timezone)
    created = 0
    resolver = NotificationPolicyResolver(session)
    for bundle in bundles:
        first_lesson = bundle.lessons[0]
        has_response = (
            await session.scalar(
                select(func.count(InteractionResponse.id))
                .join(
                    InteractionRequest,
                    InteractionRequest.id == InteractionResponse.request_id,
                )
                .join(
                    InteractionRequestLesson,
                    InteractionRequestLesson.request_id == InteractionRequest.id,
                )
                .where(
                    InteractionRequest.recipient_person_id == bundle.recipient_person_id,
                    InteractionRequest.recipient_context == bundle.recipient_context,
                    InteractionRequest.subject_person_id == bundle.subject_person_id,
                    InteractionRequestLesson.lesson_id == first_lesson.id,
                    InteractionRequestLesson.lesson_revision == first_lesson.revision,
                )
            )
            or 0
        ) > 0
        for offset in DEFAULT_RULES[NotificationEvent.LESSON_REMINDER]:
            policy = await resolver.resolve(
                recipient_person_id=bundle.recipient_person_id,
                recipient_context=bundle.recipient_context,
                subject_person_id=bundle.subject_person_id,
                event_code=NotificationEvent.LESSON_REMINDER,
                offset_minutes=offset,
            )
            prefix = f"{bundle.base_key}:reminder:{offset}:"
            existing = await session.scalar(
                select(NotificationJob).where(NotificationJob.dedupe_key.like(prefix + "%"))
            )
            after_confirmation = policy.configuration.get(
                "after_confirmation", "one_hour_only"
            )
            suppressed = has_response and (
                after_confirmation == "none"
                or (after_confirmation == "one_hour_only" and offset != 60)
            )
            if existing is not None and existing.status == "sent":
                continue
            scheduled = bundle.anchor_start_at.astimezone(UTC) - timedelta(minutes=offset)
            scheduled = apply_quiet_hours(
                scheduled,
                policy=policy,
                timezone=timezone,
                meaningful_until=bundle.anchor_start_at.astimezone(UTC),
            )
            if existing is not None:
                if not policy.enabled or suppressed or scheduled is None:
                    if existing.status in {"pending", "retry"}:
                        existing.status = "cancelled"
                    continue
                if existing.status in {"pending", "retry", "cancelled"}:
                    existing.dedupe_key = prefix + bundle.fingerprint
                    # Keep the relational anchor in sync with the recalculated
                    # daily bundle.  Otherwise cancelling the former first
                    # lesson can leave a valid replacement job pointing at the
                    # cancelled lesson even though its payload already starts
                    # with the new anchor.
                    existing.lesson_id = first_lesson.id
                    existing.scheduled_at = max(now, scheduled)
                    existing.status = "pending"
                    existing.payload = {
                        "text": render_bundle(bundle),
                        "bundle_fingerprint": bundle.fingerprint,
                        "bundle_base_key": bundle.base_key,
                        "lesson_ids": [item.id for item in bundle.lessons],
                    }
                continue
            if (
                not policy.enabled
                or suppressed
                or scheduled is None
                or scheduled >= bundle.anchor_start_at.astimezone(UTC)
            ):
                continue
            job = await enqueue_job(
                session,
                dedupe_key=prefix + bundle.fingerprint,
                event_type=NotificationEvent.LESSON_REMINDER,
                lesson_id=bundle.lessons[0].id,
                recipient_person_id=bundle.recipient_person_id,
                subject_person_id=bundle.subject_person_id,
                recipient_context=bundle.recipient_context,
                priority=PRIORITY_VALUE[policy.priority],
                scheduled_at=max(now, scheduled),
                payload={
                    "text": render_bundle(bundle),
                    "bundle_fingerprint": bundle.fingerprint,
                    "bundle_base_key": bundle.base_key,
                    "lesson_ids": [item.id for item in bundle.lessons],
                },
            )
            created += int(job is not None)
    return created


async def reconcile_confirmation_requests(
    session: AsyncSession,
    *,
    now: datetime,
    timezone: ZoneInfo,
    window_days: int = 7,
) -> int:
    """Create one confirmation per person/child/day, with lesson-level answers."""
    local_now = now.astimezone(timezone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    end = start + timedelta(days=window_days + 1)
    bundles = await daily_bundles(session, date_from=start, date_to=end, timezone=timezone)
    resolver = NotificationPolicyResolver(session)
    created = 0
    for bundle in bundles:
        # Attendance is confirmed by the student and their guardian. Teachers
        # receive the schedule/reminders, but do not answer for a pupil.
        if bundle.recipient_context == "teacher":
            continue
        selected: tuple[int, EffectivePolicy] | None = None
        for offset in DEFAULT_RULES[NotificationEvent.LESSON_CONFIRMATION_REQUEST]:
            candidate = await resolver.resolve(
                recipient_person_id=bundle.recipient_person_id,
                recipient_context=bundle.recipient_context,
                subject_person_id=bundle.subject_person_id,
                event_code=NotificationEvent.LESSON_CONFIRMATION_REQUEST,
                offset_minutes=offset,
            )
            if candidate.enabled:
                selected = (offset, candidate)
                break
        if selected is None:
            continue
        offset, policy = selected
        scheduled = apply_quiet_hours(
            bundle.anchor_start_at.astimezone(UTC) - timedelta(minutes=offset),
            policy=policy,
            timezone=timezone,
            meaningful_until=bundle.anchor_start_at.astimezone(UTC),
        )
        if scheduled is None or scheduled >= bundle.anchor_start_at.astimezone(UTC):
            continue
        prefix = f"{bundle.base_key}:confirmation:"
        current_job = await session.scalar(
            select(NotificationJob).where(NotificationJob.dedupe_key == prefix + bundle.fingerprint)
        )
        if current_job is not None:
            request = (
                await session.get(InteractionRequest, current_job.interaction_request_id)
                if current_job.interaction_request_id is not None
                else None
            )
            if request is not None and request.status == "active":
                await _enqueue_confirmation_follow_up(
                    session,
                    bundle=bundle,
                    request=request,
                    policy=policy,
                    now=now,
                    timezone=timezone,
                    keyboard=list(current_job.payload.get("keyboard") or []),
                    initial_scheduled_at=current_job.scheduled_at,
                )
            continue
        stale_jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(
                        NotificationJob.dedupe_key.like(prefix + "%"),
                        NotificationJob.status.in_(["pending", "retry"]),
                    )
                )
            ).all()
        )
        for stale in stale_jobs:
            stale.status = "cancelled"
            if stale.interaction_request_id:
                old_request = await session.get(InteractionRequest, stale.interaction_request_id)
                if old_request and old_request.status in {"active", "draft"}:
                    old_request.status = "cancelled"
        request = InteractionRequest(
            request_type="lesson_confirmation",
            question=render_bundle(bundle, confirmation=True),
            recipient_person_id=bundle.recipient_person_id,
            recipient_context=bundle.recipient_context,
            subject_person_id=bundle.subject_person_id,
            related_lesson_id=bundle.lessons[0].id,
            expires_at=max(
                max(now, scheduled) + timedelta(minutes=1),
                bundle.anchor_start_at.astimezone(UTC)
                - timedelta(minutes=int(policy.configuration.get("deadline_minutes", 60))),
            ),
        )
        session.add(request)
        await session.flush()
        for lesson in bundle.lessons:
            session.add(
                InteractionRequestLesson(
                    request_id=request.id,
                    lesson_id=lesson.id,
                    lesson_revision=lesson.revision,
                )
            )
            intent = await session.scalar(
                select(LessonAttendanceIntent).where(
                    LessonAttendanceIntent.lesson_id == lesson.id,
                    LessonAttendanceIntent.student_person_id == bundle.subject_person_id,
                    LessonAttendanceIntent.lesson_revision == lesson.revision,
                )
            )
            if intent is None:
                session.add(
                    LessonAttendanceIntent(
                        lesson_id=lesson.id,
                        student_person_id=bundle.subject_person_id,
                        lesson_revision=lesson.revision,
                        status="pending",
                        last_request_id=request.id,
                    )
                )
            else:
                intent.last_request_id = request.id
        keyboard = [
            [
                {
                    "type": "callback",
                    "text": "Буду",
                    "payload": f"interaction:{request.id}:yes",
                },
                {
                    "type": "callback",
                    "text": "Не буду",
                    "payload": f"interaction:{request.id}:no",
                },
            ]
        ]
        if len(bundle.lessons) > 1:
            keyboard.append(
                [
                    {
                        "type": "callback",
                        "text": "По занятиям",
                        "payload": f"interaction:{request.id}:partial",
                    }
                ]
            )
        job = await enqueue_job(
            session,
            dedupe_key=prefix + bundle.fingerprint,
            event_type=NotificationEvent.LESSON_CONFIRMATION_REQUEST,
            lesson_id=bundle.lessons[0].id,
            recipient_person_id=bundle.recipient_person_id,
            subject_person_id=bundle.subject_person_id,
            recipient_context=bundle.recipient_context,
            priority=PRIORITY_VALUE[policy.priority],
            scheduled_at=max(now, scheduled),
            interaction_request_id=request.id,
            payload={
                "text": request.question,
                "keyboard": keyboard,
                "lesson_ids": [item.id for item in bundle.lessons],
            },
        )
        created += int(job is not None)
        if job is not None:
            follow_up = await _enqueue_confirmation_follow_up(
                session,
                bundle=bundle,
                request=request,
                policy=policy,
                now=now,
                timezone=timezone,
                keyboard=keyboard,
                initial_scheduled_at=job.scheduled_at,
            )
            created += int(follow_up is not None)
    return created


async def _enqueue_confirmation_follow_up(
    session: AsyncSession,
    *,
    bundle: DailyBundle,
    request: InteractionRequest,
    policy: EffectivePolicy,
    now: datetime,
    timezone: ZoneInfo,
    keyboard: list[Any],
    initial_scheduled_at: datetime,
) -> NotificationJob | None:
    """Schedule the optional single nudge before the last daily reminder."""
    configuration = policy.configuration
    if configuration.get("follow_up", "once") == "none":
        return None
    follow_up_offset = int(configuration.get("follow_up_offset_minutes", 180))
    if follow_up_offset <= 60:
        # The nudge must precede the final one-hour reminder rather than race it.
        follow_up_offset = 180
    scheduled = apply_quiet_hours(
        bundle.anchor_start_at.astimezone(UTC) - timedelta(minutes=follow_up_offset),
        policy=policy,
        timezone=timezone,
        meaningful_until=bundle.anchor_start_at.astimezone(UTC),
    )
    initial = initial_scheduled_at
    if initial.tzinfo is None:
        initial = initial.replace(tzinfo=UTC)
    if scheduled is None or scheduled <= max(now, initial):
        return None
    return await enqueue_job(
        session,
        dedupe_key=f"{bundle.base_key}:confirmation:{bundle.fingerprint}:followup",
        event_type=NotificationEvent.LESSON_CONFIRMATION_REQUEST,
        lesson_id=bundle.lessons[0].id,
        recipient_person_id=bundle.recipient_person_id,
        subject_person_id=bundle.subject_person_id,
        recipient_context=bundle.recipient_context,
        priority=PRIORITY_VALUE[policy.priority],
        scheduled_at=scheduled,
        interaction_request_id=request.id,
        payload={
            "text": "Напоминание: ответ по занятиям ещё не получен.\n\n" + request.question,
            "keyboard": keyboard,
            "lesson_ids": [item.id for item in bundle.lessons],
            "follow_up": True,
        },
    )


async def expire_confirmation_requests(session: AsyncSession, *, now: datetime) -> int:
    requests = list(
        (
            await session.scalars(
                select(InteractionRequest).where(
                    InteractionRequest.request_type == "lesson_confirmation",
                    InteractionRequest.status == "active",
                    InteractionRequest.expires_at.is_not(None),
                    InteractionRequest.expires_at <= now,
                )
            )
        ).all()
    )
    for request in requests:
        request.status = "expired"
        request.updated_at = now
        links = list(
            (
                await session.scalars(
                    select(InteractionRequestLesson).where(
                        InteractionRequestLesson.request_id == request.id
                    )
                )
            ).all()
        )
        for link in links:
            intent = await session.scalar(
                select(LessonAttendanceIntent).where(
                    LessonAttendanceIntent.lesson_id == link.lesson_id,
                    LessonAttendanceIntent.student_person_id == request.subject_person_id,
                    LessonAttendanceIntent.lesson_revision == link.lesson_revision,
                    LessonAttendanceIntent.status == "pending",
                )
            )
            if intent is not None:
                intent.status = "no_response"
                intent.updated_at = now
    return len(requests)


async def record_message(
    session: AsyncSession,
    *,
    person_id: int,
    direction: str,
    text: str,
    delivery_status: str,
    message_type: str = "text",
    max_message_id: str | None = None,
    outbox_job_id: int | None = None,
    interaction_request_id: int | None = None,
    campaign_id: int | None = None,
    related_lesson_id: int | None = None,
    admin_id: int | None = None,
    notify_admin: bool | None = None,
) -> CommunicationMessage:
    now = utcnow()
    if outbox_job_id is not None:
        existing = await session.scalar(
            select(CommunicationMessage).where(
                CommunicationMessage.outbox_job_id == outbox_job_id
            )
        )
        if existing is not None:
            existing.text = text
            existing.delivery_status = delivery_status
            existing.max_message_id = max_message_id or existing.max_message_id
            existing.sent_at = now if delivery_status == "sent" else None
            return existing
    message = CommunicationMessage(
        person_id=person_id,
        direction=direction,
        message_type=message_type,
        text=text,
        max_message_id=max_message_id,
        delivery_status=delivery_status,
        outbox_job_id=outbox_job_id,
        interaction_request_id=interaction_request_id,
        campaign_id=campaign_id,
        related_lesson_id=related_lesson_id,
        admin_id=admin_id,
        sent_at=now if delivery_status == "sent" else None,
        created_at=now,
    )
    session.add(message)
    thread = await session.get(CommunicationThread, person_id)
    if thread is None:
        thread = CommunicationThread(person_id=person_id, admin_unread_count=0)
        session.add(thread)
    should_notify = (
        direction == "inbound" and message_type == "text"
        if notify_admin is None
        else notify_admin
    )
    if direction == "outbound" or should_notify:
        thread.last_message_at = now
        thread.last_message_preview = text[:240]
    if should_notify:
        thread.admin_unread_count = (thread.admin_unread_count or 0) + 1
    await session.flush()
    return message


async def cleanup_communication_history(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Delete only disposable chat history, never business confirmations/audit."""
    cutoff = now - timedelta(days=30)
    rows = list(
        (
            await session.execute(
                select(CommunicationMessage.id, CommunicationMessage.person_id)
                .where(
                    or_(
                        CommunicationMessage.created_at < cutoff,
                        CommunicationMessage.delivery_status.in_(["failed", "unavailable"]),
                    )
                )
                .order_by(CommunicationMessage.id)
                .limit(limit)
            )
        ).all()
    )
    ids = [message_id for message_id, _person_id in rows]
    if ids:
        await session.execute(delete(CommunicationMessage).where(CommunicationMessage.id.in_(ids)))
        person_ids = {person_id for _message_id, person_id in rows}
        ranked_messages = (
            select(
                CommunicationMessage.id.label("message_id"),
                CommunicationMessage.person_id,
                func.row_number()
                .over(
                    partition_by=CommunicationMessage.person_id,
                    order_by=(
                        CommunicationMessage.created_at.desc(),
                        CommunicationMessage.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(
                CommunicationMessage.person_id.in_(person_ids),
                or_(
                    (
                        (CommunicationMessage.direction == "inbound")
                        & (CommunicationMessage.message_type == "text")
                    ),
                    (
                        (CommunicationMessage.direction == "outbound")
                        & (CommunicationMessage.delivery_status == "sent")
                    ),
                ),
            )
            .subquery()
        )
        latest_messages = {
            message.person_id: message
            for message in (
                await session.scalars(
                    select(CommunicationMessage)
                    .join(
                        ranked_messages,
                        CommunicationMessage.id == ranked_messages.c.message_id,
                    )
                    .where(ranked_messages.c.position == 1)
                )
            ).all()
        }
        unread_counts = dict(
            (
                await session.execute(
                    select(CommunicationMessage.person_id, func.count(CommunicationMessage.id))
                    .join(
                        CommunicationThread,
                        CommunicationThread.person_id == CommunicationMessage.person_id,
                    )
                    .where(
                        CommunicationMessage.person_id.in_(person_ids),
                        CommunicationMessage.direction == "inbound",
                        CommunicationMessage.message_type == "text",
                        or_(
                            CommunicationThread.admin_read_at.is_(None),
                            CommunicationMessage.created_at
                            > CommunicationThread.admin_read_at,
                        ),
                    )
                    .group_by(CommunicationMessage.person_id)
                )
            ).all()
        )
        remaining_person_ids = set(
            (
                await session.scalars(
                    select(CommunicationMessage.person_id)
                    .where(CommunicationMessage.person_id.in_(person_ids))
                    .distinct()
                )
            ).all()
        )
        threads = list(
            (
                await session.scalars(
                    select(CommunicationThread).where(
                        CommunicationThread.person_id.in_(person_ids)
                    )
                )
            ).all()
        )
        for thread in threads:
            latest = latest_messages.get(thread.person_id)
            if latest is None:
                if thread.person_id in remaining_person_ids:
                    thread.last_message_at = None
                    thread.last_message_preview = None
                    thread.admin_unread_count = 0
                else:
                    await session.delete(thread)
                continue
            thread.last_message_at = latest.created_at
            thread.last_message_preview = latest.text[:240]
            thread.admin_unread_count = int(unread_counts.get(thread.person_id, 0))
    return len(ids)


def _intent_status(answers: list[str]) -> str:
    values = set(answers)
    if not values:
        return "pending"
    if len(values) > 1:
        return "conflict"
    return "confirmed" if "yes" in values else "declined"


async def save_interaction_response(
    session: AsyncSession,
    *,
    request: InteractionRequest,
    respondent_person_id: int,
    respondent_context: str,
    answer: str,
    lesson_answers: dict[str, str] | None = None,
    reason: str | None = None,
) -> InteractionResponse:
    if request.status not in {"active", "answered"}:
        raise ValueError("Запрос больше не принимает ответы")
    expires_at = request.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and expires_at < utcnow():
        request.status = "expired"
        raise ValueError("Срок ответа истёк")
    if respondent_person_id != request.recipient_person_id:
        raise PermissionError("Этот запрос предназначен другому получателю")
    current = await session.scalar(
        select(InteractionResponse).where(
            InteractionResponse.request_id == request.id,
            InteractionResponse.respondent_person_id == respondent_person_id,
            InteractionResponse.respondent_context == respondent_context,
        )
    )
    lesson_map = lesson_answers or {}
    if current is None:
        current = InteractionResponse(
            request_id=request.id,
            respondent_person_id=respondent_person_id,
            respondent_context=respondent_context,
            answer=answer,
            lesson_answers=lesson_map,
            reason=reason,
        )
        session.add(current)
        await session.flush()
    else:
        session.add(
            InteractionResponseHistory(
                response_id=current.id,
                old_answer=current.answer,
                new_answer=answer,
                old_lesson_answers=current.lesson_answers or {},
                new_lesson_answers=lesson_map,
            )
        )
        current.answer = answer
        current.lesson_answers = lesson_map
        current.reason = reason or current.reason
        current.revision += 1
        current.updated_at = utcnow()
    links = list(
        (
            await session.scalars(
                select(InteractionRequestLesson).where(
                    InteractionRequestLesson.request_id == request.id
                )
            )
        ).all()
    )
    for link in links:
        per_lesson = lesson_map.get(str(link.lesson_id), answer)
        intent = await session.scalar(
            select(LessonAttendanceIntent).where(
                LessonAttendanceIntent.lesson_id == link.lesson_id,
                LessonAttendanceIntent.student_person_id == request.subject_person_id,
                LessonAttendanceIntent.lesson_revision == link.lesson_revision,
            )
        )
        if intent is None:
            intent = LessonAttendanceIntent(
                lesson_id=link.lesson_id,
                student_person_id=int(request.subject_person_id or request.recipient_person_id),
                lesson_revision=link.lesson_revision,
                last_request_id=request.id,
            )
            session.add(intent)
            await session.flush()
        related_requests = (
            select(InteractionRequest.id)
            .join(
                InteractionRequestLesson,
                InteractionRequestLesson.request_id == InteractionRequest.id,
            )
            .where(
                InteractionRequest.subject_person_id == intent.student_person_id,
                InteractionRequest.request_type == "lesson_confirmation",
                InteractionRequestLesson.lesson_id == link.lesson_id,
                InteractionRequestLesson.lesson_revision == link.lesson_revision,
            )
        )
        responses = list(
            (
                await session.scalars(
                    select(InteractionResponse).where(
                        InteractionResponse.request_id.in_(related_requests)
                    )
                )
            ).all()
        )
        answers: list[str] = []
        for response in responses:
            response_answer = (response.lesson_answers or {}).get(
                str(link.lesson_id), response.answer
            )
            if response_answer in {"yes", "no"}:
                answers.append(response_answer)
        # A new response has been flushed above. Keep this fallback for SQLite
        # sessions configured without autoflush.
        if current not in responses and per_lesson in {"yes", "no"}:
            answers.append(per_lesson)
        intent.status = _intent_status(answers)
        intent.responded_at = utcnow()
        intent.updated_at = utcnow()
        if intent.status == "conflict":
            from .learning_models import AdminNotification

            conflict_key = (
                f"confirmation-conflict:{intent.lesson_id}:"
                f"{intent.student_person_id}:{intent.lesson_revision}"
            )
            existing_alert = await session.scalar(
                select(AdminNotification.id).where(
                    AdminNotification.dedupe_key == conflict_key
                )
            )
            if existing_alert is None:
                session.add(
                    AdminNotification(
                        dedupe_key=conflict_key,
                        kind="confirmation_conflict",
                        title="Конфликт подтверждений",
                        message="Ответы ученика и родителя по занятию расходятся.",
                        lesson_id=intent.lesson_id,
                    )
                )
    request.status = "answered"
    request.updated_at = utcnow()
    await session.execute(
        update(NotificationJob)
        .where(
            NotificationJob.interaction_request_id == request.id,
            NotificationJob.status.in_(["pending", "retry"]),
            NotificationJob.dedupe_key.like("%:followup"),
        )
        .values(status="cancelled", updated_at=utcnow())
    )
    return current


class RulePayload(BaseModel):
    event_code: str
    recipient_context: str = "*"
    offset_minutes: int = -1
    enabled: bool = True
    requires_confirmation: bool = False
    priority: str = "normal"
    quiet_hours_policy: str = "defer"
    quiet_start: str | None = "22:00"
    quiet_end: str | None = "08:00"
    configuration: dict[str, Any] = Field(default_factory=dict)


class OverridePayload(BaseModel):
    recipient_context: str
    event_code: str
    offset_minutes: int = -1
    state: str
    student_person_id: int | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class MessagePayload(BaseModel):
    person_ids: list[int] = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)
    urgent: bool = False
    preview: bool = False


class PollPayload(MessagePayload):
    related_lesson_id: int | None = None
    expires_at: datetime | None = None


class ResponsePayload(BaseModel):
    answer: Literal["yes", "no", "partial"]
    lesson_answers: dict[str, Literal["yes", "no"]] = Field(default_factory=dict)
    reason: str | None = None


class SchedulePublicationPayload(BaseModel):
    date_from: date
    date_to: date
    preview: bool = False


def _lesson_snapshot(item: BundleLesson) -> dict[str, Any]:
    return {
        "lesson_id": item.id,
        "revision": item.revision,
        "start_at": item.start_at.astimezone(UTC).isoformat(),
        "end_at": item.end_at.astimezone(UTC).isoformat(),
        "subject": item.subject,
        "teacher": item.teacher,
        "room": item.room,
        "students": list(item.students),
    }


def _schedule_text(
    bundles: list[DailyBundle],
    *,
    changed: bool,
    removed: list[dict[str, Any]] | None = None,
    changed_items: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    timezone: ZoneInfo | None = None,
) -> str:
    heading = "Изменения в расписании" if changed else "Расписание опубликовано"
    if changed:
        zone = timezone or ZoneInfo("UTC")
        lines: list[str] = []
        labels = {
            "subject": "предмет",
            "teacher": "преподаватель",
            "room": "кабинет",
        }
        for old, new in changed_items or []:
            start = datetime.fromisoformat(str(new["start_at"])).astimezone(zone)
            lines.append(f"{start:%d.%m} · {new['subject']}")
            old_start = datetime.fromisoformat(str(old["start_at"])).astimezone(zone)
            old_end = datetime.fromisoformat(str(old["end_at"])).astimezone(zone)
            new_end = datetime.fromisoformat(str(new["end_at"])).astimezone(zone)
            if (old_start, old_end) != (start, new_end):
                lines.append(
                    f"время: {old_start:%d.%m %H:%M}–{old_end:%H:%M} → "
                    f"{start:%d.%m %H:%M}–{new_end:%H:%M}"
                )
            for field, label in labels.items():
                if old.get(field) != new.get(field):
                    lines.append(f"{label}: {old.get(field) or '—'} → {new.get(field) or '—'}")
            old_students = set(old.get("students") or [])
            new_students = set(new.get("students") or [])
            if added := sorted(new_students - old_students):
                lines.append("добавлены ученики: " + ", ".join(added))
            if removed_students := sorted(old_students - new_students):
                lines.append("исключены ученики: " + ", ".join(removed_students))
            lines.append("")
        for snapshot in removed or []:
            start = datetime.fromisoformat(str(snapshot["start_at"])).astimezone(zone)
            lines.extend([f"{start:%d.%m %H:%M} · {snapshot['subject']}", "занятие исключено", ""])
        return heading + "\n\n" + "\n".join(lines).strip()
    blocks = [render_bundle(bundle) for bundle in bundles]
    return heading + "\n\n" + "\n\n".join(blocks)


async def apply_delivered_schedule_snapshot(
    session: AsyncSession, job: NotificationJob
) -> None:
    """Advance last-notified state only after MAX accepted the message."""
    delivery = job.payload.get("schedule_delivery")
    if not isinstance(delivery, dict):
        return
    publication_id = delivery.get("publication_id")
    removed_ids = delivery.get("removed_lesson_ids") or []
    if removed_ids:
        await session.execute(
            delete(ScheduleRecipientSnapshot).where(
                ScheduleRecipientSnapshot.recipient_person_id == job.recipient_person_id,
                ScheduleRecipientSnapshot.recipient_context == job.recipient_context,
                ScheduleRecipientSnapshot.subject_person_id == job.subject_person_id,
                ScheduleRecipientSnapshot.lesson_id.in_(removed_ids),
            )
        )
    for value in delivery.get("snapshots") or []:
        lesson_id = int(value["lesson_id"])
        row = await session.scalar(
            select(ScheduleRecipientSnapshot).where(
                ScheduleRecipientSnapshot.recipient_person_id == job.recipient_person_id,
                ScheduleRecipientSnapshot.recipient_context == job.recipient_context,
                ScheduleRecipientSnapshot.subject_person_id == job.subject_person_id,
                ScheduleRecipientSnapshot.lesson_id == lesson_id,
            )
        )
        if row is None:
            row = ScheduleRecipientSnapshot(
                recipient_person_id=job.recipient_person_id,
                recipient_context=job.recipient_context,
                subject_person_id=int(job.subject_person_id or job.recipient_person_id),
                lesson_id=lesson_id,
                snapshot=value,
                lesson_revision=int(value["revision"]),
            )
            session.add(row)
        row.publication_id = int(publication_id) if publication_id is not None else None
        row.lesson_revision = int(value["revision"])
        row.snapshot = value
        row.delivered_at = utcnow()


def _rule_view(item: NotificationGlobalRule) -> dict[str, Any]:
    return {
        "id": item.id,
        "event_code": item.event_code,
        "recipient_context": item.recipient_context,
        "offset_minutes": item.offset_minutes,
        "enabled": item.enabled,
        "requires_confirmation": item.requires_confirmation,
        "priority": item.priority,
        "quiet_hours_policy": item.quiet_hours_policy,
        "quiet_start": item.quiet_start,
        "quiet_end": item.quiet_end,
        "configuration": item.configuration or {},
    }


async def _campaign_poll_counts(session: AsyncSession, campaign_id: int) -> dict[str, int]:
    total = (
        await session.scalar(
            select(func.count(InteractionRequest.id)).where(
                InteractionRequest.campaign_id == campaign_id
            )
        )
        or 0
    )
    answer_rows = (
        await session.execute(
            select(InteractionResponse.answer, func.count(InteractionResponse.id))
            .join(
                InteractionRequest,
                InteractionRequest.id == InteractionResponse.request_id,
            )
            .where(InteractionRequest.campaign_id == campaign_id)
            .group_by(InteractionResponse.answer)
        )
    ).all()
    counts = {answer: count for answer, count in answer_rows}
    answered = sum(counts.values())
    return {
        "recipients": int(total),
        "yes": int(counts.get("yes", 0)),
        "no": int(counts.get("no", 0)),
        "no_response": max(0, int(total) - int(answered)),
    }


@dataclass(frozen=True)
class PollTarget:
    recipient_person_id: int
    recipient_context: str
    subject_person_id: int | None


async def _poll_targets(
    session: AsyncSession, selected_people: list[Person]
) -> list[PollTarget]:
    """Expand a selected student into one student-family-teacher response card."""
    selected_student_ids = {
        person.id
        for person in selected_people
        if any(link.role == "student" for link in person.role_links)
    }
    targets: dict[tuple[int, str, int | None], PollTarget] = {}
    selected_person_ids = {person.id for person in selected_people}
    selected_parent_links: dict[int, set[int]] = defaultdict(set)
    if selected_student_ids and selected_person_ids:
        parent_links = (
            await session.execute(
                select(StudentGuardian.guardian_id, StudentGuardian.student_id).where(
                    StudentGuardian.guardian_id.in_(selected_person_ids),
                    StudentGuardian.student_id.in_(selected_student_ids),
                )
            )
        ).all()
        for guardian_id, student_id in parent_links:
            selected_parent_links[guardian_id].add(student_id)

    def add(recipient_id: int, context: str, subject_id: int | None) -> None:
        target = PollTarget(recipient_id, context, subject_id)
        targets[(recipient_id, context, subject_id)] = target

    for person in selected_people:
        roles = {link.role for link in person.role_links}
        if "student" in roles:
            add(person.id, "student", person.id)
        linked_selected_students = selected_parent_links.get(person.id, set())
        if "parent" in roles:
            if linked_selected_students:
                for student_id in linked_selected_students:
                    add(person.id, "guardian", student_id)
            elif "student" not in roles:
                add(person.id, "guardian", None)
        if "teacher" in roles and "student" not in roles:
            add(person.id, "teacher", None)
        if not roles:
            add(person.id, "student", person.id)

    if selected_student_ids:
        relations = list(
            (
                await session.scalars(
                    select(StudentGuardian).where(
                        StudentGuardian.student_id.in_(selected_student_ids)
                    )
                )
            ).all()
        )
        for relation in relations:
            add(relation.guardian_id, "guardian", relation.student_id)

        current_time = utcnow()
        teacher_rows = (
            await session.execute(
                select(GroupMembership.person_id, StudyGroup.default_teacher_id)
                .join(StudyGroup, StudyGroup.id == GroupMembership.group_id)
                .where(
                    GroupMembership.person_id.in_(selected_student_ids),
                    GroupMembership.start_at <= current_time,
                    or_(
                        GroupMembership.end_at.is_(None),
                        GroupMembership.end_at > current_time,
                    ),
                    StudyGroup.active.is_(True),
                    StudyGroup.default_teacher_id.is_not(None),
                )
            )
        ).all()
        for student_id, teacher_id in teacher_rows:
            if teacher_id is not None:
                add(teacher_id, "teacher", student_id)

    return list(targets.values())


async def _campaign_poll_details(
    session: AsyncSession, campaign_id: int
) -> dict[str, Any] | None:
    campaign = await session.get(CommunicationCampaign, campaign_id)
    if campaign is None or campaign.campaign_type != "custom_poll":
        return None
    requests = list(
        (
            await session.scalars(
                select(InteractionRequest)
                .where(InteractionRequest.campaign_id == campaign_id)
                .order_by(InteractionRequest.id)
            )
        ).all()
    )
    person_ids = {
        person_id
        for item in requests
        for person_id in (item.recipient_person_id, item.subject_person_id)
        if person_id is not None
    }
    people = {
        item.id: item
        for item in (
            await session.scalars(select(Person).where(Person.id.in_(person_ids)))
        ).all()
    }
    responses = {
        item.request_id: item
        for item in (
            await session.scalars(
                select(InteractionResponse).where(
                    InteractionResponse.request_id.in_([item.id for item in requests])
                )
            )
        ).all()
    }
    jobs = {
        item.interaction_request_id: item
        for item in (
            await session.scalars(
                select(NotificationJob).where(NotificationJob.campaign_id == campaign_id)
            )
        ).all()
        if item.interaction_request_id is not None
    }
    recipients = []
    answer_by_target: dict[tuple[int, str, int | None], str | None] = {}
    for request in requests:
        person = people.get(request.recipient_person_id)
        response = responses.get(request.id)
        job = jobs.get(request.id)
        answer = response.answer if response is not None else None
        subject_id = request.subject_person_id
        if request.recipient_context == "student" and subject_id is None:
            subject_id = request.recipient_person_id
        answer_by_target[
            (request.recipient_person_id, request.recipient_context, subject_id)
        ] = answer
        recipients.append(
            {
                "request_id": request.id,
                "person_id": request.recipient_person_id,
                "full_name": person.full_name if person is not None else "Неизвестный получатель",
                "roles": [link.role for link in (person.role_links if person else [])],
                "context": request.recipient_context,
                "subject_person_id": subject_id,
                "delivery_status": job.status if job is not None else "unavailable",
                "answer": answer,
                "answered_at": response.answered_at if response is not None else None,
            }
        )
    student_ids = {
        request.subject_person_id or request.recipient_person_id
        for request in requests
        if request.recipient_context == "student"
    }
    agreements = []
    for student_id in sorted(student_ids):
        student_answer = answer_by_target.get((student_id, "student", student_id))
        guardians = []
        teachers = []
        for request in requests:
            if request.subject_person_id != student_id:
                continue
            answer = answer_by_target.get(
                (
                    request.recipient_person_id,
                    request.recipient_context,
                    student_id,
                )
            )
            person = people.get(request.recipient_person_id)
            target = {
                "person_id": request.recipient_person_id,
                "name": person.full_name if person is not None else "Неизвестный получатель",
                "answer": answer,
            }
            if request.recipient_context == "guardian":
                guardians.append(target)
            elif request.recipient_context == "teacher":
                teachers.append(target)
        expected_answers = [student_answer]
        expected_answers.extend(item["answer"] for item in guardians)
        expected_answers.extend(item["answer"] for item in teachers)
        answered = {answer for answer in expected_answers if answer is not None}
        if len(answered) > 1:
            result = "conflict"
        elif any(answer is None for answer in expected_answers):
            result = "waiting"
        else:
            result = "agreed"
        agreements.append(
            {
                "student_id": student_id,
                "student_name": people[student_id].full_name,
                "student_answer": student_answer,
                "guardians": guardians,
                "teachers": teachers,
                "result": result,
            }
        )
    return {
        "id": campaign.id,
        "title": campaign.title,
        "created_at": campaign.created_at,
        "recipients": recipients,
        "agreements": agreements,
        "counts": await _campaign_poll_counts(session, campaign_id),
    }


def create_communications_router(
    sessions: async_sessionmaker[AsyncSession],
    require_management_token: Callable[..., Any],
    center_timezone: str,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/communications",
        tags=["communications"],
        dependencies=[Depends(require_management_token)],
    )
    timezone = ZoneInfo(center_timezone)

    @router.get("/settings/global")
    async def global_settings() -> list[dict[str, Any]]:
        async with sessions() as session:
            await ensure_default_rules(session)
            await session.commit()
            rules = list(
                (
                    await session.scalars(
                        select(NotificationGlobalRule).order_by(
                            NotificationGlobalRule.event_code,
                            NotificationGlobalRule.recipient_context,
                            NotificationGlobalRule.offset_minutes.desc(),
                        )
                    )
                ).all()
            )
            return [_rule_view(item) for item in rules]

    @router.put("/settings/global")
    async def save_global_settings(
        payload: list[RulePayload], admin_id: int = Depends(require_management_token)
    ) -> list[dict[str, Any]]:
        async with sessions() as session:
            for value in payload:
                if value.recipient_context not in RECIPIENT_CONTEXTS | {"*"}:
                    raise HTTPException(422, "Неизвестный контекст получателя")
                item = await session.scalar(
                    select(NotificationGlobalRule).where(
                        NotificationGlobalRule.event_code == value.event_code,
                        NotificationGlobalRule.recipient_context == value.recipient_context,
                        NotificationGlobalRule.offset_minutes == value.offset_minutes,
                    )
                )
                if item is None:
                    item = NotificationGlobalRule(**value.model_dump())
                    session.add(item)
                else:
                    for key, field_value in value.model_dump().items():
                        setattr(item, key, field_value)
                    item.updated_at = utcnow()
            from .learning_models import AuditEvent

            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.global_settings_changed",
                    entity_type="notification_settings",
                    details={"rules": len(payload)},
                )
            )
            await session.commit()
        return await global_settings()

    @router.get("/settings/person/{person_id}")
    async def person_settings(person_id: int) -> dict[str, Any]:
        async with sessions() as session:
            person = await session.get(Person, person_id)
            if person is None:
                raise HTTPException(404)
            overrides = list(
                (
                    await session.scalars(
                        select(PersonNotificationOverride).where(
                            PersonNotificationOverride.person_id == person_id
                        )
                    )
                ).all()
            )
            child_overrides = list(
                (
                    await session.scalars(
                        select(GuardianNotificationOverride).where(
                            GuardianNotificationOverride.guardian_person_id == person_id
                        )
                    )
                ).all()
            )
            return {
                "person_id": person_id,
                "overrides": [
                    {
                        "id": item.id,
                        "recipient_context": item.recipient_context,
                        "event_code": item.event_code,
                        "offset_minutes": item.offset_minutes,
                        "state": item.state,
                        "configuration": item.configuration or {},
                    }
                    for item in overrides
                ],
                "guardian_child_overrides": [
                    {
                        "id": item.id,
                        "student_person_id": item.student_person_id,
                        "event_code": item.event_code,
                        "offset_minutes": item.offset_minutes,
                        "state": item.state,
                        "configuration": item.configuration or {},
                    }
                    for item in child_overrides
                ],
            }

    @router.put("/settings/person/{person_id}")
    async def save_person_settings(
        person_id: int,
        payload: list[OverridePayload],
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        async with sessions() as session:
            if await session.get(Person, person_id) is None:
                raise HTTPException(404)
            await session.execute(
                delete(PersonNotificationOverride).where(
                    PersonNotificationOverride.person_id == person_id
                )
            )
            await session.execute(
                delete(GuardianNotificationOverride).where(
                    GuardianNotificationOverride.guardian_person_id == person_id
                )
            )
            for value in payload:
                if value.state not in {"inherit", "on", "off"}:
                    raise HTTPException(422, "Неизвестное состояние настройки")
                data = value.model_dump(exclude={"student_person_id"})
                if value.student_person_id is not None:
                    session.add(
                        GuardianNotificationOverride(
                            guardian_person_id=person_id,
                            student_person_id=value.student_person_id,
                            event_code=value.event_code,
                            offset_minutes=value.offset_minutes,
                            state=value.state,
                            configuration=value.configuration,
                        )
                    )
                else:
                    session.add(PersonNotificationOverride(person_id=person_id, **data))
            from .learning_models import AuditEvent

            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.person_settings_changed",
                    entity_type="person",
                    entity_id=person_id,
                    details={"overrides": len(payload)},
                )
            )
            await session.commit()
        return await person_settings(person_id)

    async def availability(
        session: AsyncSession, person_ids: list[int]
    ) -> tuple[list[int], list[int]]:
        available = set(
            (
                await session.scalars(
                    select(PersonMaxIdentity.person_id)
                    .join(Person, Person.id == PersonMaxIdentity.person_id)
                    .where(
                        PersonMaxIdentity.person_id.in_(person_ids),
                        PersonMaxIdentity.max_user_id.is_not(None),
                        Person.active.is_(True),
                        Person.bot_access_enabled.is_(True),
                        Person.archived_at.is_(None),
                    )
                )
            ).all()
        )
        return [item for item in person_ids if item in available], [
            item for item in person_ids if item not in available
        ]

    @router.post("/send-message")
    async def send_message(
        payload: MessagePayload, admin_id: int = Depends(require_management_token)
    ) -> dict[str, Any]:
        person_ids = list(dict.fromkeys(payload.person_ids))
        async with sessions() as session:
            existing = set(
                (await session.scalars(select(Person.id).where(Person.id.in_(person_ids)))).all()
            )
            if existing != set(person_ids):
                raise HTTPException(404, "Один из получателей не найден")
            available, unavailable = await availability(session, person_ids)
            preview = {
                "recipients": len(person_ids),
                "available": len(available),
                "unavailable": unavailable,
                "messages": len(available),
                "sample": payload.text,
            }
            if payload.preview:
                return preview
            campaign = CommunicationCampaign(
                created_by_admin_id=admin_id,
                campaign_type="manual_message",
                title=payload.text[:100],
                payload={"urgent": payload.urgent},
                status="sending",
            )
            session.add(campaign)
            await session.flush()
            for person_id in person_ids:
                await enqueue_job(
                    session,
                    dedupe_key=f"campaign:{campaign.id}:{person_id}",
                    event_type=NotificationEvent.CUSTOM_MESSAGE,
                    recipient_person_id=person_id,
                    recipient_context="student",
                    priority=2 if payload.urgent else 1,
                    scheduled_at=utcnow(),
                    campaign_id=campaign.id,
                    payload={"text": payload.text, "manual": True},
                )
            campaign.status = "scheduled"
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.manual_campaign_created",
                    entity_type="communication_campaign",
                    entity_id=campaign.id,
                    details={"recipients": len(person_ids), "urgent": payload.urgent},
                )
            )
            await session.commit()
            return {**preview, "campaign_id": campaign.id, "status": campaign.status}

    @router.post("/polls")
    async def create_poll(
        payload: PollPayload, admin_id: int = Depends(require_management_token)
    ) -> dict[str, Any]:
        person_ids = list(dict.fromkeys(payload.person_ids))
        async with sessions() as session:
            selected_people = list(
                (await session.scalars(select(Person).where(Person.id.in_(person_ids)))).all()
            )
            existing = {person.id for person in selected_people}
            if existing != set(person_ids):
                raise HTTPException(404, "Один из получателей не найден")
            targets = await _poll_targets(session, selected_people)
            target_person_ids = list(
                dict.fromkeys(target.recipient_person_id for target in targets)
            )
            available, unavailable = await availability(session, target_person_ids)
            available_people = set(available)
            available_targets = sum(
                target.recipient_person_id in available_people for target in targets
            )
            preview = {
                "recipients": len(targets),
                "available": available_targets,
                "unavailable": unavailable,
                "messages": available_targets,
                "sample": payload.text,
            }
            if payload.preview:
                return preview
            campaign = CommunicationCampaign(
                created_by_admin_id=admin_id,
                campaign_type="custom_poll",
                title=payload.text[:100],
                payload={},
                status="sending",
            )
            session.add(campaign)
            await session.flush()
            request_ids = []
            subject_ids = {
                target.subject_person_id
                for target in targets
                if target.subject_person_id is not None
            }
            subjects = {
                person.id: person.full_name
                for person in (
                    await session.scalars(select(Person).where(Person.id.in_(subject_ids)))
                ).all()
            }
            for target in targets:
                person_id = target.recipient_person_id
                recipient_context = target.recipient_context
                subject_person_id = target.subject_person_id
                request = InteractionRequest(
                    request_type="yes_no",
                    question=payload.text,
                    recipient_person_id=person_id,
                    recipient_context=recipient_context,
                    subject_person_id=subject_person_id,
                    related_lesson_id=payload.related_lesson_id,
                    campaign_id=campaign.id,
                    created_by_admin_id=admin_id,
                    expires_at=payload.expires_at,
                )
                session.add(request)
                await session.flush()
                request_ids.append(request.id)
                keyboard = [
                    [
                        {
                            "type": "callback",
                            "text": "Да",
                            "payload": f"interaction:{request.id}:yes",
                        },
                        {
                            "type": "callback",
                            "text": "Нет",
                            "payload": f"interaction:{request.id}:no",
                        },
                    ]
                ]
                await enqueue_job(
                    session,
                    dedupe_key=(
                        f"campaign:{campaign.id}:{person_id}:{recipient_context}:"
                        f"{subject_person_id or 0}"
                    ),
                    event_type=NotificationEvent.CUSTOM_YES_NO_REQUEST,
                    recipient_person_id=person_id,
                    recipient_context=recipient_context,
                    subject_person_id=subject_person_id,
                    priority=1,
                    scheduled_at=utcnow(),
                    campaign_id=campaign.id,
                    interaction_request_id=request.id,
                    lesson_id=payload.related_lesson_id,
                    payload={
                        "text": (
                            f"{payload.text}\nУченик: {subjects[subject_person_id]}"
                            if subject_person_id is not None
                            and subject_person_id != person_id
                            else payload.text
                        ),
                        "keyboard": keyboard,
                    },
                )
            campaign.status = "scheduled"
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.poll_campaign_created",
                    entity_type="communication_campaign",
                    entity_id=campaign.id,
                    details={"recipients": len(targets)},
                )
            )
            await session.commit()
            return {
                **preview,
                "campaign_id": campaign.id,
                "requests": request_ids,
            }

    @router.post("/schedule/publish")
    async def publish_schedule(
        payload: SchedulePublicationPayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        if payload.date_to < payload.date_from:
            raise HTTPException(422, "Дата окончания раньше даты начала")
        period_start = datetime.combine(payload.date_from, time.min, tzinfo=timezone)
        period_end = datetime.combine(
            payload.date_to + timedelta(days=1), time.min, tzinfo=timezone
        )
        async with sessions() as session:
            bundles = await daily_bundles(
                session,
                date_from=period_start,
                date_to=period_end,
                timezone=timezone,
            )
            grouped: dict[tuple[int, str, int], list[DailyBundle]] = defaultdict(list)
            current: dict[tuple[int, str, int, int], tuple[DailyBundle, BundleLesson]] = {}
            for bundle in bundles:
                group_key = (
                    bundle.recipient_person_id,
                    bundle.recipient_context,
                    bundle.subject_person_id,
                )
                grouped[group_key].append(bundle)
                for lesson in bundle.lessons:
                    current[(*group_key, lesson.id)] = (bundle, lesson)
            snapshot_rows = list(
                (await session.scalars(select(ScheduleRecipientSnapshot))).all()
            )
            previous_rows = [
                row
                for row in snapshot_rows
                if period_start.astimezone(UTC)
                <= datetime.fromisoformat(str(row.snapshot["start_at"]))
                < period_end.astimezone(UTC)
            ]
            previous = {
                (
                    row.recipient_person_id,
                    row.recipient_context,
                    row.subject_person_id,
                    row.lesson_id,
                ): row
                for row in previous_rows
            }
            affected = set()
            new_recipients = set()
            changed_by_recipient: dict[
                tuple[int, str, int], list[tuple[dict[str, Any], dict[str, Any]]]
            ] = defaultdict(list)
            removed_by_recipient: dict[tuple[int, str, int], list[dict[str, Any]]] = defaultdict(
                list
            )
            for key, (_bundle, lesson) in current.items():
                old = previous.get(key)
                if old is None:
                    affected.add(key[:3])
                    if not any(previous_key[:3] == key[:3] for previous_key in previous):
                        new_recipients.add(key[:3])
                elif old.snapshot != _lesson_snapshot(lesson):
                    affected.add(key[:3])
                    changed_by_recipient[key[:3]].append(
                        (old.snapshot, _lesson_snapshot(lesson))
                    )
            for key, old in previous.items():
                if key not in current:
                    affected.add(key[:3])
                    removed_by_recipient[key[:3]].append(old.snapshot)
            preview = {
                "period_from": payload.date_from.isoformat(),
                "period_to": payload.date_to.isoformat(),
                "recipients": len(grouped),
                "affected_recipients": len(affected),
                "unchanged_recipients": max(0, len(grouped) - len(affected)),
                "lessons": len({key[3] for key in current}),
            }
            affected_people = list({key[0] for key in affected})
            available, unavailable = await availability(session, affected_people)
            preview.update(
                {
                    "available": len(available),
                    "unavailable": unavailable,
                    "messages": len(affected),
                }
            )
            if payload.preview:
                return preview
            campaign = CommunicationCampaign(
                created_by_admin_id=admin_id,
                campaign_type="schedule_change" if previous else "schedule_publication",
                title=f"Расписание {payload.date_from:%d.%m}–{payload.date_to:%d.%m.%Y}",
                payload=preview,
                status="sending",
            )
            session.add(campaign)
            await session.flush()
            publication = SchedulePublication(
                period_from=payload.date_from,
                period_to=payload.date_to,
                created_by_admin_id=admin_id,
                campaign_id=campaign.id,
            )
            session.add(publication)
            await session.flush()
            resolver = NotificationPolicyResolver(session)
            sent = 0
            for recipient_key in affected:
                person_id, context, subject_person_id = recipient_key
                event = (
                    NotificationEvent.SCHEDULE_PUBLISHED
                    if recipient_key in new_recipients
                    else NotificationEvent.SCHEDULE_CHANGED
                )
                policy = await resolver.resolve(
                    recipient_person_id=person_id,
                    recipient_context=context,
                    subject_person_id=subject_person_id,
                    event_code=event,
                )
                if not policy.enabled:
                    continue
                recipient_bundles = grouped.get(recipient_key, [])
                text_value = _schedule_text(
                    recipient_bundles,
                    changed=event == NotificationEvent.SCHEDULE_CHANGED,
                    removed=removed_by_recipient.get(recipient_key),
                    changed_items=changed_by_recipient.get(recipient_key),
                    timezone=timezone,
                )
                snapshot_values = [
                    _lesson_snapshot(lesson)
                    for key, (_bundle, lesson) in current.items()
                    if key[:3] == recipient_key
                ]
                job = await enqueue_job(
                    session,
                    dedupe_key=f"schedule:{publication.id}:{person_id}:{context}:{subject_person_id}",
                    event_type=event,
                    recipient_person_id=person_id,
                    recipient_context=context,
                    subject_person_id=subject_person_id,
                    priority=PRIORITY_VALUE[policy.priority],
                    scheduled_at=utcnow(),
                    campaign_id=campaign.id,
                    payload={
                        "text": text_value,
                        "schedule_delivery": {
                            "publication_id": publication.id,
                            "snapshots": snapshot_values,
                            "removed_lesson_ids": [
                                int(value["lesson_id"])
                                for value in removed_by_recipient.get(recipient_key, [])
                            ],
                        },
                    },
                )
                sent += int(job is not None)
            campaign.status = "scheduled" if sent else "completed"
            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action=(
                        "communications.schedule_change_sent"
                        if previous
                        else "communications.schedule_published"
                    ),
                    entity_type="schedule_publication",
                    entity_id=publication.id,
                    details={
                        "period_from": payload.date_from.isoformat(),
                        "period_to": payload.date_to.isoformat(),
                        "recipients": len(affected),
                    },
                )
            )
            await session.commit()
            return {
                **preview,
                "publication_id": publication.id,
                "campaign_id": campaign.id,
                "queued": sent,
            }

    @router.get("/campaigns")
    async def campaigns() -> list[dict[str, Any]]:
        async with sessions() as session:
            items = list(
                (
                    await session.scalars(
                        select(CommunicationCampaign).order_by(
                            CommunicationCampaign.created_at.desc()
                        )
                    )
                ).all()
            )
            result = []
            for item in items:
                counts = dict(
                    (
                        await session.execute(
                            select(NotificationJob.status, func.count(NotificationJob.id))
                            .where(NotificationJob.campaign_id == item.id)
                            .group_by(NotificationJob.status)
                        )
                    ).all()
                )
                result.append(
                    {
                        "id": item.id,
                        "type": item.campaign_type,
                        "title": item.title,
                        "status": item.status,
                        "created_at": item.created_at,
                        "counts": counts,
                        "poll": (
                            await _campaign_poll_counts(session, item.id)
                            if item.campaign_type == "custom_poll"
                            else None
                        ),
                    }
                )
            return result

    @router.get("/campaigns/{campaign_id}/poll")
    async def campaign_poll(campaign_id: int) -> dict[str, Any]:
        async with sessions() as session:
            details = await _campaign_poll_details(session, campaign_id)
            if details is None:
                raise HTTPException(404, "Опрос не найден")
            return details

    @router.post("/campaigns/{campaign_id}/retry-failed")
    async def retry_failed_campaign(
        campaign_id: int, admin_id: int = Depends(require_management_token)
    ) -> dict[str, Any]:
        async with sessions() as session:
            campaign = await session.get(CommunicationCampaign, campaign_id)
            if campaign is None:
                raise HTTPException(404, "Рассылка не найдена")
            jobs = list(
                (
                    await session.scalars(
                        select(NotificationJob).where(
                            NotificationJob.campaign_id == campaign_id,
                            NotificationJob.status.in_(["failed", "cancelled"]),
                        )
                    )
                ).all()
            )
            for job in jobs:
                job.status = "pending"
                job.attempts = 0
                job.last_error = None
                job.scheduled_at = utcnow()
                job.updated_at = utcnow()
            campaign.status = "scheduled" if jobs else campaign.status
            campaign.updated_at = utcnow()
            from .learning_models import AuditEvent

            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.campaign_retry_failed",
                    entity_type="communication_campaign",
                    entity_id=campaign_id,
                    details={"jobs": len(jobs)},
                )
            )
            await session.commit()
            return {"campaign_id": campaign_id, "retried": len(jobs)}

    @router.get("/conversations")
    async def conversations(search: str = "") -> list[dict[str, Any]]:
        async with sessions() as session:
            statement = (
                select(CommunicationThread, Person)
                .join(Person, Person.id == CommunicationThread.person_id)
                .order_by(CommunicationThread.last_message_at.desc())
            )
            if search.strip():
                statement = statement.where(
                    or_(
                        Person.full_name.ilike(f"%{search.strip()}%"),
                        Person.phone.ilike(f"%{search.strip()}%"),
                    )
                )
            rows = (await session.execute(statement)).all()
            return [
                {
                    "person_id": person.id,
                    "full_name": person.full_name,
                    "phone": person.phone,
                    "last_message_at": thread.last_message_at,
                    "last_message_preview": thread.last_message_preview,
                    "admin_unread_count": thread.admin_unread_count,
                }
                for thread, person in rows
            ]

    @router.get("/conversations/{person_id}/messages")
    async def conversation_messages(
        person_id: int, before_id: int | None = None, limit: int = Query(50, ge=1, le=100)
    ) -> list[dict[str, Any]]:
        async with sessions() as session:
            statement = (
                select(CommunicationMessage)
                .where(CommunicationMessage.person_id == person_id)
                .order_by(CommunicationMessage.id.desc())
                .limit(limit)
            )
            if before_id is not None:
                statement = statement.where(CommunicationMessage.id < before_id)
            items = list((await session.scalars(statement)).all())
            return [
                {
                    "id": item.id,
                    "direction": item.direction,
                    "message_type": item.message_type,
                    "text": item.text,
                    "delivery_status": item.delivery_status,
                    "created_at": item.created_at,
                    "sent_at": item.sent_at,
                }
                for item in reversed(items)
            ]

    @router.post("/conversations/{person_id}/messages")
    async def conversation_send(
        person_id: int,
        payload: MessagePayload,
        admin_id: int = Depends(require_management_token),
    ) -> dict[str, Any]:
        payload.person_ids = [person_id]
        payload.preview = False
        return await send_message(payload, admin_id)

    @router.post("/conversations/{person_id}/read")
    async def conversation_read(person_id: int) -> dict[str, Any]:
        async with sessions() as session:
            thread = await session.get(CommunicationThread, person_id)
            if thread is not None:
                thread.admin_unread_count = 0
                thread.admin_read_at = utcnow()
                await session.commit()
            return {"person_id": person_id, "admin_unread_count": 0}

    @router.get("/confirmations")
    async def confirmations(date_from: date, date_to: date) -> list[dict[str, Any]]:
        start = datetime.combine(date_from, time.min, tzinfo=timezone).astimezone(UTC)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone).astimezone(
            UTC
        )
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(LessonAttendanceIntent, Lesson, Person)
                    .join(Lesson, Lesson.id == LessonAttendanceIntent.lesson_id)
                    .join(Person, Person.id == LessonAttendanceIntent.student_person_id)
                    .where(Lesson.start_at >= start, Lesson.start_at < end)
                    .order_by(Lesson.start_at, Person.full_name)
                )
            ).all()
            result = []
            for intent, lesson, person in rows:
                response_rows = (
                    await session.execute(
                        select(InteractionResponse, InteractionRequest)
                        .join(
                            InteractionRequest,
                            InteractionRequest.id == InteractionResponse.request_id,
                        )
                        .join(
                            InteractionRequestLesson,
                            InteractionRequestLesson.request_id == InteractionRequest.id,
                        )
                        .where(
                            InteractionRequest.subject_person_id == person.id,
                            InteractionRequestLesson.lesson_id == lesson.id,
                            InteractionRequestLesson.lesson_revision == intent.lesson_revision,
                        )
                        .order_by(InteractionResponse.updated_at.desc())
                    )
                ).all()
                answers: dict[str, list[str]] = defaultdict(list)
                reasons: list[str] = []
                for response, request in response_rows:
                    answer = (response.lesson_answers or {}).get(
                        str(lesson.id), response.answer
                    )
                    answers[request.recipient_context].append(answer)
                    if response.reason:
                        reasons.append(response.reason)
                sent = bool(
                    await session.scalar(
                        select(func.count(NotificationJob.id))
                        .join(
                            InteractionRequest,
                            InteractionRequest.id == NotificationJob.interaction_request_id,
                        )
                        .join(
                            InteractionRequestLesson,
                            InteractionRequestLesson.request_id == InteractionRequest.id,
                        )
                        .where(
                            InteractionRequest.subject_person_id == person.id,
                            InteractionRequestLesson.lesson_id == lesson.id,
                            InteractionRequestLesson.lesson_revision == intent.lesson_revision,
                            NotificationJob.status == "sent",
                        )
                    )
                )
                max_available = bool(
                    await session.scalar(
                        select(PersonMaxIdentity.max_user_id).where(
                            PersonMaxIdentity.person_id == person.id
                        )
                    )
                )
                result.append(
                    {
                    "id": intent.id,
                    "student_person_id": person.id,
                    "student_name": person.full_name,
                    "lesson_id": lesson.id,
                    "lesson": (
                        f"{lesson.start_at.astimezone(timezone):%d.%m · %H:%M} · "
                        f"{lesson.subject_name_snapshot}"
                    ),
                    "lesson_revision": intent.lesson_revision,
                    "status": intent.status,
                    "responded_at": intent.responded_at,
                    "request_sent": sent,
                    "student_answer": (answers.get("student") or ["—"])[0],
                    "guardian_answer": ", ".join(answers.get("guardian") or []) or "—",
                    "reason": "; ".join(dict.fromkeys(reasons)) or "—",
                    "max_available": max_available,
                    "needs_attention": (
                        intent.status in {"pending", "no_response", "conflict", "declined"}
                        or not max_available
                    ),
                }
                )
            return result

    @router.post("/interactions/{request_id}/respond")
    async def respond(request_id: int, payload: ResponsePayload) -> dict[str, Any]:
        async with sessions() as session:
            request = await session.get(InteractionRequest, request_id)
            if request is None:
                raise HTTPException(404)
            try:
                response = await save_interaction_response(
                    session,
                    request=request,
                    respondent_person_id=request.recipient_person_id,
                    respondent_context=request.recipient_context,
                    answer=payload.answer,
                    lesson_answers=payload.lesson_answers,
                    reason=payload.reason,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            await session.commit()
            return {"id": response.id, "answer": response.answer, "revision": response.revision}

    @router.post("/reconcile")
    async def reconcile() -> dict[str, int]:
        async with sessions() as session:
            expired = await expire_confirmation_requests(session, now=utcnow())
            reminders = await reconcile_daily_reminders(session, now=utcnow(), timezone=timezone)
            confirmations_created = await reconcile_confirmation_requests(
                session, now=utcnow(), timezone=timezone
            )
            await session.commit()
            return {
                "reminders_created": reminders,
                "confirmations_created": confirmations_created,
                "confirmations_expired": expired,
            }

    @router.post("/cleanup")
    async def cleanup(admin_id: int = Depends(require_management_token)) -> dict[str, int]:
        now = utcnow()
        async with sessions() as session:
            deleted = await cleanup_communication_history(session, now=now)
            from .learning_models import AuditEvent

            session.add(
                AuditEvent(
                    actor_admin_id=admin_id,
                    action="communications.history_cleanup",
                    entity_type="communication_message",
                    details={
                        "deleted": deleted,
                        "cutoff": (now - timedelta(days=30)).isoformat(),
                    },
                )
            )
            await session.commit()
            return {"deleted": deleted}

    return router


async def run_communications_maintenance(
    sessions: async_sessionmaker[AsyncSession],
    *,
    center_timezone: str,
    interval_seconds: float = 60.0,
) -> None:
    timezone = ZoneInfo(center_timezone)
    while True:
        try:
            async with sessions() as session:
                await expire_confirmation_requests(session, now=utcnow())
                await reconcile_daily_reminders(session, now=utcnow(), timezone=timezone)
                await reconcile_confirmation_requests(session, now=utcnow(), timezone=timezone)
                await cleanup_communication_history(session, now=utcnow())
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The outbox worker remains independent; the next cycle retries
            # reconciliation without losing deduplication state.
            import structlog

            structlog.get_logger().exception("communications_maintenance_failed")
        await asyncio.sleep(interval_seconds)
