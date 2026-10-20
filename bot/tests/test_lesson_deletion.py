from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from krit_bot.communication_models import (
    InteractionRequest,
    InteractionRequestLesson,
    LessonAttendanceIntent,
    ScheduleRecipientSnapshot,
)
from krit_bot.config import Settings
from krit_bot.db import build_engine, build_session_factory
from krit_bot.learning_models import (
    AdminNotification,
    Lesson,
    LessonParticipant,
    LessonSeries,
    NotificationJob,
)
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_delete_lesson_purges_it_and_notifies_only_delivered_recipients(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'lesson-delete.db').as_posix()}"
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
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin"},
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            async def create_person(name: str, phone: str, roles: list[str]) -> int:
                response = await client.post(
                    "/api/v1/people",
                    headers=headers,
                    json={"full_name": name, "phone": phone, "roles": roles},
                )
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            teacher_id = await create_person(
                "Учитель Тестовый", "+79000001001", ["teacher"]
            )
            notified_student_id = await create_person(
                "Ученик Уведомлённый", "+79000001002", ["student"]
            )
            unpublished_student_id = await create_person(
                "Ученик Без Рассылки", "+79000001003", ["student"]
            )
            subject_id = int(
                (
                    await client.post(
                        "/api/v1/learning/subjects",
                        headers=headers,
                        json={
                            "name": "Тест удаления",
                            "color": "#2563eb",
                            "teacher_ids": [teacher_id],
                        },
                    )
                ).json()["id"]
            )
            room_id = int(
                (
                    await client.post(
                        "/api/v1/learning/rooms",
                        headers=headers,
                        json={"name": "Кабинет удаления", "capacity": 10},
                    )
                ).json()["id"]
            )
            starts_at = datetime.now(UTC) + timedelta(days=7)
            created = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    "subject_id": subject_id,
                    "teacher_id": teacher_id,
                    "room_id": room_id,
                    "start_at": starts_at.isoformat(),
                    "end_at": (starts_at + timedelta(hours=1)).isoformat(),
                    "participant_ids": [
                        notified_student_id,
                        unpublished_student_id,
                    ],
                },
            )
            assert created.status_code == 201, created.text
            lesson_id = int(created.json()["id"])

            engine = build_engine(database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                series = LessonSeries(
                    subject_id=subject_id,
                    teacher_id=teacher_id,
                    room_id=room_id,
                    starts_at=starts_at,
                    duration_minutes=60,
                    interval_weeks=1,
                    occurrences=1,
                )
                session.add(series)
                await session.flush()
                series_id = int(series.id)
                lesson = await session.get(Lesson, lesson_id)
                assert lesson is not None
                lesson.series_id = series_id
                lesson.series_occurrence_index = 1
                request = InteractionRequest(
                    request_type="lesson_confirmation",
                    question="Подтвердите занятие",
                    recipient_person_id=notified_student_id,
                    recipient_context="student",
                    subject_person_id=notified_student_id,
                    related_lesson_id=lesson_id,
                    status="active",
                )
                session.add(request)
                await session.flush()
                request_id = int(request.id)
                session.add(
                    InteractionRequestLesson(
                        request_id=request_id,
                        lesson_id=lesson_id,
                        lesson_revision=1,
                    )
                )
                session.add(
                    ScheduleRecipientSnapshot(
                        recipient_person_id=notified_student_id,
                        recipient_context="student",
                        subject_person_id=notified_student_id,
                        lesson_id=lesson_id,
                        lesson_revision=1,
                        snapshot={
                            "lesson_id": lesson_id,
                            "revision": 1,
                            "start_at": starts_at.isoformat(),
                        },
                    )
                )
                session.add(
                    NotificationJob(
                        dedupe_key=f"lesson:{lesson_id}:reminder:test",
                        event_type="lesson_reminder",
                        lesson_id=lesson_id,
                        recipient_person_id=notified_student_id,
                        recipient_context="student",
                        subject_person_id=notified_student_id,
                        scheduled_at=starts_at - timedelta(hours=3),
                        status="pending",
                        payload={"text": "Напоминание"},
                    )
                )
                session.add(
                    AdminNotification(
                        dedupe_key=f"lesson:{lesson_id}:test-alert",
                        kind="lesson_test",
                        title="Проверка",
                        message="Связанное уведомление",
                        lesson_id=lesson_id,
                    )
                )
                await session.commit()

            deleted = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/cancel",
                headers=headers,
                json={"reason": "Удалено администратором"},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json() == {
                "id": lesson_id,
                "deleted": True,
                "notifications_queued": 1,
            }

            async with sessions() as session:
                assert await session.get(Lesson, lesson_id) is None
                assert await session.get(LessonSeries, series_id) is None
                assert (
                    await session.scalar(
                        select(func.count(LessonParticipant.id)).where(
                            LessonParticipant.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        select(func.count(LessonAttendanceIntent.id)).where(
                            LessonAttendanceIntent.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        select(func.count(ScheduleRecipientSnapshot.id)).where(
                            ScheduleRecipientSnapshot.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        select(func.count(AdminNotification.id)).where(
                            AdminNotification.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
                cancelled_request = await session.get(InteractionRequest, request_id)
                assert cancelled_request is not None
                assert cancelled_request.status == "cancelled"
                assert cancelled_request.related_lesson_id is None
                assert (
                    await session.scalar(
                        select(func.count()).select_from(InteractionRequestLesson).where(
                            InteractionRequestLesson.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
                cancellation_jobs = list(
                    (
                        await session.scalars(
                            select(NotificationJob).where(
                                NotificationJob.event_type == "lesson_cancelled",
                                NotificationJob.dedupe_key.like(
                                    f"lesson:{lesson_id}:cancelled:%"
                                ),
                            )
                        )
                    ).all()
                )
                assert [job.recipient_person_id for job in cancellation_jobs] == [
                    notified_student_id
                ]
                assert cancellation_jobs[0].lesson_id is None
                assert (
                    await session.scalar(
                        select(func.count(NotificationJob.id)).where(
                            NotificationJob.lesson_id == lesson_id
                        )
                    )
                    == 0
                )
            await engine.dispose()

            student_history = await client.get(
                f"/api/v1/learning/history/person/{notified_student_id}",
                headers=headers,
            )
            assert student_history.status_code == 200, student_history.text
            assert all(
                item["id"] != lesson_id
                for item in student_history.json()["lessons"]
            )
