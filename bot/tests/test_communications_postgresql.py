from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from krit_bot.communications import reconcile_daily_reminders
from krit_bot.db import Person, build_engine, build_session_factory, utcnow
from krit_bot.learning_models import Lesson, LessonParticipant, NotificationJob, Room, Subject

POSTGRES_URL = os.getenv("KRIT_TEST_POSTGRES_URL")


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="KRIT_TEST_POSTGRES_URL is required for the PostgreSQL outbox test",
)
@pytest.mark.asyncio
async def test_parallel_daily_reconciliation_keeps_one_job_per_dedupe_key() -> None:
    engine = build_engine(str(POSTGRES_URL))
    sessions = build_session_factory(engine)
    suffix = uuid4().hex[:10]
    async with sessions() as session:
        student = Person(full_name=f"Ученик {suffix}", phone=f"+79{suffix[:9]}")
        teacher = Person(full_name=f"Учитель {suffix}", phone=f"+78{suffix[:9]}")
        subject = Subject(name=f"Предмет {suffix}", color="#2563eb")
        room = Room(name=f"Кабинет {suffix}", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        timezone = ZoneInfo("Asia/Yekaterinburg")
        local_day = utcnow().astimezone(timezone).date() + timedelta(days=2)
        start = datetime.combine(local_day, time(14, 0), tzinfo=timezone).astimezone(UTC)
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=start,
            end_at=start + timedelta(hours=1),
            teacher_name_snapshot=teacher.full_name,
            room_name_snapshot=room.name,
            subject_name_snapshot=subject.name,
        )
        session.add(lesson)
        await session.flush()
        session.add(
            LessonParticipant(
                lesson_id=lesson.id,
                person_id=student.id,
                person_name_snapshot=student.full_name,
            )
        )
        await session.commit()
        lesson_id = lesson.id

    async def reconcile() -> None:
        async with sessions() as session:
            await reconcile_daily_reminders(
                session,
                now=utcnow(),
                timezone=ZoneInfo("Asia/Yekaterinburg"),
            )
            await session.commit()

    await asyncio.gather(reconcile(), reconcile())
    async with sessions() as session:
        jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(
                        NotificationJob.lesson_id == lesson_id,
                        NotificationJob.event_type == "lesson_reminder",
                    )
                )
            ).all()
        )
        assert len(jobs) == 6
        assert len({job.dedupe_key for job in jobs}) == len(jobs)
    await engine.dispose()
