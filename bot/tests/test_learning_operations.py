from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_operational_lesson_flow_and_identity_guards(tmp_path) -> None:
    database = tmp_path / "operations.db"
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
            token = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin"},
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            async def create_person(
                name: str,
                phone: str,
                roles: list[str],
                max_auth_phone: str | None = None,
            ) -> int:
                response = await client.post(
                    "/api/v1/people",
                    headers=headers,
                    json={
                        "full_name": name,
                        "phone": phone,
                        "max_auth_phone": max_auth_phone,
                        "roles": roles,
                    },
                )
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            teacher = await create_person("Иванов Иван Иванович", "+79000000001", ["teacher"])
            substitute = await create_person("Петров Пётр Петрович", "+79000000002", ["teacher"])
            student = await create_person("Сидоров Сергей Сергеевич", "+79000000003", ["student"])
            absent = await create_person("Орлова Анна Олеговна", "+79000000004", ["student"])
            guardian = await create_person("Сидорова Мария Ивановна", "+79000000003", ["parent"])
            assert (
                await client.post(f"/api/v1/people/{student}/guardians/{guardian}", headers=headers)
            ).status_code == 200

            subject = (
                await client.post(
                    "/api/v1/learning/subjects",
                    headers=headers,
                    json={
                        "name": "Математика",
                        "color": "#2563eb",
                        "teacher_ids": [teacher, substitute],
                    },
                )
            ).json()["id"]
            room = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": "Кабинет 1", "capacity": 10},
                )
            ).json()["id"]
            room_two = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": "Кабинет 2", "capacity": 10},
                )
            ).json()["id"]
            start = datetime.now(UTC) + timedelta(hours=2)
            payload = {
                "subject_id": subject,
                "teacher_id": teacher,
                "room_id": room,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "participant_ids": [student, absent],
            }
            lesson = await client.post("/api/v1/learning/lessons", headers=headers, json=payload)
            assert lesson.status_code == 201, lesson.text
            lesson_id = int(lesson.json()["id"])

            cancelled = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student}/cancel",
                headers=headers,
                json={
                    "cancelled_by": "guardian",
                    "cancelled_by_person_id": guardian,
                    "reason": "Семейные обстоятельства",
                },
            )
            assert cancelled.status_code == 200, cancelled.text
            changed_room = await client.put(
                f"/api/v1/learning/lessons/{lesson_id}",
                headers=headers,
                json={**payload, "room_id": room_two},
            )
            saved_student = next(
                item for item in changed_room.json()["participants"] if item["person_id"] == student
            )
            assert saved_student["attendance_status"] == "excused"
            assert saved_student["cancelled_by_person_id"] == guardian
            assert saved_student["cancellation_reason"] == "Семейные обстоятельства"

            await client.post(f"/api/v1/learning/lessons/{lesson_id}/start", headers=headers)
            await client.post(f"/api/v1/learning/presence/{student}/arrival", headers=headers)
            current = (
                await client.get(f"/api/v1/learning/lesson/{lesson_id}", headers=headers)
            ).json()
            assert (
                next(item for item in current["participants"] if item["person_id"] == student)[
                    "attendance_status"
                ]
                == "excused"
            )

            await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student}/restore",
                headers=headers,
            )
            restored_attendance = await client.put(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student}/attendance",
                headers=headers,
                json={"status": "present"},
            )
            assert restored_attendance.status_code == 200
            structural = await client.put(
                f"/api/v1/learning/lessons/{lesson_id}", headers=headers, json=payload
            )
            assert structural.status_code == 409
            left = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student}/leave-early",
                headers=headers,
                json={"reason": "Плохое самочувствие"},
            )
            assert left.status_code == 200, left.text
            assert left.json()["early_leave_reason"] == "Плохое самочувствие"
            today = (await client.get("/api/v1/learning/today", headers=headers)).json()
            assert any(item["person_id"] == student for item in today["present"])

            replacement = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/teacher-transition",
                headers=headers,
                json={
                    "action": "substitute",
                    "replacement_teacher_id": substitute,
                    "reason": "Экстренный уход преподавателя",
                },
            )
            assert replacement.status_code == 200, replacement.text
            assert len(replacement.json()["teacher_segments"]) == 2
            completed = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/finish-early",
                headers=headers,
                json={
                    "reason": "Продолжать занятие невозможно",
                    "public_comment": "Занятие завершено раньше.",
                },
            )
            assert completed.status_code == 200, completed.text
            assert completed.json()["status"] == "completed"
            assert completed.json()["completion_type"] == "early"
            old_attendance = await client.put(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{absent}/attendance",
                headers=headers,
                json={"status": "present"},
            )
            assert old_attendance.status_code == 409
            first_history = (
                await client.get(f"/api/v1/learning/history/teacher/{teacher}", headers=headers)
            ).json()
            second_history = (
                await client.get(f"/api/v1/learning/history/teacher/{substitute}", headers=headers)
            ).json()
            assert first_history["lessons"][0]["teacher_segment_type"] == "primary"
            assert second_history["lessons"][0]["teacher_segment_type"] == "substitute"

            future_start = start + timedelta(days=3)
            future = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **payload,
                    "start_at": future_start.isoformat(),
                    "end_at": (future_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [absent],
                },
            )
            assert future.status_code == 201, future.text
            blocked_archive = await client.post(
                f"/api/v1/people/{teacher}/archive", headers=headers, json={}
            )
            assert blocked_archive.status_code == 409
            assert blocked_archive.json()["detail"]["dependencies"]["teacher_lessons"]["count"] == 1
            role_removal = await client.put(
                f"/api/v1/people/{teacher}",
                headers=headers,
                json={
                    "full_name": "Иванов Иван Иванович",
                    "phone": "+79000000001",
                    "roles": ["parent"],
                },
            )
            assert role_removal.status_code == 409
            blocked_student = await client.post(
                f"/api/v1/people/{absent}/archive", headers=headers, json={}
            )
            assert blocked_student.status_code == 409
            resolved_student = await client.post(
                f"/api/v1/people/{absent}/archive",
                headers=headers,
                json={"resolve_future_student_dependencies": True},
            )
            assert resolved_student.status_code == 200, resolved_student.text
            future_view = (
                await client.get(f"/api/v1/learning/lesson/{future.json()['id']}", headers=headers)
            ).json()
            assert future_view["participants"][0]["attendance_status"] == "excused"

            first_shared = await create_person(
                "Общий Номер Первый", "+79991112233", ["parent"], "+79991112233"
            )
            assert first_shared
            second_shared = await create_person("Общий Номер Второй", "+79991112233", ["student"])
            assert second_shared
            duplicate_auth = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Дубликат Авторизации",
                    "phone": "+79990001122",
                    "max_auth_phone": "+79991112233",
                    "roles": ["parent"],
                },
            )
            assert duplicate_auth.status_code == 409

            near_start = datetime.now(UTC) + timedelta(minutes=5)
            near_lesson = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **payload,
                    "room_id": room,
                    "start_at": near_start.isoformat(),
                    "end_at": (near_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student],
                },
            )
            assert near_lesson.status_code == 201, near_lesson.text
            await asyncio.gather(
                client.get("/api/v1/learning/today", headers=headers),
                client.get("/api/v1/learning/today", headers=headers),
            )
            updated_near = await client.put(
                f"/api/v1/learning/lessons/{near_lesson.json()['id']}",
                headers=headers,
                json={
                    **payload,
                    "room_id": room,
                    "start_at": near_start.isoformat(),
                    "end_at": (near_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student, second_shared],
                },
            )
            assert updated_near.status_code == 200, updated_near.text
            assert updated_near.json()["active_participant_count"] == 2
            refreshed_today = (
                await client.get("/api/v1/learning/today", headers=headers)
            ).json()
            near_alerts = [
                item
                for item in refreshed_today["alerts"]
                if item.get("lesson_id") == near_lesson.json()["id"]
                and item.get("kind") == "lesson_starts_soon"
            ]
            assert len(near_alerts) == 1
            assert "Прибыли 1 из 2" in near_alerts[0]["message"]

    connection = sqlite3.connect(database)
    try:
        teacher_reminders = connection.execute(
            "SELECT COUNT(*) FROM learning_notification_jobs "
            "WHERE lesson_id = ? AND recipient_person_id = ? "
            "AND event_type LIKE 'lesson_reminder_%'",
            (future.json()["id"], teacher),
        ).fetchone()[0]
        assert teacher_reminders == 3
        absent_notifications = connection.execute(
            "SELECT COUNT(*) FROM learning_notification_jobs "
            "WHERE lesson_id = ? AND recipient_person_id = ? "
            "AND event_type IN ('lesson_started', 'lesson_finished')",
            (lesson_id, absent),
        ).fetchone()[0]
        assert absent_notifications == 0
        no_active_alerts = connection.execute(
            "SELECT COUNT(*) FROM learning_admin_notifications WHERE dedupe_key = ?",
            (f"lesson:{lesson_id}:no_active_students",),
        ).fetchone()[0]
        assert no_active_alerts == 1
        concurrent_alerts = connection.execute(
            "SELECT COUNT(*) FROM learning_admin_notifications WHERE dedupe_key = ?",
            (f"lesson:{near_lesson.json()['id']}:starting_soon",),
        ).fetchone()[0]
        assert concurrent_alerts == 1
        added_immediate_max_jobs = connection.execute(
            "SELECT COUNT(*) FROM learning_notification_jobs "
            "WHERE lesson_id = ? AND event_type = 'lesson_participant_added'",
            (near_lesson.json()["id"],),
        ).fetchone()[0]
        assert added_immediate_max_jobs == 0
    finally:
        connection.close()
