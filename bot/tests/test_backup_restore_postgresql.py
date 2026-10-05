from __future__ import annotations

import json
import os
import shutil
import zipfile

import pytest
from deploy.krit_restore_helper import restore_operation, run_command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from krit_bot.backups import BackupService

POSTGRES_URL = os.getenv("KRIT_TEST_POSTGRES_URL")


@pytest.mark.skipif(
    not POSTGRES_URL or not all(shutil.which(item) for item in ("pg_dump", "pg_restore")),
    reason="PostgreSQL URL and client tools are required for the backup/restore test",
)
@pytest.mark.asyncio
async def test_postgresql_backup_can_replace_database_and_recover_data(tmp_path) -> None:
    url = make_url(str(POSTGRES_URL))
    assert url.database == "krit_bot", "The destructive CI restore test needs its isolated database"
    engine = create_async_engine(str(POSTGRES_URL))
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS krit_restore_probe"))
        await connection.execute(text("CREATE TABLE krit_restore_probe(value TEXT NOT NULL)"))
        await connection.execute(text("INSERT INTO krit_restore_probe VALUES ('from-backup')"))
    await engine.dispose()

    backup = BackupService(
        database_url=str(POSTGRES_URL),
        database_names=("krit_bot",),
        root=tmp_path / "backups",
    )
    info = await backup.create()
    _metadata, archive_path = backup.get(info["id"]) or (None, None)
    assert archive_path is not None

    engine = create_async_engine(str(POSTGRES_URL))
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE krit_restore_probe"))
    await engine.dispose()

    operation_id = "c" * 32
    operation_dir = tmp_path / "restore" / operation_id
    operation_dir.mkdir(parents=True)
    shutil.copy2(archive_path, operation_dir / "upload.backup")
    (operation_dir / "state.json").write_text(
        json.dumps({"id": operation_id, "phase": "applying"}), encoding="utf-8"
    )

    environment = {
        "KRIT_DATABASE_NAMES": "krit_bot",
        "KRIT_PGHOST": str(url.host or "127.0.0.1"),
        "KRIT_PGPORT": str(url.port or 5432),
        "KRIT_PGUSER": str(url.username or "postgres"),
        "KRIT_PGPASSWORD": str(url.password or ""),
    }

    def runner(argv: list[str], command_environment: dict[str, str]) -> None:
        if argv[0] == "/opt/krit-bot/venv/bin/krit-migrate":
            return
        run_command(argv, command_environment)

    restore_operation(
        tmp_path / "restore",
        operation_id,
        environment=environment,
        runner=runner,
    )

    engine = create_async_engine(str(POSTGRES_URL))
    async with engine.connect() as connection:
        recovered = await connection.scalar(text("SELECT value FROM krit_restore_probe"))
    await engine.dispose()
    assert recovered == "from-backup"

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("krit_bot.dump").startswith(b"PGDMP")
