from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from krit_bot.communication_models import CommunicationMessage, CommunicationThread
from krit_bot.db import (
    Person,
    PersonRole,
    StudentGuardian,
    build_engine,
    build_session_factory,
    ensure_schema,
    utcnow,
)
from krit_bot.learning import _queue_guardian_event, _queue_lesson_state_notifications
from krit_bot.learning_models import (
    Lesson,
    LessonParticipant,
    NotificationJob,
    PersonMaxIdentity,
    Room,
    Subject,
)
from krit_bot.learning_notifications import LearningNotificationWorker


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_text(self, *, user_id: int, text: str, **_: Any) -> dict[str, Any]:
        self.sent.append((user_id, text))
        return {}


async def test_unavailable_recipient_is_not_added_to_dialog_history(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'unavailable.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Получатель без MAX",
            phone="+79000000109",
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            NotificationJob(
                dedupe_key="test:unavailable",
                event_type="test",
                recipient_person_id=person.id,
                scheduled_at=utcnow() - timedelta(minutes=1),
                payload={"text": "Не должно попасть в диалог"},
            )
        )
        await session.commit()
        person_id = person.id

    worker = LearningNotificationWorker(sessions=sessions, api=FakeMax())  # type: ignore[arg-type]
    assert await worker.process_one() is True
    async with sessions() as session:
        assert list((await session.scalars(select(CommunicationMessage))).all()) == []
        assert await session.get(CommunicationThread, person_id) is None
        job = await session.scalar(
            select(NotificationJob).where(NotificationJob.dedupe_key == "test:unavailable")
        )
        assert job is not None
        assert job.status == "cancelled"
    await engine.dispose()


async def test_notification_recovery_and_deduplicated_delivery(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Получатель Уведомления",
            phone="+79000000100",
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add_all(
            [
                PersonMaxIdentity(
                    person_id=person.id,
                    verified_phone=person.phone,
                    max_user_id=100,
                ),
                NotificationJob(
                    dedupe_key="test:once",
                    event_type="test",
                    recipient_person_id=person.id,
                    scheduled_at=utcnow() - timedelta(minutes=1),
                    status="processing",
                    payload={"text": "Проверка"},
                ),
            ]
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


async def test_start_and_finish_notifications_are_role_specific_without_self_copy(
    tmp_path,
) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'lesson-events.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        guardian = Person(
            full_name="Сергеев Сергей Петрович",
            phone="+79000000201",
            active=True,
            role_links=[PersonRole(role="student"), PersonRole(role="parent")],
        )
        child = Person(
            full_name="Сергеев Артём Сергеевич",
            phone="+79000000202",
            active=True,
            role_links=[PersonRole(role="student")],
        )
        teacher = Person(
            full_name="Иванова Мария Сергеевна",
            phone="+79000000203",
            active=True,
            role_links=[PersonRole(role="teacher")],
        )
        subject = Subject(name="Математика", color="#2563eb")
        room = Room(name="Кабинет 2", capacity=10)
        session.add_all([guardian, child, teacher, subject, room])
        await session.flush()
        session.add(StudentGuardian(student_id=child.id, guardian_id=guardian.id))
        now = utcnow()
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=now,
            end_at=now + timedelta(hours=1),
            actual_start_at=now,
            actual_end_at=now + timedelta(minutes=57),
            status="completed",
            teacher_name_snapshot=teacher.full_name,
            room_name_snapshot=room.name,
            subject_name_snapshot=subject.name,
        )
        session.add(lesson)
        await session.flush()
        participants = [
            LessonParticipant(
                lesson_id=lesson.id,
                person_id=person.id,
                person_name_snapshot=person.full_name,
                attendance_status="present",
            )
            for person in (guardian, child)
        ]
        session.add_all(participants)
        await session.flush()
        await _queue_lesson_state_notifications(
            session,
            lesson,
            participants,
            event="started",
            center_timezone="Asia/Yekaterinburg",
        )
        await _queue_lesson_state_notifications(
            session,
            lesson,
            participants,
            event="finished",
            center_timezone="Asia/Yekaterinburg",
        )
        await _queue_lesson_state_notifications(
            session,
            lesson,
            [participants[1]],
            event="participant_started",
            center_timezone="Asia/Yekaterinburg",
        )
        await _queue_lesson_state_notifications(
            session,
            lesson,
            [participants[1]],
            event="participant_started",
            center_timezone="Asia/Yekaterinburg",
        )
        await session.commit()
        jobs = list(
            (
                await session.scalars(select(NotificationJob).order_by(NotificationJob.dedupe_key))
            ).all()
        )
        assert len(jobs) == 8
        assert not any(
            f"guardian:{guardian.id}:student:{guardian.id}" in job.dedupe_key for job in jobs
        )
        assert any(
            job.recipient_person_id == guardian.id
            and job.payload.get("subject_person_id") == child.id
            and "Артём" in job.payload["text"]
            for job in jobs
        )
        assert any("Занятие началось" in job.payload["text"] for job in jobs)
        assert sum(job.event_type == "lesson_participant_started" for job in jobs) == 2
        assert any(
            job.event_type == "lesson_participant_started"
            and job.recipient_person_id == guardian.id
            and job.payload.get("subject_person_id") == child.id
            for job in jobs
        )
        assert any("Фактически:" in job.payload["text"] for job in jobs)
    await engine.dispose()
