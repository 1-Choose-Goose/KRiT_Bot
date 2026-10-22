from __future__ import annotations

import os
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from krit_bot.config import Settings
from krit_bot.db import Person, PersonRole, build_engine, build_session_factory
from krit_bot.learning_models import (
    Lesson,
    LessonParticipant,
    PersonMaxIdentity,
    Room,
    Subject,
    SubjectTeacher,
)
from krit_bot.max_api import MaxApiClient
from krit_bot.online import (
    cleanup_online_room,
    cleanup_orphan_online_files,
    reset_online_presence,
)
from krit_bot.online_models import OnlineLessonParticipant
from krit_bot.webhook import create_app


async def _create_person(
    sessions,
    *,
    name: str,
    phone: str,
    max_user_id: int,
    role: str,
) -> int:
    async with sessions() as session:
        person = Person(
            full_name=name,
            phone=phone,
            max_auth_phone=phone,
            active=True,
            bot_access_enabled=True,
        )
        session.add(person)
        await session.flush()
        session.add(PersonRole(person_id=person.id, role=role))
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=phone,
                max_user_id=max_user_id,
                verified_at=datetime.now(UTC),
                channel_subscription_status="subscribed",
            )
        )
        await session.commit()
        return person.id


async def _login(client: httpx.AsyncClient, sent: dict[int, str], phone: str, user_id: int):
    requested = await client.post(
        "/api/v1/online/auth/challenges",
        json={"phone": phone},
        headers={"X-Forwarded-For": f"198.51.100.{user_id % 200 + 1}"},
    )
    assert requested.status_code == 202, requested.text
    code = re.search(r"\b(\d{6})\b", sent[user_id]).group(1)
    verified = await client.post(
        "/api/v1/online/auth/challenges/verify",
        json={"challenge_id": requested.json()["challenge_id"], "code": code},
    )
    assert verified.status_code == 200, verified.text
    client.headers["X-Online-CSRF"] = verified.json()["csrf_token"]
    return verified


@pytest.mark.asyncio
async def test_online_auth_uses_verified_max_identity_and_one_time_code(
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'online-auth.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
        online_cookie_secure=False,
        online_upload_root=tmp_path / "assets",
        online_cleanup_interval_seconds=60,
    )
    sent: dict[int, str] = {}

    async def fake_send_text(self, *, user_id: int, text: str, attachments=None):
        sent[user_id] = text
        return {"message": {"body": {"mid": "test"}}}

    monkeypatch.setattr(MaxApiClient, "send_text", fake_send_text)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = build_engine(database_url)
        sessions = build_session_factory(engine)
        await _create_person(
            sessions,
            name="Иван Петров",
            phone="+79991234567",
            max_user_id=5001,
            role="student",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            requested = await client.post(
                "/api/v1/online/auth/challenges", json={"phone": "8 (999) 123-45-67"}
            )
            assert requested.status_code == 202
            assert requested.json()["expires_in"] == 300
            assert "КРиТ — вход в онлайн-занятие" in sent[5001]
            code = re.search(r"\b(\d{6})\b", sent[5001]).group(1)

            wrong = await client.post(
                "/api/v1/online/auth/challenges/verify",
                json={"challenge_id": requested.json()["challenge_id"], "code": "000000"},
            )
            assert wrong.status_code == 401

            verified = await client.post(
                "/api/v1/online/auth/challenges/verify",
                json={"challenge_id": requested.json()["challenge_id"], "code": code},
            )
            assert verified.status_code == 200
            assert verified.cookies.get("krit_online_session")

            me = await client.get("/api/v1/online/auth/me")
            assert me.status_code == 200
            assert me.json()["person"]["full_name"] == "Иван Петров"
            assert me.json()["roles"] == ["student"]
            assert me.json()["csrf_token"] != verified.json()["csrf_token"]
            assert me.headers["cache-control"] == "no-store"

            replay = await client.post(
                "/api/v1/online/auth/challenges/verify",
                json={"challenge_id": requested.json()["challenge_id"], "code": code},
            )
            assert replay.status_code == 401

            unknown = await client.post(
                "/api/v1/online/auth/challenges", json={"phone": "+79990000000"}
            )
            assert unknown.status_code == 202
            assert set(unknown.json()) == set(requested.json())
        await engine.dispose()


@pytest.mark.asyncio
async def test_teacher_controls_server_enforced_board_permission(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'online-room.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
        online_cookie_secure=False,
        online_upload_root=tmp_path / "assets",
        online_cleanup_interval_seconds=60,
    )
    sent: dict[int, str] = {}

    async def fake_send_text(self, *, user_id: int, text: str, attachments=None):
        sent[user_id] = text
        return {"message": {"body": {"mid": "test"}}}

    monkeypatch.setattr(MaxApiClient, "send_text", fake_send_text)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = build_engine(database_url)
        sessions = build_session_factory(engine)
        teacher_id = await _create_person(
            sessions,
            name="Мария Иванова",
            phone="+79000000001",
            max_user_id=6001,
            role="teacher",
        )
        student_id = await _create_person(
            sessions,
            name="Иван Петров",
            phone="+79000000002",
            max_user_id=6002,
            role="student",
        )
        await _create_person(
            sessions,
            name="Чужой Ученик",
            phone="+79000000003",
            max_user_id=6003,
            role="student",
        )
        async with sessions() as session:
            subject = Subject(name="Математика", color="#0369a1", active=True)
            room = Room(name="Кабинет 1", capacity=30, active=True)
            session.add_all([subject, room])
            await session.flush()
            session.add(SubjectTeacher(subject_id=subject.id, teacher_id=teacher_id))
            start = datetime.now(UTC) - timedelta(minutes=5)
            lesson = Lesson(
                subject_id=subject.id,
                teacher_id=teacher_id,
                room_id=None,
                delivery_mode="online",
                start_at=start,
                end_at=start + timedelta(hours=1),
                status="scheduled",
                teacher_name_snapshot="Мария Иванова",
                room_name_snapshot="Кабинет 1",
                subject_name_snapshot="Математика",
            )
            session.add(lesson)
            await session.flush()
            session.add(
                LessonParticipant(
                    lesson_id=lesson.id,
                    person_id=student_id,
                    person_name_snapshot="Иван Петров",
                )
            )
            await session.commit()
            lesson_id = lesson.id
            offline_lesson = Lesson(
                subject_id=subject.id,
                teacher_id=teacher_id,
                room_id=room.id,
                delivery_mode="offline",
                start_at=start + timedelta(hours=2),
                end_at=start + timedelta(hours=3),
                status="planned",
                teacher_name_snapshot="Мария Иванова",
                room_name_snapshot="Кабинет 1",
                subject_name_snapshot="Математика",
            )
            session.add(offline_lesson)
            await session.commit()
            offline_lesson_id = offline_lesson.id

        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as teacher_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as student_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as outsider_client,
        ):
            await _login(teacher_client, sent, "+79000000001", 6001)
            await _login(student_client, sent, "+79000000002", 6002)
            await _login(outsider_client, sent, "+79000000003", 6003)

            teacher_lessons = await teacher_client.get("/api/v1/online/lessons")
            student_lessons = await student_client.get("/api/v1/online/lessons")
            assert teacher_lessons.json()["data"][0]["role"] == "teacher"
            assert student_lessons.json()["data"][0]["role"] == "student"
            assert teacher_lessons.json()["data"][0]["delivery_mode"] == "online"
            assert [item["id"] for item in teacher_lessons.json()["data"]] == [lesson_id]
            unavailable = await teacher_client.post(
                f"/api/v1/online/lessons/{offline_lesson_id}/room"
            )
            assert unavailable.status_code == 409

            created = await teacher_client.post(f"/api/v1/online/lessons/{lesson_id}/room")
            assert created.status_code == 201, created.text
            room_id = created.json()["id"]

            async with sessions() as session:
                session.add(
                    OnlineLessonParticipant(
                        online_session_id=room_id,
                        person_id=student_id,
                    )
                )
                await session.commit()
            assert await reset_online_presence(sessions) == 1
            async with sessions() as session:
                stale_presence = await session.scalar(
                    select(OnlineLessonParticipant).where(
                        OnlineLessonParticipant.online_session_id == room_id,
                        OnlineLessonParticipant.person_id == student_id,
                    )
                )
                assert stale_presence is not None
                assert stale_presence.left_at is not None

            visible = await student_client.get(f"/api/v1/online/rooms/{room_id}")
            assert visible.status_code == 200
            hidden = await outsider_client.get(f"/api/v1/online/rooms/{room_id}")
            assert hidden.status_code == 404

            blocked = await student_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "student-op-1",
                    "page_id": "main",
                    "kind": "add",
                    "payload": {"object": {"id": "stroke-1", "type": "path"}},
                },
            )
            assert blocked.status_code == 403

            started = await teacher_client.put(
                f"/api/v1/online/rooms/{room_id}/status", json={"status": "active"}
            )
            assert started.status_code == 200
            grant = await teacher_client.put(
                f"/api/v1/online/rooms/{room_id}/permissions/{student_id}",
                json={"can_draw": True},
            )
            assert grant.status_code == 200
            allowed = await student_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "student-op-1",
                    "page_id": "main",
                    "kind": "add",
                    "payload": {"object": {"id": "stroke-1", "type": "path"}},
                },
            )
            assert allowed.status_code == 201, allowed.text
            student_page = await student_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "student-page-1",
                    "page_id": "student-page",
                    "kind": "page_add",
                    "payload": {"page": {"kind": "blank"}},
                },
            )
            assert student_page.status_code == 403
            teacher_page = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "teacher-page-1",
                    "page_id": "teacher-page",
                    "kind": "page_add",
                    "payload": {"page": {"kind": "blank", "name": "Чистая доска"}},
                },
            )
            assert teacher_page.status_code == 201

            revoke = await teacher_client.put(
                f"/api/v1/online/rooms/{room_id}/permissions/{student_id}",
                json={"can_draw": False},
            )
            assert revoke.status_code == 200
            rejected = await student_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "student-op-2",
                    "page_id": "main",
                    "kind": "add",
                    "payload": {"object": {"id": "stroke-2", "type": "path"}},
                },
            )
            assert rejected.status_code == 403

            invited = await teacher_client.put(
                f"/api/v1/online/rooms/{room_id}/permissions/{student_id}",
                json={"can_draw": True, "invite": True},
            )
            assert invited.json() == {
                "type": "permission.changed",
                "person_id": student_id,
                "can_draw": False,
                "invited": True,
            }
            accepted = await student_client.put(
                f"/api/v1/online/rooms/{room_id}/board-invitation/accept"
            )
            assert accepted.json()["can_draw"] is True

            duplicate = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "teacher-op-1",
                    "page_id": "main",
                    "kind": "add",
                    "payload": {"object": {"id": "teacher-stroke", "type": "path"}},
                },
            )
            assert duplicate.status_code == 201
            replay = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/board/operations",
                json={
                    "operation_id": "teacher-op-1",
                    "page_id": "main",
                    "kind": "add",
                    "payload": {"object": {"id": "teacher-stroke", "type": "path"}},
                },
            )
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True

            original_disk_usage = shutil.disk_usage
            monkeypatch.setattr(
                "krit_bot.online.shutil.disk_usage", lambda _path: SimpleNamespace(free=0)
            )
            no_space = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/assets",
                files={"upload": ("blocked.txt", b"data", "text/plain")},
            )
            assert no_space.status_code == 507
            monkeypatch.setattr("krit_bot.online.shutil.disk_usage", original_disk_usage)

            invalid_file = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/assets",
                files={"upload": ("broken.png", b"not-a-png", "image/png")},
            )
            assert invalid_file.status_code == 415
            assert not (tmp_path / "assets" / str(room_id)).exists()

            uploaded = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/assets",
                files={"upload": ("task.txt", b"3x + 7 = 22", "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            downloaded = await student_client.get(
                "/api/v1/online" + uploaded.json()["download_url"]
            )
            assert downloaded.status_code == 200
            assert downloaded.content == b"3x + 7 = 22"
            assert downloaded.headers["x-content-type-options"] == "nosniff"

            assignment = await teacher_client.post(
                f"/api/v1/online/rooms/{room_id}/assignments",
                json={
                    "kind": "homework",
                    "title": "Задача 1",
                    "instructions": "Решите уравнение",
                    "recipient_ids": [student_id],
                },
            )
            assert assignment.status_code == 201, assignment.text
            assignment_id = assignment.json()["id"]
            student_tasks = await student_client.get(f"/api/v1/online/rooms/{room_id}/assignments")
            assert [item["id"] for item in student_tasks.json()] == [assignment_id]
            submitted = await student_client.put(
                f"/api/v1/online/assignments/{assignment_id}/submission",
                json={
                    "answer_text": "x = 4",
                    "board_payload": {"page_id": "main", "objects": []},
                    "submit": True,
                },
            )
            assert submitted.json()["status"] == "submitted"
            student_task = (
                await student_client.get(f"/api/v1/online/rooms/{room_id}/assignments")
            ).json()[0]
            assert student_task["status"] == "submitted"
            assert student_task["submission"]["answer_text"] == "x = 4"
            teacher_task = (
                await teacher_client.get(f"/api/v1/online/rooms/{room_id}/assignments")
            ).json()[0]
            assert teacher_task["submissions"][0]["person"]["id"] == student_id
            assert teacher_task["submissions"][0]["board_payload"]["page_id"] == "main"
            reviewed = await teacher_client.put(
                f"/api/v1/online/assignments/{assignment_id}/submissions/{student_id}/review",
                json={"status": "checked", "teacher_comment": "Верно", "grade": "5"},
            )
            assert reviewed.json()["status"] == "checked"

            completed = await teacher_client.put(
                f"/api/v1/online/rooms/{room_id}/status", json={"status": "completed"}
            )
            assert completed.status_code == 200, completed.text
            assert completed.json()["cleanup"]["files"] == 1
            assert not (tmp_path / "assets" / str(room_id)).exists()
            cleared_board = await student_client.get(f"/api/v1/online/rooms/{room_id}/board")
            assert cleared_board.json()["revision"] == 0
            assert cleared_board.json()["operations"] == []
            assert await cleanup_online_room(sessions, settings, room_id) == {
                "files": 0,
                "records": 0,
            }
            orphan = tmp_path / "assets" / "999" / "interrupted.part"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"partial")
            old = time.time() - 300
            orphan.touch()
            os.utime(orphan, (old, old))
            os.utime(orphan.parent, (old, old))
            assert await cleanup_orphan_online_files(sessions, settings) == 1
            assert not orphan.parent.exists()
        await engine.dispose()
