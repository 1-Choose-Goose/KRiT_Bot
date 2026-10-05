from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.webhook import create_app

POSTGRES_URL = os.getenv("KRIT_TEST_POSTGRES_URL")
CI_ADMIN_PASSWORD = "ci-only-strong-password"


async def _admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "Choose_Goose", "password": CI_ADMIN_PASSWORD},
    )
    if login.status_code == 401:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "Choose_Goose", "password": "123"},
        )
    assert login.status_code == 200, login.text
    token = login.json()
    if token["must_change_password"]:
        changed = await client.post(
            "/api/v1/auth/change-initial-password",
            headers={"Authorization": f"Bearer {token['access_token']}"},
            json={"current_password": "123", "new_password": CI_ADMIN_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        token = changed.json()
    return {"Authorization": f"Bearer {token['access_token']}"}


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="KRIT_TEST_POSTGRES_URL is required for the PostgreSQL concurrency test",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_kind", ["room", "teacher", "student"])
async def test_concurrent_booking_allows_only_one_lesson(conflict_kind: str) -> None:
    """Verify PostgreSQL advisory locks close all person/resource empty-result races."""
    suffix = str(uuid4().int)[-10:]
    settings = Settings(
        database_url=str(POSTGRES_URL),
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bootstrap_admin_password=SecretStr("ci-only-strong-password"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _admin_headers(client)

            async def person(name: str, phone: str, roles: list[str]) -> int:
                response = await client.post(
                    "/api/v1/people",
                    headers=headers,
                    json={"full_name": name, "phone": phone, "roles": roles},
                )
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            teacher_one = await person(
                f"Преподаватель Один {suffix}", f"+79{suffix[:9]}", ["teacher"]
            )
            teacher_two = await person(
                f"Преподаватель Два {suffix}", f"+78{suffix[:9]}", ["teacher"]
            )
            student = await person(f"Ученик Один {suffix}", f"+77{suffix[:9]}", ["student"])
            subject = (
                await client.post(
                    "/api/v1/learning/subjects",
                    headers=headers,
                    json={
                        "name": f"Предмет {suffix}",
                        "color": "#2563eb",
                        "teacher_ids": [teacher_one, teacher_two],
                    },
                )
            ).json()["id"]
            room = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": f"Кабинет {suffix}", "capacity": 10},
                )
            ).json()["id"]
            room_two = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": f"Кабинет второй {suffix}", "capacity": 10},
                )
            ).json()["id"]
            start = datetime.now(UTC) + timedelta(days=60)
            payload = {
                "subject_id": subject,
                "room_id": room,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "participant_ids": [],
            }

            first_payload = {
                **payload,
                "teacher_id": teacher_one,
                "room_id": room,
                "participant_ids": [student] if conflict_kind == "student" else [],
            }
            second_payload = {
                **payload,
                "teacher_id": teacher_one if conflict_kind == "teacher" else teacher_two,
                "room_id": room if conflict_kind == "room" else room_two,
                "participant_ids": [student] if conflict_kind == "student" else [],
            }
            first, second = await asyncio.gather(
                client.post(
                    "/api/v1/learning/lessons",
                    headers=headers,
                    json=first_payload,
                ),
                client.post(
                    "/api/v1/learning/lessons",
                    headers=headers,
                    json=second_payload,
                ),
            )
            assert sorted((first.status_code, second.status_code)) == [201, 409]


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="KRIT_TEST_POSTGRES_URL is required for the PostgreSQL concurrency test",
)
@pytest.mark.asyncio
async def test_concurrent_arrival_and_group_membership_are_serialized() -> None:
    suffix = str(uuid4().int)[-10:]
    settings = Settings(
        database_url=str(POSTGRES_URL),
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bootstrap_admin_password=SecretStr("ci-only-strong-password"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _admin_headers(client)
            person = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": f"Ученик Конкурентный {suffix}",
                    "phone": f"+76{suffix[:9]}",
                    "roles": ["student"],
                },
            )
            assert person.status_code == 201, person.text
            person_id = int(person.json()["id"])

            arrivals = await asyncio.gather(
                client.post(
                    f"/api/v1/learning/presence/{person_id}/arrival", headers=headers
                ),
                client.post(
                    f"/api/v1/learning/presence/{person_id}/arrival", headers=headers
                ),
            )
            assert [response.status_code for response in arrivals] == [201, 201]
            assert sorted(response.json()["already_present"] for response in arrivals) == [
                False,
                True,
            ]
            today = (await client.get("/api/v1/learning/today", headers=headers)).json()
            assert sum(item["person_id"] == person_id for item in today["present"]) == 1

            group = await client.post(
                "/api/v1/learning/groups",
                headers=headers,
                json={
                    "name": f"Группа конкурентная {suffix}",
                    "default_duration_minutes": 60,
                },
            )
            assert group.status_code == 201, group.text
            group_id = int(group.json()["id"])
            membership_payload = {
                "person_id": person_id,
                "start_at": datetime.now(UTC).isoformat(),
                "end_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            }
            memberships = await asyncio.gather(
                client.post(
                    f"/api/v1/learning/groups/{group_id}/memberships",
                    headers=headers,
                    json=membership_payload,
                ),
                client.post(
                    f"/api/v1/learning/groups/{group_id}/memberships",
                    headers=headers,
                    json=membership_payload,
                ),
            )
            assert sorted(response.status_code for response in memberships) == [201, 409]
