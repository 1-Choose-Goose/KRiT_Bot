from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_learning_schedule_conflicts_capacity_and_lifecycle(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'learning.db').as_posix()}",
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

            async def person(
                name: str, phone: str, roles: list[str], *, active: bool = True
            ) -> int:
                response = await client.post(
                    "/api/v1/people",
                    headers=headers,
                    json={
                        "full_name": name,
                        "phone": phone,
                        "roles": roles,
                        "active": active,
                    },
                )
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            teacher = await person("Иванова Мария Сергеевна", "+79000000001", ["teacher", "parent"])
            teacher_without_bot_access = await person(
                "Быков Валерий Андреевич",
                "+79000000007",
                ["teacher"],
                active=False,
            )
            student_one = await person("Петров Иван Олегович", "+79000000002", ["student"])
            student_two = await person("Сидорова Анна Ильинична", "+79000000003", ["student"])
            student_three = await person("Орлов Пётр Андреевич", "+79000000004", ["student"])
            student_without_bot_access = await person(
                "Куц Олег Олегович",
                "+79000000008",
                ["student"],
                active=False,
            )

            subject_response = await client.post(
                "/api/v1/learning/subjects",
                headers=headers,
                json={
                    "name": "Робототехника",
                    "color": "#2563eb",
                    "teacher_ids": [teacher, teacher_without_bot_access],
                },
            )
            room_response = await client.post(
                "/api/v1/learning/rooms",
                headers=headers,
                json={"name": "Кабинет 1", "capacity": 2},
            )
            subject = subject_response.json()["id"]
            room = room_response.json()["id"]
            start = datetime.now(UTC) + timedelta(hours=2)
            end = start + timedelta(hours=1)

            base = {
                "subject_id": subject,
                "teacher_id": teacher,
                "room_id": room,
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "participant_ids": [student_one, student_two],
            }
            created = await client.post("/api/v1/learning/lessons", headers=headers, json=base)
            assert created.status_code == 201, created.text
            lesson_id = created.json()["id"]
            assert len(created.json()["participants"]) == 2

            group_response = await client.post(
                "/api/v1/learning/groups",
                headers=headers,
                json={
                    "name": "Группа робототехники",
                    "subject_id": subject,
                    "default_teacher_id": teacher,
                    "default_duration_minutes": 60,
                    "active": True,
                },
            )
            assert group_response.status_code == 201, group_response.text
            group_id = group_response.json()["id"]
            membership_response = await client.post(
                f"/api/v1/learning/groups/{group_id}/memberships",
                headers=headers,
                json={
                    "person_id": student_one,
                    "start_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "end_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                },
            )
            assert membership_response.status_code == 201, membership_response.text
            membership_without_bot = await client.post(
                f"/api/v1/learning/groups/{group_id}/memberships",
                headers=headers,
                json={
                    "person_id": student_without_bot_access,
                    "start_at": datetime.now(UTC).isoformat(),
                    "end_at": None,
                },
            )
            assert membership_without_bot.status_code == 201, membership_without_bot.text

            references = await client.get("/api/v1/learning/reference-data", headers=headers)
            assert references.status_code == 200
            reference_teachers = {item["id"] for item in references.json()["teachers"]}
            assert teacher_without_bot_access in reference_teachers
            assert teacher_without_bot_access in references.json()["subjects"][0]["teacher_ids"]
            group_reference = next(
                item for item in references.json()["groups"] if item["id"] == group_id
            )
            assert {item["person_id"] for item in group_reference["memberships"]} == {
                student_one,
                student_without_bot_access,
            }

            inactive_teacher_lesson = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": teacher_without_bot_access,
                    "start_at": (end + timedelta(hours=2)).isoformat(),
                    "end_at": (end + timedelta(hours=3)).isoformat(),
                    "participant_ids": [student_without_bot_access],
                },
            )
            assert inactive_teacher_lesson.status_code == 201, inactive_teacher_lesson.text

            excluded_group_member = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "group_id": group_id,
                    "start_at": (end + timedelta(hours=5)).isoformat(),
                    "end_at": (end + timedelta(hours=6)).isoformat(),
                    "participant_ids": [],
                    "excluded_participant_ids": [student_without_bot_access],
                },
            )
            assert excluded_group_member.status_code == 201, excluded_group_member.text
            assert excluded_group_member.json()["participants"] == []

            too_many = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={**base, "participant_ids": [student_one, student_two, student_three]},
            )
            assert too_many.status_code == 409
            assert too_many.json()["detail"]["kind"] == "capacity"

            overlap = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "start_at": (start + timedelta(minutes=30)).isoformat(),
                    "end_at": (end + timedelta(minutes=30)).isoformat(),
                    "participant_ids": [student_three],
                },
            )
            assert overlap.status_code == 409
            assert any(item["kind"] == "teacher" for item in overlap.json()["detail"]["conflicts"])

            adjacent = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "start_at": end.isoformat(),
                    "end_at": (end + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student_three],
                },
            )
            assert adjacent.status_code == 201, adjacent.text

            for premature_status in ("present", "late"):
                premature = await client.put(
                    f"/api/v1/learning/lessons/{lesson_id}/participants/{student_one}/attendance",
                    headers=headers,
                    json={"status": premature_status},
                )
                assert premature.status_code == 409
            before_start = await client.get(
                f"/api/v1/learning/lesson/{lesson_id}", headers=headers
            )
            assert next(
                item
                for item in before_start.json()["participants"]
                if item["person_id"] == student_one
            )["attendance_status"] == "expected"

            started = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/start", headers=headers
            )
            assert started.status_code == 200
            assert started.json()["warnings"][0]["kind"] == "missing"

            arrived = await client.post(
                f"/api/v1/learning/presence/{student_one}/arrival", headers=headers
            )
            assert arrived.status_code == 201
            departed = await client.post(
                f"/api/v1/learning/presence/{student_one}/departure", headers=headers
            )
            assert departed.status_code == 200
            assert departed.json()["warnings"][0]["kind"] == "active_lesson"

            finished = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/finish", headers=headers
            )
            assert finished.status_code == 200
            statuses = {
                p["person_id"]: p["attendance_status"] for p in finished.json()["participants"]
            }
            assert statuses[student_one] == "left_early"
            assert statuses[student_two] == "absent"

            corrected = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student_two}/correct",
                headers=headers,
                json={
                    "attendance_status": "excused",
                    "reason": "Подтверждённая уважительная причина",
                },
            )
            assert corrected.status_code == 200, corrected.text
            assert corrected.json()["attendance_status"] == "excused"

            corrected_present = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/participants/{student_two}/correct",
                headers=headers,
                json={
                    "attendance_status": "present",
                    "reason": "Посещаемость внесена задним числом",
                },
            )
            assert corrected_present.status_code == 200, corrected_present.text
            assert corrected_present.json()["attendance_status"] == "present"
            assert datetime.fromisoformat(
                corrected_present.json()["arrived_at"]
            ).replace(tzinfo=UTC) == datetime.fromisoformat(
                finished.json()["actual_start_at"]
            ).replace(tzinfo=UTC)
            assert datetime.fromisoformat(corrected_present.json()["left_at"]).replace(
                tzinfo=UTC
            ) == datetime.fromisoformat(finished.json()["actual_end_at"]).replace(
                tzinfo=UTC
            )

            corrected_time = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/correct-time",
                headers=headers,
                json={
                    "actual_start_at": (start + timedelta(minutes=3)).isoformat(),
                    "actual_end_at": (end + timedelta(minutes=2)).isoformat(),
                    "reason": "Уточнение по журналу администратора",
                },
            )
            assert corrected_time.status_code == 200, corrected_time.text
            assert corrected_time.json()["actual_start_at"] is not None

            history = await client.get(
                f"/api/v1/learning/history/person/{student_one}", headers=headers
            )
            assert history.status_code == 200
            assert history.json()["lessons"][0]["subject_name_snapshot"] == "Робототехника"

            repeated_finish = await client.post(
                f"/api/v1/learning/lessons/{lesson_id}/finish", headers=headers
            )
            assert repeated_finish.status_code == 200
            repeated_departure = await client.post(
                f"/api/v1/learning/presence/{student_one}/departure", headers=headers
            )
            assert repeated_departure.status_code == 200
            assert repeated_departure.json()["already_departed"] is True

            series_start = start + timedelta(days=10)
            series = await client.post(
                "/api/v1/learning/series",
                headers=headers,
                json={
                    "subject_id": subject,
                    "teacher_id": teacher,
                    "room_id": room,
                    "starts_at": series_start.isoformat(),
                    "duration_minutes": 60,
                    "interval_weeks": 1,
                    "occurrences": 3,
                    "participant_ids": [student_three],
                },
            )
            assert series.status_code == 201, series.text
            assert len(series.json()["lesson_ids"]) == 3
            series_id = int(series.json()["id"])
            first_series_lesson, exception_lesson, last_series_lesson = series.json()[
                "lesson_ids"
            ]
            assert (
                await client.post(
                    f"/api/v1/learning/lessons/{first_series_lesson}/start",
                    headers=headers,
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/learning/lessons/{first_series_lesson}/finish",
                    headers=headers,
                )
            ).status_code == 200
            exception_start = series_start + timedelta(weeks=1, days=1)
            exception = await client.put(
                f"/api/v1/learning/lessons/{exception_lesson}",
                headers=headers,
                json={
                    "subject_id": subject,
                    "teacher_id": teacher,
                    "room_id": room,
                    "start_at": exception_start.isoformat(),
                    "end_at": (exception_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student_three],
                },
            )
            assert exception.status_code == 200, exception.text
            assert exception.json()["series_exception"] is True
            series_update = await client.put(
                f"/api/v1/learning/series/{series_id}",
                headers=headers,
                json={
                    "subject_id": subject,
                    "teacher_id": teacher,
                    "room_id": room,
                    "starts_at": series_start.isoformat(),
                    "duration_minutes": 60,
                    "interval_weeks": 1,
                    "occurrences": 3,
                    "participant_ids": [student_three],
                    "scope": "all",
                },
            )
            assert series_update.status_code == 200, series_update.text
            assert series_update.json()["lesson_ids"] == [last_series_lesson]
            first_after = (
                await client.get(
                    f"/api/v1/learning/lesson/{first_series_lesson}", headers=headers
                )
            ).json()
            exception_after = (
                await client.get(
                    f"/api/v1/learning/lesson/{exception_lesson}", headers=headers
                )
            ).json()
            last_after = (
                await client.get(
                    f"/api/v1/learning/lesson/{last_series_lesson}", headers=headers
                )
            ).json()
            assert first_after["status"] == "completed"
            assert datetime.fromisoformat(exception_after["start_at"]).replace(
                tzinfo=UTC
            ) == exception_start
            assert datetime.fromisoformat(last_after["start_at"]).replace(
                tzinfo=UTC
            ) == series_start + timedelta(weeks=2)

            dual_role = await person(
                "Сергеев Сергей Петрович",
                "+79000000005",
                ["student", "teacher"],
            )
            subject_update = await client.put(
                f"/api/v1/learning/subjects/{subject}",
                headers=headers,
                json={
                    "name": "Робототехника",
                    "color": "#2563eb",
                    "teacher_ids": [teacher, dual_role],
                },
            )
            assert subject_update.status_code == 200, subject_update.text
            room_two = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": "Кабинет 2", "capacity": 5},
                )
            ).json()["id"]
            cross_start = start + timedelta(days=30)
            teaches = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": dual_role,
                    "start_at": cross_start.isoformat(),
                    "end_at": (cross_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [student_three],
                },
            )
            assert teaches.status_code == 201, teaches.text
            cannot_be_student = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": teacher,
                    "room_id": room_two,
                    "start_at": (cross_start + timedelta(minutes=30)).isoformat(),
                    "end_at": (cross_start + timedelta(minutes=90)).isoformat(),
                    "participant_ids": [dual_role],
                },
            )
            assert cannot_be_student.status_code == 409

            reverse_start = cross_start + timedelta(days=1)
            studies = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": teacher,
                    "start_at": reverse_start.isoformat(),
                    "end_at": (reverse_start + timedelta(hours=1)).isoformat(),
                    "participant_ids": [dual_role],
                },
            )
            assert studies.status_code == 201, studies.text
            cannot_teach = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": dual_role,
                    "room_id": room_two,
                    "start_at": (reverse_start + timedelta(minutes=30)).isoformat(),
                    "end_at": (reverse_start + timedelta(minutes=90)).isoformat(),
                    "participant_ids": [student_three],
                },
            )
            assert cannot_teach.status_code == 409

            unrelated_teacher = await person(
                "Учитель Другого Предмета",
                "+79000000006",
                ["teacher"],
            )
            not_qualified = await client.post(
                "/api/v1/learning/lessons",
                headers=headers,
                json={
                    **base,
                    "teacher_id": unrelated_teacher,
                    "room_id": room_two,
                    "start_at": (reverse_start + timedelta(days=2)).isoformat(),
                    "end_at": (reverse_start + timedelta(days=2, hours=1)).isoformat(),
                    "participant_ids": [],
                },
            )
            assert not_qualified.status_code == 422
            assert "не закреплён" in not_qualified.text

            archived = await client.post(f"/api/v1/people/{student_one}/archive", headers=headers)
            assert archived.status_code == 200
            archived_history = await client.get(
                f"/api/v1/learning/history/person/{student_one}", headers=headers
            )
            assert archived_history.status_code == 200
            cannot_delete_history = await client.delete(
                f"/api/v1/people/{student_one}", headers=headers
            )
            assert cannot_delete_history.status_code == 409

            deleted_group = await client.delete(
                f"/api/v1/learning/groups/{group_id}", headers=headers
            )
            assert deleted_group.status_code == 200, deleted_group.text
            assert deleted_group.json() == {"id": group_id, "deleted": True}
            references_after_delete = await client.get(
                "/api/v1/learning/reference-data", headers=headers
            )
            assert all(
                item["id"] != group_id
                for item in references_after_delete.json()["groups"]
            )
            lesson_after_group_delete = await client.get(
                f"/api/v1/learning/lesson/{excluded_group_member.json()['id']}",
                headers=headers,
            )
            assert lesson_after_group_delete.json()["group_id"] is None
