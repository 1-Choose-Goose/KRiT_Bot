from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from krit_bot.auth import AdminPrincipal
from krit_bot.backup_exports import (
    BackupExportBusyError,
    BackupExportService,
    create_backup_export_router,
)


async def _metadata(database_name: str) -> dict:
    return {
        "critical_counts": {"persons": 3 if database_name == "krit_bot" else 0},
        "audit_watermark": 12,
    }


def _principal(role: str = "superadmin", *, must_change: bool = False) -> AdminPrincipal:
    return AdminPrincipal(
        id=7,
        username="tester",
        full_name="Тестовый пользователь",
        role=role,
        must_change_password=must_change,
        auth_version=1,
        is_protected=role == "superadmin",
    )


def _decode_stream(body: bytes) -> tuple[bytes, dict]:
    assert body.startswith(b"KRITDB1\n")
    position = len(b"KRITDB1\n")
    data = bytearray()
    while True:
        size = int.from_bytes(body[position : position + 4], "big")
        position += 4
        if size == 0:
            break
        data.extend(body[position : position + size])
        position += size
    footer_size = int.from_bytes(body[position : position + 4], "big")
    position += 4
    footer = json.loads(body[position : position + footer_size])
    assert position + footer_size == len(body)
    return bytes(data), footer


@pytest.mark.asyncio
async def test_export_stream_has_verified_footer_and_no_server_archive(tmp_path) -> None:
    async def stream_runner(database_name: str) -> AsyncIterator[bytes]:
        assert database_name == "krit_bot"
        yield b"PGDMP"
        yield b"database-content"

    service = BackupExportService(
        database_url="postgresql+asyncpg://krit:secret@db:5432/postgres",
        database_names=("krit_bot",),
        metadata_collector=_metadata,
        stream_runner=stream_runner,
    )
    export = await service.begin(principal_id=7, installation_id="computer-001")
    body = b"".join(
        [chunk async for chunk in service.stream_database(export["id"], "krit_bot", 7)]
    )

    data, footer = _decode_stream(body)
    assert data == b"PGDMPdatabase-content"
    assert footer == {
        "complete": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_dump_never_emits_completion_footer() -> None:
    async def stream_runner(_database_name: str) -> AsyncIterator[bytes]:
        yield b"PGDMPpartial"
        raise RuntimeError("pg_dump failed")

    service = BackupExportService(
        database_url="postgresql+asyncpg://krit:secret@db/postgres",
        database_names=("krit_bot",),
        metadata_collector=_metadata,
        stream_runner=stream_runner,
    )
    export = await service.begin(principal_id=7, installation_id="computer-001")
    received = bytearray()

    with pytest.raises(RuntimeError, match="pg_dump failed"):
        async for chunk in service.stream_database(export["id"], "krit_bot", 7):
            received.extend(chunk)

    assert b'"complete":true' not in received


@pytest.mark.asyncio
async def test_export_enforces_owner_allowlist_conflicts_and_parallel_limit() -> None:
    release = asyncio.Event()

    async def stream_runner(_database_name: str) -> AsyncIterator[bytes]:
        yield b"PGDMP"
        await release.wait()
        yield b"done"

    service = BackupExportService(
        database_url="postgresql+asyncpg://krit:secret@db/postgres",
        database_names=("krit_bot",),
        metadata_collector=_metadata,
        stream_runner=stream_runner,
        max_concurrent=1,
    )
    export = await service.begin(principal_id=7, installation_id="computer-001")
    first = service.stream_database(export["id"], "krit_bot", 7).__aiter__()
    assert await anext(first) == b"KRITDB1\n"

    with pytest.raises(BackupExportBusyError):
        await anext(service.stream_database(export["id"], "krit_bot", 7).__aiter__())
    with pytest.raises(PermissionError):
        await anext(service.stream_database(export["id"], "krit_bot", 8).__aiter__())
    with pytest.raises(KeyError):
        await anext(service.stream_database(export["id"], "other", 7).__aiter__())

    release.set()
    assert b"".join([chunk async for chunk in first])
    service.conflict_checker = lambda: True
    with pytest.raises(BackupExportBusyError):
        await service.begin(principal_id=7, installation_id="computer-002")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["superadmin", "director", "administrator"])
async def test_export_api_allows_every_active_program_role(role: str) -> None:
    async def stream_runner(_database_name: str) -> AsyncIterator[bytes]:
        yield b"PGDMPcontent"

    service = BackupExportService(
        database_url="postgresql+asyncpg://krit:secret@db/postgres",
        database_names=("krit_bot",),
        metadata_collector=_metadata,
        stream_runner=stream_runner,
    )

    async def require_admin() -> AdminPrincipal:
        return _principal(role)

    app = FastAPI()
    app.include_router(create_backup_export_router(require_admin, service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/backups/exports", json={"installation_id": "computer-001"}
        )
        assert created.status_code == 201, created.text
        downloaded = await client.get(
            f"/api/v1/backups/exports/{created.json()['id']}/databases/krit_bot"
        )
        assert downloaded.status_code == 200, downloaded.text
        data, footer = _decode_stream(downloaded.content)
        assert data.startswith(b"PGDMP")
        assert footer["complete"] is True


@pytest.mark.asyncio
async def test_export_api_rejects_initial_password_account() -> None:
    async def stream_runner(_database_name: str) -> AsyncIterator[bytes]:
        yield b"PGDMPcontent"

    service = BackupExportService(
        database_url="postgresql+asyncpg://krit:secret@db/postgres",
        database_names=("krit_bot",),
        metadata_collector=_metadata,
        stream_runner=stream_runner,
    )

    async def require_admin() -> AdminPrincipal:
        return _principal("administrator", must_change=True)

    app = FastAPI()
    app.include_router(create_backup_export_router(require_admin, service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/backups/exports", json={"installation_id": "computer-001"}
        )
    assert response.status_code == 403
