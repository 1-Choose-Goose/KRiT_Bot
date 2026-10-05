from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from deploy.krit_restore_helper import restore_operation
from pydantic import SecretStr

from krit_bot.config import Settings
from krit_bot.restores import RestoreConflict, RestoreService, RestoreValidationError
from krit_bot.webhook import create_app


def _backup_body() -> tuple[dict, bytes]:
    dump = b"PGDMP postgres custom dump"
    manifest = {
        "format": 1,
        "id": "source-backup",
        "created_at": "2026-10-05T12:00:00+00:00",
        "schema_version": "20261004_administration_v7",
        "archive_sha256": None,
        "databases": [
            {
                "name": "krit_bot",
                "filename": "krit_bot.dump",
                "size": len(dump),
                "sha256": hashlib.sha256(dump).hexdigest(),
                "critical_counts": {"persons": 3},
                "audit_watermark": 10,
            }
        ],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("krit_bot.dump", dump)
    body = stream.getvalue()
    return (
        {
            "id": "source-backup",
            "created_at": manifest["created_at"],
            "schema_version": manifest["schema_version"],
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "databases": manifest["databases"],
        },
        body,
    )


@pytest.mark.asyncio
async def test_restore_validates_stream_dispatches_only_operation_id_and_blocks_next(
    tmp_path,
) -> None:
    dispatched: list[str] = []

    async def dispatch(operation_id: str) -> dict:
        dispatched.append(operation_id)
        return {"id": "safety-1", "size": 456}

    service = RestoreService(
        root=tmp_path,
        database_names=("krit_bot",),
        dispatcher=dispatch,
        free_space=lambda: 10_000_000,
    )
    info, body = _backup_body()
    operation = await service.create(info, target_has_business_data=False)
    uploaded = await service.upload(operation["id"], [body[:10], body[10:]])
    assert uploaded["phase"] == "uploaded"
    completed = await service.apply(operation["id"], allow_existing=False)
    assert completed["phase"] == "completed"
    assert dispatched == [operation["id"]]
    assert service.pending_safety()["id"] == "safety-1"
    with pytest.raises(RestoreConflict, match="safety"):
        await service.create(info, target_has_business_data=False)


@pytest.mark.asyncio
async def test_restore_rejects_bad_hash_database_set_space_and_weak_confirmation(tmp_path) -> None:
    async def dispatch(_operation_id: str) -> dict:
        return {"id": "safety"}

    info, body = _backup_body()
    service = RestoreService(
        root=tmp_path,
        database_names=("krit_bot",),
        dispatcher=dispatch,
        free_space=lambda: len(body),
    )
    with pytest.raises(RestoreValidationError, match="space"):
        await service.create(info, target_has_business_data=True)

    service = RestoreService(
        root=tmp_path / "second",
        database_names=("krit_bot",),
        dispatcher=dispatch,
        free_space=lambda: len(body) * 10,
    )
    operation = await service.create(info, target_has_business_data=True)
    with pytest.raises(RestoreValidationError, match="checksum"):
        corrupted = body[:-1] + bytes([body[-1] ^ 1])
        await service.upload(operation["id"], [corrupted])

    operation = await service.create(info, target_has_business_data=True)
    await service.upload(operation["id"], [body])
    with pytest.raises(RestoreConflict, match="confirmation"):
        await service.apply(operation["id"], allow_existing=False)


@pytest.mark.asyncio
async def test_restore_refuses_to_start_while_backup_is_active(tmp_path) -> None:
    async def dispatch(_operation_id: str) -> dict:
        return {"id": "safety"}

    service = RestoreService(
        root=tmp_path,
        database_names=("krit_bot",),
        dispatcher=dispatch,
        free_space=lambda: 10_000_000,
        conflict_checker=lambda: True,
    )
    info, _body = _backup_body()

    with pytest.raises(RestoreConflict, match="backup"):
        await service.create(info, target_has_business_data=False)


@pytest.mark.asyncio
async def test_restore_api_streams_archive_and_requires_safety_decision(tmp_path) -> None:
    dispatched: list[str] = []

    async def dispatch(operation_id: str) -> dict:
        dispatched.append(operation_id)
        return {"id": "safety-api", "size": 42}

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
        backup_root=tmp_path / "backups",
        restore_root=tmp_path / "restore",
    )
    app = create_app(settings, restore_dispatcher=dispatch)
    info, body = _backup_body()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin"},
                )
            ).json()
            headers = {"Authorization": f"Bearer {login['access_token']}"}
            created = await client.post(
                "/api/v1/administration/restores", headers=headers, json=info
            )
            assert created.status_code == 201, created.text
            operation_id = created.json()["id"]
            uploaded = await client.put(
                f"/api/v1/administration/restores/{operation_id}/content",
                headers={**headers, "Content-Type": "application/octet-stream"},
                content=body,
            )
            assert uploaded.status_code == 200, uploaded.text
            applied = await client.post(
                f"/api/v1/administration/restores/{operation_id}/apply",
                headers=headers,
                json={},
            )
            assert applied.status_code == 200, applied.text
            assert dispatched == [operation_id]
            assert (
                await client.post(
                    "/api/v1/administration/restores", headers=headers, json=info
                )
            ).status_code == 409
            deleted = await client.post(
                "/api/v1/administration/restores/safety/delete",
                headers=headers,
                json={"confirmation_phrase": "УДАЛИТЬ"},
            )
            assert deleted.status_code == 200


def test_privileged_helper_safety_dumps_all_databases_and_rolls_back_set(tmp_path) -> None:
    operation_id = "a" * 32
    operation_dir = tmp_path / operation_id
    operation_dir.mkdir()
    info, _body = _backup_body()
    second_dump = b"PGDMP second database"
    manifest = {
        "format": 1,
        "schema_version": info["schema_version"],
        "databases": [
            info["databases"][0],
            {
                "name": "krit_messages",
                "filename": "krit_messages.dump",
                "size": len(second_dump),
                "sha256": hashlib.sha256(second_dump).hexdigest(),
            },
        ],
    }
    first_dump = b"PGDMP postgres custom dump"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("krit_bot.dump", first_dump)
        archive.writestr("krit_messages.dump", second_dump)
    (operation_dir / "upload.backup").write_bytes(stream.getvalue())
    (operation_dir / "state.json").write_text(
        json.dumps({"id": operation_id, "phase": "applying"}), encoding="utf-8"
    )
    commands: list[list[str]] = []

    def runner(argv: list[str], _environment: dict[str, str]) -> None:
        commands.append(argv)
        if argv[0] == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP safety")

    safety = restore_operation(
        tmp_path,
        operation_id,
        environment={
            "KRIT_DATABASE_NAMES": "krit_bot,krit_messages",
            "KRIT_PGPASSWORD": "secret",
        },
        runner=runner,
    )
    assert safety["databases"] == ["krit_bot", "krit_messages"]
    assert len([item for item in commands if item[0] == "pg_dump"]) == 2
    assert len([item for item in commands if item[0] == "pg_restore"]) == 2
    assert all("secret" not in " ".join(item) for item in commands)


def test_privileged_helper_reapplies_every_safety_dump_after_restore_failure(
    tmp_path,
) -> None:
    operation_id = "b" * 32
    operation_dir = tmp_path / operation_id
    operation_dir.mkdir()
    first = b"PGDMP first"
    second = b"PGDMP second"
    manifest = {
        "databases": [
            {"name": "krit_bot", "filename": "krit_bot.dump"},
            {"name": "krit_messages", "filename": "krit_messages.dump"},
        ]
    }
    with zipfile.ZipFile(operation_dir / "upload.backup", "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("krit_bot.dump", first)
        archive.writestr("krit_messages.dump", second)
    (operation_dir / "state.json").write_text(
        json.dumps({"id": operation_id, "phase": "applying"}), encoding="utf-8"
    )
    restored_paths: list[str] = []
    failed_once = False

    def runner(argv: list[str], _environment: dict[str, str]) -> None:
        nonlocal failed_once
        if argv[0] == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP safety")
        if argv[0] == "pg_restore":
            restored_paths.append(argv[-1])
            if "extracted" in argv[-1] and "krit_messages" in argv[-1] and not failed_once:
                failed_once = True
                raise RuntimeError("restore failed")

    with pytest.raises(RuntimeError, match="restore failed"):
        restore_operation(
            tmp_path,
            operation_id,
            environment={"KRIT_DATABASE_NAMES": "krit_bot,krit_messages"},
            runner=runner,
        )
    assert sum("safety-pending" in path for path in restored_paths) == 2
    state = json.loads((operation_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "failed"
