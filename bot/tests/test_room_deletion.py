from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from krit_bot.communication_models import (
    CommunicationMessage,
    InteractionRequest,
    InteractionRequestLesson,
)
from krit_bot.config import Settings
from krit_bot.db import build_engine, build_session_factory
from krit_bot.learning_models import AuditEvent, Lesson, LessonSeries, NotificationJob, Room
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_superadmin_permanently_deletes_room_and_all_learning_links(tmp_path) -> None:
    database = tmp_path / "room-deletion.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
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
            super_headers = {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }
            operator = await client.post(
                "/api/v1/administration/users",
                headers=super_headers,
                json={
                    "full_name": "Обычный администратор",
                    "username": "operator",
                    "role": "administrator",
                    "password": "operator-password",
                },
            )
            assert operator.status_code == 201, operator.text
            operator_login = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": "operator-password"},
            )
            operator_headers = {
                "Authorization": f"Bearer {operator_login.json()['access_token']}"
            }

            unused_room = await client.post(
                "/api/v1/learning/rooms",
                headers=super_headers,
                json={"name": "Свободный кабинет", "capacity": 5},
            )
            unused_deleted = await client.delete(
                f"/api/v1/learning/rooms/{unused_room.json()['id']}",
                headers=operator_headers,
                params={"cascade": "true"},
            )
            assert unused_deleted.status_code == 200, unused_deleted.text

            async def person(name: str, phone: str, roles: list[str]) -> int:
                response = await client.post(
                    "/api/v1/people",
                    headers=super_headers,
                    json={"full_name": name, "phone": phone, "roles": roles},
                )
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            teacher_id = await person("Учитель Кабинета", "+79000002101", ["teacher"])
            student_id = await person("Ученик Кабинета", "+79000002102", ["student"])
            subject = await client.post(
                "/api/v1/learning/subjects",
                headers=super_headers,
                json={
                    "name": "Предмет кабинета",
                    "color": "#2563eb",
                    "teacher_ids": [teacher_id],
                },
            )
            room = await client.post(
                "/api/v1/learning/rooms",
                headers=super_headers,
                json={"name": "Удаляемый кабинет", "capacity": 10},
            )
            room_id = int(room.json()["id"])
            start = datetime.now(UTC) + timedelta(days=2)
            lesson = await client.post(
                "/api/v1/learning/series",
                headers=super_headers,
                json={
                    "subject_id": int(subject.json()["id"]),
                    "teacher_id": teacher_id,
                    "room_id": room_id,
                    "starts_at": start.isoformat(),
                    "duration_minutes": 60,
                    "interval_weeks": 1,
                    "occurrences": 2,
                    "participant_ids": [student_id],
                },
            )
            assert lesson.status_code == 201, lesson.text
            lesson_id = int(lesson.json()["lesson_ids"][0])

            engine = build_engine(settings.database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                series_id = await session.scalar(
                    select(Lesson.series_id).where(Lesson.id == lesson_id)
                )
                assert series_id is not None
                request_id = await session.scalar(
                    select(InteractionRequestLesson.request_id).where(
                        InteractionRequestLesson.lesson_id == lesson_id
                    )
                )
                assert request_id is not None
                linked_job = next(
                    job
                    for job in list((await session.scalars(select(NotificationJob))).all())
                    if lesson_id
                    in [int(value) for value in (job.payload or {}).get("lesson_ids", [])]
                )
                message = CommunicationMessage(
                    person_id=student_id,
                    direction="outbound",
                    message_type="text",
                    text="Уведомление о занятии в удаляемом кабинете",
                    delivery_status="sent",
                    related_lesson_id=lesson_id,
                    interaction_request_id=int(request_id),
                    outbox_job_id=int(linked_job.id),
                )
                session.add(message)
                await session.commit()
                message_id = int(message.id)
                linked_job_id = int(linked_job.id)

            confirmation_required = await client.delete(
                f"/api/v1/learning/rooms/{room_id}", headers=super_headers
            )
            assert confirmation_required.status_code == 409

            forbidden = await client.delete(
                f"/api/v1/learning/rooms/{room_id}",
                headers=operator_headers,
                params={"cascade": "true"},
            )
            assert forbidden.status_code == 403

            deleted = await client.delete(
                f"/api/v1/learning/rooms/{room_id}",
                headers=super_headers,
                params={"cascade": "true"},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["deleted"] is True
            assert deleted.json()["lessons_deleted"] == 2
            assert deleted.json()["series_deleted"] == 1

        async with sessions() as session:
            assert await session.get(Room, room_id) is None
            assert await session.scalar(
                select(Lesson.id).where(Lesson.room_id == room_id)
            ) is None
            assert await session.get(LessonSeries, int(series_id)) is None
            assert await session.get(InteractionRequest, int(request_id)) is None
            assert await session.get(NotificationJob, linked_job_id) is None
            assert await session.get(CommunicationMessage, message_id) is None
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "learning.room_deleted",
                    AuditEvent.entity_id == room_id,
                )
            )
            assert audit is not None
        await engine.dispose()
