from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, select

from krit_bot.communication_models import (
    CommunicationCampaign,
    CommunicationMessage,
    CommunicationThread,
    GuardianNotificationOverride,
    InteractionRequest,
    InteractionRequestLesson,
    InteractionResponse,
    InteractionResponseHistory,
    LessonAttendanceIntent,
    PersonNotificationOverride,
)
from krit_bot.communications import (
    EffectivePolicy,
    NotificationPolicyResolver,
    _campaign_delivery_counts_batch,
    _campaign_poll_counts_batch,
    _campaign_poll_details,
    _lesson_snapshot,
    _normalize_schedule_snapshot,
    _poll_targets,
    _schedule_text,
    apply_quiet_hours,
    cleanup_communication_history,
    daily_bundles,
    ensure_default_rules,
    lesson_confirmation_details,
    lesson_confirmation_details_batch,
    reconcile_confirmation_requests,
    reconcile_daily_reminders,
    record_message,
    render_bundle,
    save_interaction_response,
)
from krit_bot.config import Settings
from krit_bot.db import (
    Person,
    PersonRole,
    StudentGuardian,
    build_engine,
    build_session_factory,
    ensure_schema,
    utcnow,
)
from krit_bot.learning import _conflicts
from krit_bot.learning_models import (
    AdminNotification,
    GroupMembership,
    Lesson,
    LessonParticipant,
    LessonTeacherSegment,
    NotificationJob,
    PersonMaxIdentity,
    Room,
    StudyGroup,
    Subject,
)
from krit_bot.webhook import create_app


async def test_schedule_publication_serializes_period_dates(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'schedule-publish.db').as_posix()}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            response = await client.post(
                "/api/v1/communications/schedule/publish",
                headers=headers,
                json={
                    "date_from": date(2026, 10, 1).isoformat(),
                    "date_to": date(2026, 10, 8).isoformat(),
                    "preview": False,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["period_from"] == "2026-10-01"
            assert response.json()["period_to"] == "2026-10-08"


async def _database(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'communications.db'}")
    await ensure_schema(engine)
    return engine, build_session_factory(engine)


async def test_global_rule_update_keeps_identity_and_moves_person_override(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'rule-update.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            current = await client.get("/api/v1/communications/settings/global", headers=headers)
            assert current.status_code == 200
            rules = current.json()
            target = next(
                item
                for item in rules
                if item["event_code"] == "lesson_reminder"
                and item["recipient_context"] == "student"
                and item["offset_minutes"] == 180
            )

            engine = build_engine(database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                person = Person(full_name="Ученик", phone="+79000000201")
                session.add(person)
                await session.flush()
                session.add(
                    PersonNotificationOverride(
                        person_id=person.id,
                        recipient_context="student",
                        event_code="lesson_reminder",
                        offset_minutes=180,
                        state="off",
                    )
                )
                await session.commit()

            target["offset_minutes"] = 120
            saved = await client.put(
                "/api/v1/communications/settings/global", headers=headers, json=rules
            )
            assert saved.status_code == 200, saved.text
            matching = [
                item
                for item in saved.json()
                if item["event_code"] == "lesson_reminder"
                and item["recipient_context"] == "student"
                and item["offset_minutes"] in {120, 180}
            ]
            assert [(item["id"], item["offset_minutes"]) for item in matching] == [
                (target["id"], 120)
            ]

            async with sessions() as session:
                override = await session.scalar(select(PersonNotificationOverride))
                assert override is not None
                assert override.offset_minutes == 120
            await engine.dispose()


async def test_invalid_quiet_hours_are_rejected_without_saving(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'quiet-hours.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            current = await client.get("/api/v1/communications/settings/global", headers=headers)
            rules = current.json()
            rules[0]["quiet_start"] = "25:99"

            response = await client.put(
                "/api/v1/communications/settings/global", headers=headers, json=rules
            )

            assert response.status_code == 422
            unchanged = await client.get(
                "/api/v1/communications/settings/global", headers=headers
            )
            assert unchanged.json()[0]["quiet_start"] != "25:99"


async def test_late_lesson_queues_only_one_current_reminder_per_recipient(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    async with sessions() as session:
        student = Person(full_name="Поздний ученик", phone="+79000000211")
        teacher = Person(full_name="Поздний учитель", phone="+79000000212")
        subject = Subject(name="Информатика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=now + timedelta(minutes=30),
            end_at=now + timedelta(minutes=90),
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

        await reconcile_daily_reminders(session, now=now, timezone=ZoneInfo("UTC"))
        jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(
                        NotificationJob.event_type == "lesson_reminder"
                    )
                )
            ).all()
        )

        assert len(jobs) == 2
        assert {job.recipient_context for job in jobs} == {"student", "teacher"}
        assert all(":reminder:60:" in job.dedupe_key for job in jobs)
        assert all(
            (
                job.scheduled_at.replace(tzinfo=UTC)
                if job.scheduled_at.tzinfo is None
                else job.scheduled_at
            )
            == now
            for job in jobs
        )
        for job in jobs:
            job.status = "sent"
        await session.flush()

        await reconcile_daily_reminders(session, now=now, timezone=ZoneInfo("UTC"))
        all_jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(
                        NotificationJob.event_type == "lesson_reminder"
                    )
                )
            ).all()
        )
        assert len(all_jobs) == 2
    await engine.dispose()


async def test_manual_message_applies_policy_and_reports_excluded_recipient(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'manual-policy.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = build_engine(database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            allowed = Person(
                full_name="Учитель с рассылкой",
                phone="+79000000221",
                role_links=[PersonRole(role="teacher")],
            )
            blocked = Person(
                full_name="Ученик без рассылки",
                phone="+79000000222",
                role_links=[PersonRole(role="student")],
            )
            session.add_all([allowed, blocked])
            await session.flush()
            session.add_all(
                [
                    PersonMaxIdentity(
                        person_id=allowed.id,
                        verified_phone=allowed.phone,
                        max_user_id=2201,
                    ),
                    PersonMaxIdentity(
                        person_id=blocked.id,
                        verified_phone=blocked.phone,
                        max_user_id=2202,
                    ),
                    PersonNotificationOverride(
                        person_id=blocked.id,
                        recipient_context="student",
                        event_code="custom_message",
                        offset_minutes=-1,
                        state="off",
                    ),
                    PersonNotificationOverride(
                        person_id=blocked.id,
                        recipient_context="student",
                        event_code="custom_yes_no_request",
                        offset_minutes=-1,
                        state="off",
                    ),
                ]
            )
            await session.commit()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            body = {
                "person_ids": [allowed.id, blocked.id],
                "text": "Проверка ручной рассылки",
                "preview": True,
            }
            preview = await client.post(
                "/api/v1/communications/send-message", headers=headers, json=body
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["messages"] == 1
            assert preview.json()["excluded"] == [
                {"person_id": blocked.id, "reason": "disabled_by_policy"}
            ]

            poll_preview = await client.post(
                "/api/v1/communications/polls",
                headers=headers,
                json={
                    "person_ids": [blocked.id],
                    "text": "Будете?",
                    "preview": True,
                    "target_mode": "selected",
                },
            )
            assert poll_preview.status_code == 200, poll_preview.text
            assert poll_preview.json()["messages"] == 0
            assert poll_preview.json()["excluded"] == [
                {
                    "person_id": blocked.id,
                    "recipient_context": "student",
                    "subject_person_id": blocked.id,
                    "reason": "disabled_by_policy",
                }
            ]
            assert poll_preview.json()["targets"] == [
                {
                    "person_id": blocked.id,
                    "name": blocked.full_name,
                    "recipient_context": "student",
                    "subject_person_id": blocked.id,
                }
            ]
            invalid_lesson = await client.post(
                "/api/v1/communications/polls",
                headers=headers,
                json={
                    "person_ids": [allowed.id],
                    "text": "Будете?",
                    "preview": True,
                    "target_mode": "selected",
                    "related_lesson_id": 999_999,
                },
            )
            assert invalid_lesson.status_code == 404

            body["preview"] = False
            sent = await client.post(
                "/api/v1/communications/send-message", headers=headers, json=body
            )
            assert sent.status_code == 200, sent.text
            async with sessions() as session:
                jobs = list(
                    (
                        await session.scalars(
                            select(NotificationJob).where(
                                NotificationJob.campaign_id == sent.json()["campaign_id"]
                            )
                        )
                    ).all()
                )
                assert [(job.recipient_person_id, job.recipient_context) for job in jobs] == [
                    (allowed.id, "teacher")
                ]
        await engine.dispose()


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
        local_start = (utcnow().astimezone(timezone) + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
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
        scenario_now = utcnow()
        await reconcile_daily_reminders(session, now=scenario_now, timezone=timezone)
        await reconcile_confirmation_requests(session, now=scenario_now, timezone=timezone)
        jobs = list((await session.scalars(select(NotificationJob))).all())
        assert len([job for job in jobs if job.event_type == "lesson_reminder"]) == 6
        confirmation_jobs = [
            job for job in jobs if job.event_type == "lesson_confirmation_request"
        ]
        assert len(confirmation_jobs) == 3
        assert not [job for job in confirmation_jobs if job.payload.get("follow_up")]
        confirmation = next(
            job for job in confirmation_jobs if not job.payload.get("follow_up")
        )
        assert any(
            button.get("payload", "").endswith(":partial")
            for row in confirmation.payload["keyboard"]
            for button in row
        )
        request = await session.get(InteractionRequest, confirmation.interaction_request_id)
        assert request is not None
        await save_interaction_response(
            session,
            request=request,
            respondent_person_id=request.recipient_person_id,
            respondent_context=request.recipient_context,
            answer="yes",
        )
        assert request.status == "answered"
    await engine.dispose()


async def test_teacher_bundle_exists_when_lesson_has_no_students(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    timezone = ZoneInfo("Asia/Yekaterinburg")
    async with sessions() as session:
        teacher = Person(full_name="Учитель без группы", phone="+79000000231")
        subject = Subject(name="Информатика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([teacher, subject, room])
        await session.flush()
        start = datetime(2026, 10, 6, 10, 0, tzinfo=timezone)
        session.add(
            Lesson(
                subject_id=subject.id,
                teacher_id=teacher.id,
                room_id=room.id,
                start_at=start.astimezone(UTC),
                end_at=(start + timedelta(hours=1)).astimezone(UTC),
                teacher_name_snapshot=teacher.full_name,
                room_name_snapshot=room.name,
                subject_name_snapshot=subject.name,
            )
        )
        await session.flush()

        bundles = await daily_bundles(
            session,
            date_from=start - timedelta(days=1),
            date_to=start + timedelta(days=1),
            timezone=timezone,
        )

        assert len(bundles) == 1
        assert bundles[0].recipient_context == "teacher"
        assert bundles[0].lessons[0].students == ()
        assert "Ученики пока не назначены" in render_bundle(bundles[0])
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
        for index in range(3):
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
        follow_up = NotificationJob(
            dedupe_key=f"confirmation-test:{request.id}:followup",
            event_type="lesson_confirmation_request",
            recipient_context="student",
            recipient_person_id=student.id,
            subject_person_id=student.id,
            interaction_request_id=request.id,
            scheduled_at=utcnow() + timedelta(hours=1),
            status="pending",
            payload={"follow_up": True},
        )
        session.add(follow_up)
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
        assert [item.status for item in intents] == ["confirmed", "declined", "pending"]
        assert request.status == "active"
        assert follow_up.status == "pending"

        await save_interaction_response(
            session,
            request=request,
            respondent_person_id=student.id,
            respondent_context="student",
            answer="partial",
            lesson_answers={str(lessons[2].id): "yes"},
        )
        await session.flush()

        assert request.status == "answered"
        assert follow_up.status == "cancelled"
        assert [item.status for item in intents] == ["confirmed", "declined", "confirmed"]
        response = await session.scalar(select(InteractionResponse))
        assert response is not None
        assert response.lesson_answers == {
            str(lessons[0].id): "yes",
            str(lessons[1].id): "no",
            str(lessons[2].id): "yes",
        }
        assert await session.scalar(select(InteractionResponseHistory)) is not None
    await engine.dispose()


async def test_past_lesson_answer_is_rejected_without_persisting_response(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000901")
        teacher = Person(full_name="Учитель", phone="+79000000902")
        subject = Subject(name="Физика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=utcnow() - timedelta(hours=2),
            end_at=utcnow() - timedelta(hours=1),
            teacher_name_snapshot=teacher.full_name,
            room_name_snapshot=room.name,
            subject_name_snapshot=subject.name,
        )
        session.add(lesson)
        await session.flush()
        request = InteractionRequest(
            request_type="lesson_confirmation",
            question="Будете?",
            recipient_person_id=student.id,
            recipient_context="student",
            subject_person_id=student.id,
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
        await session.flush()

        with pytest.raises(ValueError, match="уже началось"):
            await save_interaction_response(
                session,
                request=request,
                respondent_person_id=student.id,
                respondent_context="student",
                answer="yes",
            )
        assert await session.scalar(select(InteractionResponse)) is None
    await engine.dispose()


async def test_one_parent_is_sufficient_and_parent_is_optional_when_not_linked(
    tmp_path,
) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000911")
        teacher = Person(full_name="Учитель", phone="+79000000912")
        parent_one = Person(full_name="Родитель 1", phone="+79000000913")
        parent_two = Person(full_name="Родитель 2", phone="+79000000914")
        subject = Subject(name="Физика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([student, teacher, parent_one, parent_two, subject, room])
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
        participant = LessonParticipant(
            lesson_id=lesson.id,
            person_id=student.id,
            person_name_snapshot=student.full_name,
        )
        session.add(participant)
        await session.flush()

        async def answer(person: Person, context: str, subject_person_id: int) -> None:
            request = InteractionRequest(
                request_type="lesson_confirmation",
                question="Будете?",
                recipient_person_id=person.id,
                recipient_context=context,
                subject_person_id=subject_person_id,
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
            await session.flush()
            await save_interaction_response(
                session,
                request=request,
                respondent_person_id=person.id,
                respondent_context=context,
                answer="yes",
            )

        await answer(student, "student", student.id)
        await answer(teacher, "teacher", teacher.id)
        without_parent = await lesson_confirmation_details(session, lesson, [participant])
        assert without_parent["state"] == "green"

        session.add_all(
            [
                StudentGuardian(student_id=student.id, guardian_id=parent_one.id),
                StudentGuardian(student_id=student.id, guardian_id=parent_two.id),
            ]
        )
        await session.flush()
        waiting_parent = await lesson_confirmation_details(session, lesson, [participant])
        assert waiting_parent["state"] == "yellow"

        await answer(parent_one, "guardian", student.id)
        one_parent_answered = await lesson_confirmation_details(session, lesson, [participant])
        assert one_parent_answered["state"] == "green"
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
        conflict = await session.scalar(select(AdminNotification))
        assert conflict is not None
        assert conflict.resolved_at is None

        await save_interaction_response(
            session,
            request=requests[1],
            respondent_person_id=guardian.id,
            respondent_context="guardian",
            answer="yes",
        )
        assert intent.status == "confirmed"
        assert conflict.resolved_at is not None

        await save_interaction_response(
            session,
            request=requests[1],
            respondent_person_id=guardian.id,
            respondent_context="guardian",
            answer="no",
        )
        assert intent.status == "conflict"
        assert conflict.resolved_at is None
        assert conflict.read_at is None
    await engine.dispose()


async def test_retry_failed_campaign_does_not_revive_cancelled_jobs(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'retry-failed.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = build_engine(database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            person = Person(full_name="Получатель", phone="+79000000024")
            campaign = CommunicationCampaign(
                campaign_type="manual_message",
                title="Проверка повторов",
                status="partial",
            )
            session.add_all([person, campaign])
            await session.flush()
            session.add_all(
                [
                    NotificationJob(
                        dedupe_key="retry-only-failed",
                        event_type="manual_message",
                        recipient_person_id=person.id,
                        campaign_id=campaign.id,
                        scheduled_at=utcnow(),
                        status="failed",
                    ),
                    NotificationJob(
                        dedupe_key="keep-intentionally-cancelled",
                        event_type="manual_message",
                        recipient_person_id=person.id,
                        campaign_id=campaign.id,
                        scheduled_at=utcnow(),
                        status="cancelled",
                    ),
                ]
            )
            await session.commit()
            campaign_id = int(campaign.id)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            response = await client.post(
                f"/api/v1/communications/campaigns/{campaign_id}/retry-failed",
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert response.json()["retried"] == 1
        async with sessions() as session:
            cancelled = await session.scalar(
                select(NotificationJob).where(
                    NotificationJob.dedupe_key == "keep-intentionally-cancelled"
                )
            )
            assert cancelled is not None
            assert cancelled.status == "cancelled"
        await engine.dispose()


async def test_anchor_moves_to_next_lesson_without_recreating_sent_offsets(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    timezone = ZoneInfo("Asia/Yekaterinburg")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone).astimezone(UTC)
    async with sessions() as session:
        student = Person(full_name="Артём Ученик", phone="+79000000041")
        teacher = Person(full_name="Анна Учитель", phone="+79000000042")
        subject = Subject(name="Математика", color="#2563eb")
        room = Room(name="Кабинет 1", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        lessons: list[Lesson] = []
        for hour in (14, 15, 18):
            start = datetime(2026, 10, 5, hour, 0, tzinfo=timezone)
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
            lessons.append(lesson)
        await session.flush()

        await reconcile_daily_reminders(session, now=now, timezone=timezone)
        student_jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(
                        NotificationJob.recipient_person_id == student.id,
                        NotificationJob.event_type == "lesson_reminder",
                    )
                )
            ).all()
        )
        assert len(student_jobs) == 2
        assert all(job.lesson_id == lessons[0].id for job in student_jobs)
        assert all(len(job.payload["lesson_ids"]) == 3 for job in student_jobs)
        sent = next(job for job in student_jobs if ":reminder:180:" in job.dedupe_key)
        sent.status = "sent"
        sent_key = sent.dedupe_key

        lessons[0].status = "cancelled"
        lessons[0].notification_revision += 1
        await reconcile_daily_reminders(session, now=now, timezone=timezone)
        await session.flush()

        assert sent.dedupe_key == sent_key
        pending = [job for job in student_jobs if job.status != "sent"]
        assert all(job.lesson_id == lessons[1].id for job in pending)
        assert all(job.payload["lesson_ids"] == [lessons[1].id, lessons[2].id] for job in pending)
        assert all("14:00" not in job.payload["text"] for job in pending)
        assert all("15:00" in job.payload["text"] for job in pending)
    await engine.dispose()


async def test_confirm_all_updates_every_lesson_but_not_attendance(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000051")
        teacher = Person(full_name="Учитель", phone="+79000000052")
        subject = Subject(name="Физика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([student, teacher, subject, room])
        await session.flush()
        request = InteractionRequest(
            request_type="lesson_confirmation",
            question="Будете на всех занятиях?",
            recipient_person_id=student.id,
            recipient_context="student",
            subject_person_id=student.id,
        )
        session.add(request)
        await session.flush()
        participants: list[LessonParticipant] = []
        for index in range(3):
            start = utcnow() + timedelta(days=2, hours=index)
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
            participant = LessonParticipant(
                lesson_id=lesson.id,
                person_id=student.id,
                person_name_snapshot=student.full_name,
            )
            participants.append(participant)
            session.add_all(
                [
                    participant,
                    InteractionRequestLesson(
                        request_id=request.id,
                        lesson_id=lesson.id,
                        lesson_revision=lesson.notification_revision,
                    ),
                ]
            )
        await save_interaction_response(
            session,
            request=request,
            respondent_person_id=student.id,
            respondent_context="student",
            answer="yes",
        )
        intents = list((await session.scalars(select(LessonAttendanceIntent))).all())
        assert len(intents) == 3
        assert {item.status for item in intents} == {"confirmed"}
        assert {item.attendance_status for item in participants} == {"expected"}
    await engine.dispose()


def test_quiet_hours_defer_normal_message_and_drop_stale_reminder() -> None:
    timezone = ZoneInfo("Asia/Yekaterinburg")
    scheduled = datetime(2026, 10, 1, 23, 0, tzinfo=timezone).astimezone(UTC)
    policy = EffectivePolicy(
        enabled=True,
        priority="normal",
        quiet_hours_policy="defer",
        quiet_start="22:00",
        quiet_end="08:00",
        configuration={},
        source="context",
    )
    deferred = apply_quiet_hours(scheduled, policy=policy, timezone=timezone)
    assert deferred is not None
    assert deferred.astimezone(timezone) == datetime(2026, 10, 2, 8, 0, tzinfo=timezone)
    assert (
        apply_quiet_hours(
            scheduled,
            policy=policy,
            timezone=timezone,
            meaningful_until=datetime(2026, 10, 2, 7, 0, tzinfo=timezone),
        )
        is None
    )


def test_schedule_diff_reports_semantic_changes() -> None:
    timezone = ZoneInfo("Asia/Yekaterinburg")
    old = {
        "lesson_id": 1,
        "revision": 1,
        "start_at": datetime(2026, 10, 5, 13, 0, tzinfo=UTC).isoformat(),
        "end_at": datetime(2026, 10, 5, 14, 0, tzinfo=UTC).isoformat(),
        "subject": "Математика",
        "teacher": "Иванов И.И.",
        "room": "Кабинет 1",
        "students": ["Анна"],
    }
    new = {
        **old,
        "revision": 2,
        "start_at": datetime(2026, 10, 5, 14, 0, tzinfo=UTC).isoformat(),
        "end_at": datetime(2026, 10, 5, 15, 0, tzinfo=UTC).isoformat(),
        "room": "Кабинет 2",
        "students": ["Анна", "Марина"],
    }
    text = _schedule_text([], changed=True, changed_items=[(old, new)], timezone=timezone)
    assert "05.10 18:00–19:00 → 05.10 19:00–20:00" in text
    assert "кабинет: Кабинет 1 → Кабинет 2" in text
    assert "добавлены ученики: Марина" in text


def test_added_lesson_text_is_specific_and_snapshots_are_recipient_safe() -> None:
    timezone = ZoneInfo("Asia/Yekaterinburg")
    lesson = type(
        "LessonValue",
        (),
        {
            "id": 7,
            "revision": 2,
            "start_at": datetime(2026, 10, 5, 10, 0, tzinfo=timezone),
            "end_at": datetime(2026, 10, 5, 11, 0, tzinfo=timezone),
            "subject": "Информатика",
            "teacher": "Олег Учитель",
            "room": "Кабинет 1",
            "students": ("Анна", "Марина"),
        },
    )()
    guardian_snapshot = _lesson_snapshot(lesson, recipient_context="guardian")
    teacher_snapshot = _lesson_snapshot(lesson, recipient_context="teacher")

    assert "students" not in guardian_snapshot
    assert teacher_snapshot["students"] == ["Анна", "Марина"]
    legacy = {**guardian_snapshot, "students": ["Чужой ребёнок"]}
    assert _normalize_schedule_snapshot(legacy, recipient_context="guardian") == (
        _normalize_schedule_snapshot(guardian_snapshot, recipient_context="guardian")
    )

    text = _schedule_text(
        [],
        changed=True,
        added=[guardian_snapshot],
        timezone=timezone,
    )
    assert "Добавлено занятие" in text
    assert "05.10 10:00–11:00" in text
    assert "Информатика" in text
    assert "Олег Учитель" in text


async def test_history_cleanup_keeps_business_confirmation(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    now = utcnow()
    async with sessions() as session:
        student = Person(full_name="Ученик", phone="+79000000061")
        session.add(student)
        await session.flush()
        request = InteractionRequest(
            request_type="yes_no",
            question="Подтвердите",
            recipient_person_id=student.id,
            recipient_context="student",
            subject_person_id=student.id,
        )
        session.add(request)
        await session.flush()
        response = await save_interaction_response(
            session,
            request=request,
            respondent_person_id=student.id,
            respondent_context="student",
            answer="yes",
        )
        session.add_all(
            [
                CommunicationMessage(
                    person_id=student.id,
                    direction="inbound",
                    text="Старое сообщение",
                    delivery_status="received",
                    created_at=now - timedelta(days=31),
                ),
                CommunicationMessage(
                    person_id=student.id,
                    direction="inbound",
                    text="Новое сообщение",
                    delivery_status="received",
                    created_at=now - timedelta(days=1),
                ),
            ]
        )
        await session.flush()
        assert await cleanup_communication_history(session, now=now) == 1
        messages = list((await session.scalars(select(CommunicationMessage))).all())
        assert [item.text for item in messages] == ["Новое сообщение"]
        assert await session.get(type(response), response.id) is not None
    await engine.dispose()


async def test_history_cleanup_removes_undelivered_messages_and_repairs_threads(
    tmp_path,
) -> None:
    engine, sessions = await _database(tmp_path)
    now = utcnow()
    async with sessions() as session:
        delivered_person = Person(full_name="Получатель", phone="+79000000063")
        unavailable_person = Person(full_name="Без MAX", phone="+79000000064")
        command_person = Person(full_name="Команды бота", phone="+79000000065")
        session.add_all([delivered_person, unavailable_person, command_person])
        await session.flush()

        delivered = await record_message(
            session,
            person_id=delivered_person.id,
            direction="outbound",
            text="Доставленное сообщение",
            delivery_status="sent",
        )
        delivered.created_at = now - timedelta(minutes=2)
        await record_message(
            session,
            person_id=delivered_person.id,
            direction="outbound",
            text="Не доставлено",
            delivery_status="failed",
        )
        await record_message(
            session,
            person_id=unavailable_person.id,
            direction="outbound",
            text="MAX недоступен",
            delivery_status="unavailable",
        )
        await record_message(
            session,
            person_id=command_person.id,
            direction="inbound",
            text="/start",
            delivery_status="received",
            message_type="command",
        )
        await record_message(
            session,
            person_id=command_person.id,
            direction="outbound",
            text="MAX недоступен после команды",
            delivery_status="unavailable",
        )
        await session.flush()

        assert await cleanup_communication_history(session, now=now) == 3
        messages = list(
            (
                await session.scalars(
                    select(CommunicationMessage).order_by(CommunicationMessage.id)
                )
            ).all()
        )
        assert [item.text for item in messages] == ["Доставленное сообщение", "/start"]

        repaired = await session.get(CommunicationThread, delivered_person.id)
        assert repaired is not None
        assert repaired.last_message_preview == "Доставленное сообщение"
        assert await session.get(CommunicationThread, unavailable_person.id) is None
        command_thread = await session.get(CommunicationThread, command_person.id)
        assert command_thread is not None
        assert command_thread.last_message_at is None
    await engine.dispose()


async def test_only_free_inbound_text_updates_unread_dialog_state(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        person = Person(full_name="Клиент", phone="+79000000062")
        session.add(person)
        await session.flush()
        await record_message(
            session,
            person_id=person.id,
            direction="inbound",
            text="/start",
            delivery_status="received",
            message_type="command",
        )
        thread = await session.get(CommunicationThread, person.id)
        assert thread is not None
        assert thread.admin_unread_count == 0
        assert thread.last_message_at is None

        await record_message(
            session,
            person_id=person.id,
            direction="inbound",
            text="Нужна помощь администратора",
            delivery_status="received",
        )
        await session.flush()
        last_message_at = thread.last_message_at
        assert thread.admin_unread_count == 1
        assert thread.last_message_preview == "Нужна помощь администратора"

        await record_message(
            session,
            person_id=person.id,
            direction="inbound",
            text="Ответ на запрос: Да",
            delivery_status="received",
            message_type="interaction_callback",
        )
        await session.flush()
        assert thread.admin_unread_count == 1
        assert thread.last_message_at == last_message_at
        assert thread.last_message_preview == "Нужна помощь администратора"
        messages = list(
            (
                await session.scalars(
                    select(CommunicationMessage).order_by(CommunicationMessage.id)
                )
            ).all()
        )
        assert [item.message_type for item in messages] == [
            "command",
            "text",
            "interaction_callback",
        ]
    await engine.dispose()


async def test_poll_details_keep_answers_delivery_and_family_agreement(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(
            full_name="Анна Ученица",
            phone="+79000000063",
            role_links=[PersonRole(role="student")],
        )
        guardian = Person(
            full_name="Ирина Родитель",
            phone="+79000000064",
            role_links=[PersonRole(role="parent")],
        )
        teacher = Person(
            full_name="Олег Учитель",
            phone="+79000000065",
            role_links=[PersonRole(role="teacher")],
        )
        campaign = CommunicationCampaign(
            campaign_type="custom_poll",
            title="Придёте на занятие?",
            payload={},
            status="completed",
        )
        session.add_all([student, guardian, teacher, campaign])
        await session.flush()
        session.add(StudentGuardian(student_id=student.id, guardian_id=guardian.id))
        requests = []
        for person, context in (
            (student, "student"),
            (guardian, "guardian"),
            (teacher, "teacher"),
        ):
            request = InteractionRequest(
                request_type="yes_no",
                question=campaign.title,
                recipient_person_id=person.id,
                recipient_context=context,
                subject_person_id=student.id,
                campaign_id=campaign.id,
            )
            session.add(request)
            await session.flush()
            requests.append(request)
            session.add(
                NotificationJob(
                    dedupe_key=f"poll-test:{person.id}",
                    event_type="custom_yes_no_request",
                    recipient_context=context,
                    recipient_person_id=person.id,
                    subject_person_id=request.subject_person_id,
                    campaign_id=campaign.id,
                    interaction_request_id=request.id,
                    scheduled_at=utcnow(),
                    status="sent",
                    payload={},
                )
            )
        await save_interaction_response(
            session,
            request=requests[0],
            respondent_person_id=student.id,
            respondent_context="student",
            answer="no",
        )
        await save_interaction_response(
            session,
            request=requests[1],
            respondent_person_id=guardian.id,
            respondent_context="guardian",
            answer="yes",
        )
        await session.flush()

        details = await _campaign_poll_details(session, campaign.id)
        assert details is not None
        assert details["counts"] == {
            "recipients": 3,
            "yes": 1,
            "no": 1,
            "no_response": 1,
        }
        assert details["recipients"][2]["delivery_status"] == "sent"
        assert details["recipients"][2]["answer"] is None
        assert details["agreements"][0]["guardians"] == [
            {"person_id": guardian.id, "name": "Ирина Родитель", "answer": "yes"}
        ]
        assert details["agreements"][0]["teachers"] == [
            {"person_id": teacher.id, "name": "Олег Учитель", "answer": None}
        ]
        assert details["agreements"][0]["result"] == "conflict"
    await engine.dispose()


async def test_poll_target_expansion_builds_student_family_teacher_card(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        student = Person(
            full_name="Анна Ученица",
            phone="+79000000081",
            role_links=[PersonRole(role="student")],
        )
        guardian = Person(
            full_name="Ирина Родитель",
            phone="+79000000082",
            role_links=[PersonRole(role="parent")],
        )
        teacher = Person(
            full_name="Олег Учитель",
            phone="+79000000083",
            role_links=[PersonRole(role="teacher")],
        )
        session.add_all([student, guardian, teacher])
        await session.flush()
        group = StudyGroup(name="Группа опроса", default_teacher_id=teacher.id)
        session.add(group)
        await session.flush()
        session.add_all(
            [
                StudentGuardian(student_id=student.id, guardian_id=guardian.id),
                GroupMembership(
                    group_id=group.id,
                    person_id=student.id,
                    start_at=utcnow() - timedelta(days=1),
                ),
            ]
        )
        await session.flush()

        targets = await _poll_targets(session, [student])

        assert set(targets) == {
            type(targets[0])(student.id, "student", student.id),
            type(targets[0])(guardian.id, "guardian", student.id),
            type(targets[0])(teacher.id, "teacher", student.id),
        }

        selected_only = await _poll_targets(session, [student], mode="selected")
        assert selected_only == [type(targets[0])(student.id, "student", student.id)]

        family = await _poll_targets(session, [student], mode="family")
        assert set(family) == {
            type(targets[0])(student.id, "student", student.id),
            type(targets[0])(guardian.id, "guardian", student.id),
        }
    await engine.dispose()


async def test_old_unclosed_teacher_segment_does_not_block_future_lesson(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        teacher = Person(full_name="Олег Учитель", phone="+79000000091")
        subject = Subject(name="Предмет старого занятия", color="#2563eb")
        room = Room(name="Кабинет старого занятия", capacity=10)
        session.add_all([teacher, subject, room])
        await session.flush()
        old_start = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=teacher.id,
            room_id=room.id,
            start_at=old_start,
            end_at=old_start + timedelta(hours=1),
            status="in_progress",
            teacher_name_snapshot=teacher.full_name,
            room_name_snapshot=room.name,
            subject_name_snapshot=subject.name,
        )
        session.add(lesson)
        await session.flush()
        session.add(
            LessonTeacherSegment(
                lesson_id=lesson.id,
                teacher_person_id=teacher.id,
                teacher_name_snapshot=teacher.full_name,
                started_at=old_start,
                segment_type="primary",
            )
        )
        await session.flush()

        new_start = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
        conflicts = await _conflicts(
            session,
            start_at=new_start,
            end_at=new_start + timedelta(hours=1),
            teacher_id=teacher.id,
            room_id=room.id,
            participant_ids=set(),
        )

        assert conflicts == []
    await engine.dispose()


async def test_multirole_guardian_bundles_keep_each_child_separate(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    timezone = ZoneInfo("Asia/Yekaterinburg")
    async with sessions() as session:
        sergey = Person(full_name="Сергей", phone="+79000000071")
        artem = Person(full_name="Артём", phone="+79000000072")
        maria = Person(full_name="Мария", phone="+79000000073")
        teacher = Person(full_name="Учитель", phone="+79000000074")
        subject = Subject(name="Русский язык", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        session.add_all([sergey, artem, maria, teacher, subject, room])
        await session.flush()
        session.add_all(
            [
                StudentGuardian(student_id=artem.id, guardian_id=sergey.id),
                StudentGuardian(student_id=maria.id, guardian_id=sergey.id),
            ]
        )
        start = datetime(2026, 10, 5, 14, 0, tzinfo=timezone)
        for index, student in enumerate((sergey, artem, maria)):
            lesson_start = start + timedelta(hours=index)
            lesson = Lesson(
                subject_id=subject.id,
                teacher_id=teacher.id,
                room_id=room.id,
                start_at=lesson_start.astimezone(UTC),
                end_at=(lesson_start + timedelta(hours=1)).astimezone(UTC),
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
            date_from=start - timedelta(days=1),
            date_to=start + timedelta(days=1),
            timezone=timezone,
        )
        sergey_bundles = [item for item in bundles if item.recipient_person_id == sergey.id]
        assert {
            (item.recipient_context, item.subject_person_id)
            for item in sergey_bundles
        } == {
            ("student", sergey.id),
            ("guardian", artem.id),
            ("guardian", maria.id),
        }
        guardian_bundles = [
            item for item in sergey_bundles if item.recipient_context == "guardian"
        ]
        assert all(len(item.lessons) == 1 for item in guardian_bundles)
    await engine.dispose()


async def test_batch_summaries_use_constant_query_count(tmp_path) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        teacher = Person(full_name="Учитель", phone="+79000000101")
        students = [
            Person(full_name=f"Ученик {index}", phone=f"+7900000011{index}")
            for index in range(3)
        ]
        subject = Subject(name="Математика", color="#2563eb")
        room = Room(name="Кабинет", capacity=10)
        campaigns = [
            CommunicationCampaign(
                campaign_type="custom_poll",
                title=f"Опрос {index}",
                payload={},
            )
            for index in range(3)
        ]
        session.add_all([teacher, *students, subject, room, *campaigns])
        await session.flush()
        lessons: list[Lesson] = []
        participants_by_lesson: dict[int, list[LessonParticipant]] = {}
        for index, student in enumerate(students):
            start_at = utcnow() + timedelta(days=index + 1)
            lesson = Lesson(
                subject_id=subject.id,
                teacher_id=teacher.id,
                room_id=room.id,
                start_at=start_at,
                end_at=start_at + timedelta(hours=1),
                teacher_name_snapshot=teacher.full_name,
                room_name_snapshot=room.name,
                subject_name_snapshot=subject.name,
            )
            session.add(lesson)
            await session.flush()
            participant = LessonParticipant(
                lesson_id=lesson.id,
                person_id=student.id,
                person_name_snapshot=student.full_name,
            )
            session.add(participant)
            lessons.append(lesson)
            participants_by_lesson[int(lesson.id)] = [participant]
            session.add(
                InteractionRequest(
                    request_type="yes_no",
                    question="Будете?",
                    recipient_person_id=student.id,
                    recipient_context="student",
                    subject_person_id=student.id,
                    campaign_id=campaigns[index].id,
                )
            )
        await session.commit()

        statements = 0

        def count_statement(*_args) -> None:
            nonlocal statements
            statements += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
        try:
            statements = 0
            one_confirmation = await lesson_confirmation_details_batch(
                session,
                lessons[:1],
                {int(lessons[0].id): participants_by_lesson[int(lessons[0].id)]},
            )
            one_confirmation_queries = statements
            statements = 0
            all_confirmations = await lesson_confirmation_details_batch(
                session, lessons, participants_by_lesson
            )
            all_confirmation_queries = statements
            statements = 0
            await _campaign_delivery_counts_batch(session, [int(campaigns[0].id)])
            await _campaign_poll_counts_batch(session, [int(campaigns[0].id)])
            one_campaign_queries = statements
            statements = 0
            delivery_counts = await _campaign_delivery_counts_batch(
                session, [int(campaign.id) for campaign in campaigns]
            )
            poll_counts = await _campaign_poll_counts_batch(
                session, [int(campaign.id) for campaign in campaigns]
            )
            all_campaign_queries = statements
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

        assert set(one_confirmation) == {int(lessons[0].id)}
        assert set(all_confirmations) == {int(lesson.id) for lesson in lessons}
        assert all_confirmation_queries == one_confirmation_queries
        assert all_campaign_queries == one_campaign_queries == 3
        assert delivery_counts == {}
        assert all(value["recipients"] == 1 for value in poll_counts.values())
    await engine.dispose()
