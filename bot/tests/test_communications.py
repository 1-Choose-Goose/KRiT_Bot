from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from krit_bot.communication_models import (
    GuardianNotificationOverride,
    InteractionRequest,
    InteractionRequestLesson,
    LessonAttendanceIntent,
    PersonNotificationOverride,
)
from krit_bot.communications import (
    NotificationPolicyResolver,
    daily_bundles,
    ensure_default_rules,
    reconcile_confirmation_requests,
    reconcile_daily_reminders,
    save_interaction_response,
)
from krit_bot.db import (
    Person,
    StudentGuardian,
    build_engine,
    build_session_factory,
    ensure_schema,
    utcnow,
)
from krit_bot.learning_models import Lesson, LessonParticipant, NotificationJob, Room, Subject


async def _database(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'communications.db'}")
    await ensure_schema(engine)
    return engine, build_session_factory(engine)


async def test_policy_hierarchy_uses_person_and_guardian_child_overrides(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        await ensure_default_rules(session)
        student = Person(full_name="Ученик", phone="+79000000001")
        guardian = Person(full_name="Родитель", phone="+79000000002")
        session.add_all([student, guardian])
        await session.flush()
        session.add_all(
            [
                PersonNotificationOverride(
                    person_id=guardian.id,
                    recipient_context="guardian",
                    event_code="lesson_reminder",
                    offset_minutes=60,
                    state="off",
                ),
                GuardianNotificationOverride(
                    guardian_person_id=guardian.id,
                    student_person_id=student.id,
                    event_code="lesson_reminder",
                    offset_minutes=60,
                    state="on",
                ),
            ]
        )
        await session.flush()
        policy = await NotificationPolicyResolver(session).resolve(
            recipient_person_id=guardian.id,
            recipient_context="guardian",
            subject_person_id=student.id,
            event_code="lesson_reminder",
            offset_minutes=60,
        )
        assert policy.enabled is True
        assert policy.source == "guardian_child"
    await engine.dispose()


async def test_daily_lessons_are_bundled_once_per_person_and_day(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    timezone = ZoneInfo("Asia/Yekaterinburg")
    async with sessions() as session:
        student = Person(full_name="Ученик Один", phone="+79000000011")
        guardian = Person(full_name="Родитель Один", phone="+79000000012")
        teacher = Person(full_name="Учитель Один", phone="+79000000013")
        session.add_all([student, guardian, teacher])
        await session.flush()
        session.add(StudentGuardian(student_id=student.id, guardian_id=guardian.id))
        subject = Subject(name="Информатика", color="#2563eb")
        room = Room(name="Кабинет 1", capacity=10)
        session.add_all([subject, room])
        await session.flush()
        local_start = datetime(2026, 10, 5, 10, 0, tzinfo=timezone)
        for index in range(2):
            start = local_start + timedelta(hours=index * 2)
            lesson = Lesson(
                subject_id=subject.id,
                teacher_id=teacher.id,
                room_id=room.id,
                start_at=start.astimezone(UTC),
                end_at=(start + timedelta(hours=1)).astimezone(UTC),
                status="planned",
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
        await session.flush()
        bundles = await daily_bundles(
            session,
            date_from=local_start - timedelta(days=1),
            date_to=local_start + timedelta(days=1),
            timezone=timezone,
        )
        assert len(bundles) == 3
        assert {bundle.recipient_context for bundle in bundles} == {
            "student",
            "guardian",
            "teacher",
        }
        assert all(len(bundle.lessons) == 2 for bundle in bundles)
        await reconcile_daily_reminders(session, now=utcnow(), timezone=timezone)
        await reconcile_confirmation_requests(session, now=utcnow(), timezone=timezone)
        jobs = list((await session.scalars(select(NotificationJob))).all())
        assert len([job for job in jobs if job.event_type == "lesson_reminder"]) == 9
        assert len([job for job in jobs if job.event_type == "lesson_confirmation_request"]) == 2
        confirmation = next(
            job for job in jobs if job.event_type == "lesson_confirmation_request"
        )
        assert any(
            button.get("payload", "").endswith(":partial")
            for row in confirmation.payload["keyboard"]
            for button in row
        )
    await engine.dispose()


async def test_partial_confirmation_is_saved_for_each_lesson(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000031")
        teacher = Person(full_name="Учитель", phone="+79000000032")
        subject = Subject(name="Математика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        lessons = []
        for index in range(2):
            start = utcnow() + timedelta(days=2, hours=index * 2)
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
            lessons.append(lesson)
        request = InteractionRequest(
            request_type="lesson_confirmation",
            question="На каких занятиях будете?",
            recipient_person_id=student.id,
            recipient_context="student",
            subject_person_id=student.id,
            related_lesson_id=lessons[0].id,
        )
        session.add(request)
        await session.flush()
        session.add_all(
            [
                InteractionRequestLesson(
                    request_id=request.id,
                    lesson_id=lesson.id,
                    lesson_revision=lesson.notification_revision,
                )
                for lesson in lessons
            ]
        )
        await save_interaction_response(
            session,
            request=request,
            respondent_person_id=student.id,
            respondent_context="student",
            answer="partial",
            lesson_answers={str(lessons[0].id): "yes", str(lessons[1].id): "no"},
        )
        intents = list(
            (
                await session.scalars(
                    select(LessonAttendanceIntent).order_by(LessonAttendanceIntent.lesson_id)
                )
            ).all()
        )
        assert [item.status for item in intents] == ["confirmed", "declined"]
    await engine.dispose()


async def test_student_and_guardian_disagreement_is_a_conflict(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000021")
        guardian = Person(full_name="Родитель", phone="+79000000022")
        teacher = Person(full_name="Учитель", phone="+79000000023")
        session.add_all([student, guardian, teacher])
        await session.flush()
        subject = Subject(name="Русский язык", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([subject, room])
        await session.flush()
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=utcnow() + timedelta(days=2),
            end_at=utcnow() + timedelta(days=2, hours=1),
            teacher_name_snapshot=teacher.full_name,
            room_name_snapshot=room.name,
            subject_name_snapshot=subject.name,
        )
        session.add(lesson)
        await session.flush()
        requests = []
        for recipient, context in ((student, "student"), (guardian, "guardian")):
            request = InteractionRequest(
                request_type="lesson_confirmation",
                question="Будете на занятии?",
                recipient_person_id=recipient.id,
                recipient_context=context,
                subject_person_id=student.id,
                related_lesson_id=lesson.id,
            )
            session.add(request)
            await session.flush()
            session.add(
                InteractionRequestLesson(
                    request_id=request.id,
                    lesson_id=lesson.id,
                    lesson_revision=lesson.notification_revision,
                )
            )
            requests.append(request)
        await save_interaction_response(
            session,
            request=requests[0],
            respondent_person_id=student.id,
            respondent_context="student",
            answer="yes",
        )
        await save_interaction_response(
            session,
            request=requests[1],
            respondent_person_id=guardian.id,
            respondent_context="guardian",
            answer="no",
        )
        intent = await session.scalar(select(LessonAttendanceIntent))
        assert intent is not None
        assert intent.status == "conflict"
    await engine.dispose()
