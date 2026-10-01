from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from krit_bot.communication_models import (
    CommunicationCampaign,
    CommunicationMessage,
    CommunicationThread,
    GuardianNotificationOverride,
    InteractionRequest,
    InteractionRequestLesson,
    LessonAttendanceIntent,
    PersonNotificationOverride,
)
from krit_bot.communications import (
    EffectivePolicy,
    NotificationPolicyResolver,
    _campaign_poll_details,
    _schedule_text,
    apply_quiet_hours,
    cleanup_communication_history,
    daily_bundles,
    ensure_default_rules,
    reconcile_confirmation_requests,
    reconcile_daily_reminders,
    record_message,
    save_interaction_response,
)
from krit_bot.db import (
    Person,
    PersonRole,
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
        confirmation_jobs = [
            job for job in jobs if job.event_type == "lesson_confirmation_request"
        ]
        assert len(confirmation_jobs) == 4
        assert len([job for job in confirmation_jobs if job.payload.get("follow_up")]) == 2
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
        linked_follow_up = next(
            job
            for job in confirmation_jobs
            if job.interaction_request_id == request.id and job.payload.get("follow_up")
        )
        assert linked_follow_up.status == "cancelled"
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
        assert len(student_jobs) == 3
        assert all(job.lesson_id == lessons[0].id for job in student_jobs)
        assert all(len(job.payload["lesson_ids"]) == 3 for job in student_jobs)
        sent = next(job for job in student_jobs if ":reminder:1440:" in job.dedupe_key)
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
                subject_person_id=student.id if context != "teacher" else None,
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
        assert details["agreements"][0]["teacher_name"] == "Олег Учитель"
        assert details["agreements"][0]["teacher_answer"] is None
        assert details["agreements"][0]["result"] == "conflict"
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
