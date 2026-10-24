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
from krit_bot.learning_models import AuditEvent, Lesson, NotificationJob, StudyGroup, Subject
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_superadmin_permanently_deletes_subject_and_all_learning_links(tmp_path) -> None:
    database = tmp_path / "subject-deletion.db"
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

            unused_subject = await client.post(
                "/api/v1/learning/subjects",
                headers=super_headers,
                json={"name": "Неиспользуемый предмет", "color": "#2563eb"},
            )
            unused_deleted = await client.delete(
                f"/api/v1/learning/subjects/{unused_subject.json()['id']}",
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

            teacher_id = await person("Учитель Предмета", "+79000002001", ["teacher"])
            student_id = await person("Ученик Предмета", "+79000002002", ["student"])
            subject = await client.post(
                "/api/v1/learning/subjects",
                headers=super_headers,
                json={
                    "name": "Удаляемый предмет",
                    "color": "#2563eb",
                    "teacher_ids": [teacher_id],
                },
            )
            assert subject.status_code == 201, subject.text
            subject_id = int(subject.json()["id"])
            group = await client.post(
                "/api/v1/learning/groups",
                headers=super_headers,
                json={
                    "name": "Группа удаляемого предмета",
                    "subject_id": subject_id,
                    "default_teacher_id": teacher_id,
                    "default_duration_minutes": 60,
                },
            )
            assert group.status_code == 201, group.text
            group_id = int(group.json()["id"])
            room = await client.post(
                "/api/v1/learning/rooms",
                headers=super_headers,
                json={"name": "Кабинет удаления", "capacity": 10},
            )
            start = datetime.now(UTC) + timedelta(days=2)
            lesson = await client.post(
                "/api/v1/learning/lessons",
                headers=super_headers,
                json={
                    "subject_id": subject_id,
                    "teacher_id": teacher_id,
                    "room_id": int(room.json()["id"]),
                    "group_id": group_id,
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student_id],
                },
            )
            assert lesson.status_code == 201, lesson.text
            lesson_id = int(lesson.json()["id"])

            engine = build_engine(settings.database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                request_id = await session.scalar(
                    select(InteractionRequestLesson.request_id).where(
                        InteractionRequestLesson.lesson_id == lesson_id
                    )
                )
                assert request_id is not None
                jobs_before = list((await session.scalars(select(NotificationJob))).all())
                linked_job = next(
                    job
                    for job in jobs_before
                    if lesson_id
                    in [int(value) for value in (job.payload or {}).get("lesson_ids", [])]
                )
                message = CommunicationMessage(
                    person_id=student_id,
                    direction="outbound",
                    message_type="text",
                    text="Уведомление об удаляемом занятии",
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
                f"/api/v1/learning/subjects/{subject_id}",
                headers=super_headers,
            )
            assert confirmation_required.status_code == 409

            forbidden = await client.delete(
                f"/api/v1/learning/subjects/{subject_id}",
                headers=operator_headers,
                params={"cascade": "true"},
            )
            assert forbidden.status_code == 403

            deleted = await client.delete(
                f"/api/v1/learning/subjects/{subject_id}",
                headers=super_headers,
                params={"cascade": "true"},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["deleted"] is True
            assert deleted.json()["lessons_deleted"] == 1
            assert deleted.json()["groups_unlinked"] == 1

            assert (
                await client.get(
                    f"/api/v1/learning/lesson/{lesson_id}", headers=super_headers
                )
            ).status_code == 404

        async with sessions() as session:
            assert await session.get(Subject, subject_id) is None
            assert await session.scalar(
                select(Lesson.id).where(Lesson.subject_id == subject_id)
            ) is None
            saved_group = await session.get(StudyGroup, group_id)
            assert saved_group is not None
            assert saved_group.subject_id is None
            assert saved_group.default_teacher_id is None
            assert await session.scalar(
                select(InteractionRequestLesson.request_id).where(
                    InteractionRequestLesson.lesson_id == lesson_id
                )
            ) is None
            requests = list((await session.scalars(select(InteractionRequest))).all())
            assert all(request.related_lesson_id != lesson_id for request in requests)
            assert await session.get(InteractionRequest, int(request_id)) is None
            assert await session.get(NotificationJob, linked_job_id) is None
            assert await session.get(CommunicationMessage, message_id) is None
            jobs = list((await session.scalars(select(NotificationJob))).all())
            assert all(
                lesson_id
                not in [int(value) for value in (job.payload or {}).get("lesson_ids", [])]
                for job in jobs
            )
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "learning.subject_deleted",
                    AuditEvent.entity_id == subject_id,
                )
            )
            assert audit is not None
        await engine.dispose()

