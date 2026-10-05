from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.system_status import SystemStatusProvider
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_status_provider_caches_snapshot_and_isolates_collector_failures(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    calls = {"database": 0, "worker": 0}

    async def database() -> dict:
        calls["database"] += 1
        return {
            "available": True,
            "version": "PostgreSQL 16",
            "revision": "revision-7",
            "active_connections": 3,
            "databases": [{"name": "krit_bot", "size_bytes": 1234}],
        }

    async def worker() -> dict:
        calls["worker"] += 1
        raise RuntimeError("external response with a token")

    monkeypatch.setattr(
        SystemStatusProvider,
        "_memory_values",
        staticmethod(lambda: (1000, 600, 400)),
    )
    monkeypatch.setattr(
        SystemStatusProvider,
        "_system_uptime_seconds",
        staticmethod(lambda: 5000),
    )
    monkeypatch.setattr(
        "krit_bot.system_status.shutil.disk_usage",
        lambda _path: (2000, 1500, 500),
    )

    provider = SystemStatusProvider(
        data_root=tmp_path,
        database_collector=database,
        worker_collector=worker,
        queue_collector=lambda: _async_value(
            {"pending": 2, "processing": 1, "failed": 0}
        ),
        backup_collector=lambda: _async_value(
            {
                "last_backup_at": None,
                "last_result": "not_started",
                "trusted_count": 0,
                "suspicious_count": 0,
                "safety_set_pending": False,
                "free_bytes": 500,
            }
        ),
        now=lambda: now,
    )

    first = await provider.snapshot()
    second = await provider.snapshot()
    assert first == second
    assert calls == {"database": 1, "worker": 1}
    assert first["server"]["system_uptime_seconds"] == 5000
    assert first["resources"]["memory"]["available_bytes"] == 400
    assert first["resources"]["disk"]["free_bytes"] == 500
    assert first["database"]["revision"] == "revision-7"
    assert first["bot"] == {
        "available": False,
        "error": "unavailable",
    }
    assert "token" not in repr(first)

    now += timedelta(seconds=11)
    await provider.snapshot()
    assert calls == {"database": 2, "worker": 2}


async def _async_value(value: dict) -> dict:
    return value


@pytest.mark.asyncio
async def test_system_status_endpoint_is_superadmin_only(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'status.db').as_posix()}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    snapshots = 0

    async def status_snapshot() -> dict:
        nonlocal snapshots
        snapshots += 1
        return {
            "collected_at": "2026-10-05T12:00:00+00:00",
            "server": {},
            "resources": {},
            "database": {},
            "api": {},
            "bot": {},
            "queues": {},
            "backups": {},
        }

    app = create_app(settings, status_snapshot=status_snapshot)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            admin_login = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin"},
                )
            ).json()
            admin_headers = {
                "Authorization": f"Bearer {admin_login['access_token']}"
            }
            created = await client.post(
                "/api/v1/administration/users",
                headers=admin_headers,
                json={
                    "full_name": "Директор Статуса",
                    "username": "director",
                    "role": "director",
                    "password": "1234567",
                },
            )
            assert created.status_code == 201
            director_login = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "director", "password": "1234567"},
                )
            ).json()
            director_headers = {
                "Authorization": f"Bearer {director_login['access_token']}"
            }

            forbidden = await client.get(
                "/api/v1/administration/system-status", headers=director_headers
            )
            assert forbidden.status_code == 403
            assert snapshots == 0

            response = await client.get(
                "/api/v1/administration/system-status", headers=admin_headers
            )
            assert response.status_code == 200
            assert set(response.json()) == {
                "collected_at",
                "server",
                "resources",
                "database",
                "api",
                "bot",
                "queues",
                "backups",
            }
            assert snapshots == 1
