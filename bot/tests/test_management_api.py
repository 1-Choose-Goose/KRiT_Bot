from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_login_protects_management_api_and_allows_person_creation(tmp_path) -> None:
    database_path = (tmp_path / "api.db").as_posix()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/status")).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong"},
                )
            ).status_code == 401

            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin"},
            )
            assert login.status_code == 200
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            assert (await client.get("/api/v1/status", headers=headers)).status_code == 200

            created = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Иванов Иван Иванович",
                    "phone": "8 (900) 123-45-67",
                    "roles": ["student"],
                    "active": True,
                },
            )
            assert created.status_code == 201
            assert created.json()["phone"] == "+79001234567"
            assert created.json()["max_user_id"] is None
            assert len((await client.get("/api/v1/people", headers=headers)).json()) == 1
            snapshot = (await client.get("/api/v1/snapshot", headers=headers)).json()
            assert snapshot["status"] == "ok"
            assert len(snapshot["people"]) == 1

            person_id = created.json()["id"]
            parent = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Иванова Анна Петровна",
                    "phone": "+79007654321",
                    "roles": ["parent"],
                    "active": True,
                },
            )
            assert parent.status_code == 201
            parent_id = parent.json()["id"]
            linked = await client.post(
                f"/api/v1/people/{person_id}/guardians/{parent_id}", headers=headers
            )
            assert linked.status_code == 200
            assert linked.json()["guardians"][0]["id"] == parent_id
            people = (await client.get("/api/v1/people", headers=headers)).json()
            parent_view = next(item for item in people if item["id"] == parent_id)
            assert parent_view["students"][0]["id"] == person_id

            archived = await client.post(
                f"/api/v1/people/{person_id}/archive", headers=headers
            )
            assert archived.status_code == 200
            assert archived.json()["archived_at"] is not None
            assert all(
                item["id"] != person_id
                for item in (await client.get("/api/v1/people", headers=headers)).json()
            )
            assert len(
                (await client.get("/api/v1/people-archive", headers=headers)).json()
            ) == 1

            restored = await client.post(
                f"/api/v1/people/{person_id}/restore", headers=headers
            )
            assert restored.status_code == 200
            assert restored.json()["archived_at"] is None
            assert restored.json()["guardians"][0]["id"] == parent_id

            unlinked = await client.delete(
                f"/api/v1/people/{person_id}/guardians/{parent_id}", headers=headers
            )
            assert unlinked.status_code == 200
            assert len((await client.get("/api/v1/people", headers=headers)).json()) == 2

            await client.post(f"/api/v1/people/{person_id}/archive", headers=headers)
            deleted = await client.delete(
                f"/api/v1/people/{person_id}", headers=headers
            )
            assert deleted.status_code == 200
            assert (await client.get("/api/v1/people-archive", headers=headers)).json() == []
