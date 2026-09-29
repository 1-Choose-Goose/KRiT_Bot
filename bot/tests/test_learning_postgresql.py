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


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="KRIT_TEST_POSTGRES_URL is required for the PostgreSQL concurrency test",
)
@pytest.mark.asyncio
async def test_concurrent_room_booking_allows_only_one_lesson() -> None:
    """Verify the PostgreSQL advisory lock closes the empty-result race."""
    suffix = str(uuid4().int)[-10:]
    settings = Settings(
        database_url=str(POSTGRES_URL),
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
            subject = (
                await client.post(
                    "/api/v1/learning/subjects",
                    headers=headers,
                    json={"name": f"Предмет {suffix}", "color": "#2563eb"},
                )
            ).json()["id"]
            room = (
                await client.post(
                    "/api/v1/learning/rooms",
                    headers=headers,
                    json={"name": f"Кабинет {suffix}", "capacity": 10},
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

            first, second = await asyncio.gather(
                client.post(
                    "/api/v1/learning/lessons",
                    headers=headers,
                    json={**payload, "teacher_id": teacher_one},
                ),
                client.post(
                    "/api/v1/learning/lessons",
                    headers=headers,
                    json={**payload, "teacher_id": teacher_two},
                ),
            )
            assert sorted((first.status_code, second.status_code)) == [201, 409]
