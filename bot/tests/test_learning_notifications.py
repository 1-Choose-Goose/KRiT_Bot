from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from krit_bot.db import (
    Person,
    PersonRole,
    StudentGuardian,
    build_engine,
    build_session_factory,
    ensure_schema,
    utcnow,
)
from krit_bot.learning import _queue_guardian_event
from krit_bot.learning_models import NotificationJob
from krit_bot.learning_notifications import LearningNotificationWorker


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_text(self, *, user_id: int, text: str, **_: Any) -> dict[str, Any]:
        self.sent.append((user_id, text))
        return {}


async def test_notification_recovery_and_deduplicated_delivery(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Получатель Уведомления",
            phone="+79000000100",
            max_user_id=100,
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            NotificationJob(
                dedupe_key="test:once",
                event_type="test",
                recipient_person_id=person.id,
                scheduled_at=utcnow() - timedelta(minutes=1),
                status="processing",
                payload={"text": "Проверка"},
            )
        )
        await session.commit()
    api = FakeMax()
    worker = LearningNotificationWorker(sessions=sessions, api=api)  # type: ignore[arg-type]
    await worker.recover_interrupted()
    assert await worker.process_one() is True
    assert await worker.process_one() is False
    assert api.sent == [(100, "Проверка")]
    async with sessions() as session:
        job = await session.scalar(
            select(NotificationJob).where(NotificationJob.dedupe_key == "test:once")
        )
        assert job is not None
        assert job.status == "sent"
        assert job.attempts == 1
    await engine.dispose()


async def test_guardian_event_never_notifies_the_subject_itself(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'guardian.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        guardian = Person(
            full_name="Родитель и ученик",
            phone="+79000000101",
            max_user_id=101,
            role_links=[PersonRole(role="student"), PersonRole(role="parent")],
        )
        child = Person(
            full_name="Ребёнок Ученика",
            phone="+79000000102",
            role_links=[PersonRole(role="student")],
        )
        session.add_all([guardian, child])
        await session.flush()
        session.add(StudentGuardian(student_id=child.id, guardian_id=guardian.id))
        occurred_at = utcnow()
        await _queue_guardian_event(
            session, person=guardian, event_type="arrival", occurred_at=occurred_at
        )
        await _queue_guardian_event(
            session, person=child, event_type="arrival", occurred_at=occurred_at
        )
        await session.commit()
        jobs = list((await session.scalars(select(NotificationJob))).all())
        assert len(jobs) == 1
        assert jobs[0].recipient_person_id == guardian.id
    await engine.dispose()
