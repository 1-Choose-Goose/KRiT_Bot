import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from krit_bot.backups import BackupBusyError, BackupScheduler, BackupService
from krit_bot.config import Settings
from krit_bot.webhook import create_app


@pytest.mark.asyncio
async def test_sqlite_backup_api_creates_verified_complete_archive(tmp_path) -> None:
    database = tmp_path / "krit.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
        backup_root=tmp_path / "server-backups",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin"},
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            created = await client.post("/api/v1/administration/backups", headers=headers)
            assert created.status_code == 201, created.text
            info = created.json()
            assert info["size"] > 0
            assert len(info["sha256"]) == 64
            assert info["schema_version"]
            assert [item["name"] for item in info["databases"]] == ["krit_bot"]

            downloaded = await client.get(
                f"/api/v1/administration/backups/{info['id']}", headers=headers
            )
            assert downloaded.status_code == 200
            assert len(downloaded.content) == info["size"]
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                assert set(archive.namelist()) == {"manifest.json", "krit_bot.sqlite"}
                manifest = json.loads(archive.read("manifest.json"))
                assert manifest["archive_sha256"] is None
                assert manifest["databases"][0]["name"] == "krit_bot"


@pytest.mark.asyncio
async def test_postgresql_backup_uses_allowlist_and_fixed_pg_dump_arguments(tmp_path) -> None:
    invocations: list[tuple[list[str], dict[str, str]]] = []

    async def runner(argv: list[str], env: dict[str, str]) -> None:
        invocations.append((argv, env))
        output = Path(argv[argv.index("--file") + 1])
        await asyncio.to_thread(output.write_bytes, b"postgres-dump")

    service = BackupService(
        database_url="postgresql+asyncpg://krit:top-secret@db.example:5432/postgres",
        database_names=("krit_bot", "krit_messages"),
        root=tmp_path,
        subprocess_runner=runner,
    )
    info = await service.create()
    assert [item["name"] for item in info["databases"]] == [
        "krit_bot",
        "krit_messages",
    ]
    assert len(invocations) == 2
    assert all("unrelated" not in " ".join(argv) for argv, _env in invocations)
    for argv, env in invocations:
        assert argv[0] == "pg_dump"
        assert "--format=custom" in argv
        assert "top-secret" not in " ".join(argv)
        assert env["PGPASSWORD"] == "top-secret"


@pytest.mark.asyncio
async def test_backup_refuses_to_start_while_restore_is_active(tmp_path) -> None:
    service = BackupService(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'krit.db').as_posix()}",
        database_names=("krit_bot",),
        root=tmp_path / "backups",
        conflict_checker=lambda: True,
    )

    with pytest.raises(BackupBusyError):
        await service.create()


@pytest.mark.asyncio
async def test_automatic_backup_is_verified_and_rotation_keeps_daily_weekly_and_manual(
    tmp_path,
) -> None:
    database = tmp_path / "krit.db"
    database.touch()
    root = tmp_path / "backups"
    service = BackupService(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        database_names=("krit_bot",),
        root=root,
    )
    now = datetime(2026, 10, 9, 4, tzinfo=UTC)
    automatic_ids: list[str] = []
    for days_ago in range(35):
        info = await service.create(kind="automatic", created_at=now - timedelta(days=days_ago))
        automatic_ids.append(info["id"])
    manual = await service.create(kind="manual", created_at=now - timedelta(days=40))

    assert service.verify(automatic_ids[0]) is True
    removed = service.rotate_automatic(daily_retention=7, weekly_retention=4)
    remaining = {
        path.stem for path in root.glob("*.json") if service.get(path.stem) is not None
    }

    assert automatic_ids[0] in remaining
    assert set(automatic_ids[:7]).issubset(remaining)
    assert manual["id"] in remaining
    assert removed
    assert not any(root.glob("*.part"))


@pytest.mark.asyncio
async def test_scheduler_uses_persisted_last_backup_after_restart(tmp_path) -> None:
    database = tmp_path / "krit.db"
    database.touch()
    service = BackupService(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        database_names=("krit_bot",),
        root=tmp_path / "backups",
    )
    now = datetime(2026, 10, 9, 10, tzinfo=UTC)
    await service.create(kind="automatic", created_at=now - timedelta(hours=2))
    scheduler = BackupScheduler(
        service,
        interval_seconds=24 * 60 * 60,
        initial_delay_seconds=0,
    )

    assert await scheduler.run_once_if_due(now=now) is False
    assert await scheduler.run_once_if_due(now=now + timedelta(days=1)) is True


@pytest.mark.asyncio
async def test_failed_backup_removes_partial_archive(tmp_path) -> None:
    async def failing_runner(_argv: list[str], _env: dict[str, str]) -> None:
        raise RuntimeError("dump failed")

    service = BackupService(
        database_url="postgresql+asyncpg://krit:secret@localhost/krit_bot",
        database_names=("krit_bot",),
        root=tmp_path,
        subprocess_runner=failing_runner,
    )

    with pytest.raises(RuntimeError, match="dump failed"):
        await service.create()
    assert not any(tmp_path.glob("*.part"))
