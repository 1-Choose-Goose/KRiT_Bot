from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.engine import make_url

from .auth import AdminPrincipal
from .db import EXPECTED_ALEMBIC_REVISION

MAGIC = b"KRITDB1\n"
MetadataCollector = Callable[[str], Awaitable[dict[str, Any]]]
StreamRunner = Callable[[str], AsyncIterator[bytes]]
ConflictChecker = Callable[[], bool]
_INSTALLATION_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class BackupExportBusyError(RuntimeError):
    pass


class BackupExportRequest(BaseModel):
    installation_id: str


class BackupExportService:
    def __init__(
        self,
        *,
        database_url: str,
        database_names: tuple[str, ...],
        metadata_collector: MetadataCollector,
        stream_runner: StreamRunner | None = None,
        conflict_checker: ConflictChecker | None = None,
        max_concurrent: int = 2,
        lease_minutes: int = 15,
    ) -> None:
        if not database_names or any(
            not name.replace("_", "").isalnum() for name in database_names
        ):
            raise ValueError("Invalid KRiT database allowlist")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.database_url = database_url
        self.database_names = database_names
        self.metadata_collector = metadata_collector
        self.stream_runner = stream_runner or self._postgres_chunks
        self.conflict_checker = conflict_checker or (lambda: False)
        self.max_concurrent = max_concurrent
        self.lease = timedelta(minutes=lease_minutes)
        self._exports: dict[str, dict[str, Any]] = {}
        self._active_streams = 0

    @property
    def active(self) -> bool:
        return self._active_streams > 0

    def _discard_expired(self, now: datetime) -> None:
        expired = [
            export_id
            for export_id, export in self._exports.items()
            if now - export["created_at_value"] > self.lease
        ]
        for export_id in expired:
            self._exports.pop(export_id, None)

    async def begin(self, *, principal_id: int, installation_id: str) -> dict[str, Any]:
        if self.conflict_checker() or self._active_streams >= self.max_concurrent:
            raise BackupExportBusyError
        if not _INSTALLATION_ID.fullmatch(installation_id):
            raise ValueError("invalid installation id")
        now = datetime.now(UTC)
        self._discard_expired(now)
        databases = []
        for database_name in self.database_names:
            metadata = await self.metadata_collector(database_name)
            databases.append(
                {
                    "name": database_name,
                    "critical_counts": metadata.get("critical_counts", {}),
                    "audit_watermark": metadata.get("audit_watermark"),
                }
            )
        export_id = uuid.uuid4().hex
        stored = {
            "id": export_id,
            "principal_id": principal_id,
            "installation_id": installation_id,
            "created_at": now.isoformat(),
            "created_at_value": now,
            "schema_version": EXPECTED_ALEMBIC_REVISION,
            "databases": databases,
        }
        self._exports[export_id] = stored
        return {key: value for key, value in stored.items() if key != "created_at_value"}

    async def _postgres_chunks(self, database_name: str) -> AsyncIterator[bytes]:
        url = make_url(self.database_url)
        if not url.drivername.startswith("postgresql"):
            raise RuntimeError("Streaming backup export requires PostgreSQL")
        argv = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--host",
            str(url.host or "localhost"),
            "--port",
            str(url.port or 5432),
            "--username",
            str(url.username or "postgres"),
            "--dbname",
            database_name,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            env={**os.environ, "PGPASSWORD": str(url.password or "")},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            while chunk := await process.stdout.read(1024 * 1024):
                yield chunk
            return_code = await process.wait()
            stderr = await stderr_task
            if return_code:
                raise RuntimeError(
                    f"pg_dump failed with exit code {return_code}: "
                    f"{stderr.decode(errors='replace')[:200]}"
                )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def stream_database(
        self, export_id: str, database_name: str, principal_id: int
    ) -> AsyncIterator[bytes]:
        export = self._exports.get(export_id)
        if export is None:
            raise FileNotFoundError(export_id)
        if export["principal_id"] != principal_id:
            raise PermissionError(export_id)
        if database_name not in self.database_names:
            raise KeyError(database_name)
        if self.conflict_checker() or self._active_streams >= self.max_concurrent:
            raise BackupExportBusyError
        self._active_streams += 1
        digest = hashlib.sha256()
        size = 0
        try:
            yield MAGIC
            async for chunk in self.stream_runner(database_name):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                yield len(chunk).to_bytes(4, "big") + chunk
            footer = json.dumps(
                {"complete": True, "sha256": digest.hexdigest(), "size": size},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            yield (0).to_bytes(4, "big") + len(footer).to_bytes(4, "big") + footer
        finally:
            self._active_streams -= 1


def create_backup_export_router(
    require_admin: Callable[..., Any], service: BackupExportService
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/backups/exports")

    async def require_program_user(
        principal: AdminPrincipal = Depends(require_admin),
    ) -> AdminPrincipal:
        if principal.must_change_password or principal.role not in {
            "superadmin",
            "director",
            "administrator",
        }:
            raise HTTPException(status_code=403)
        return principal

    @router.post("", status_code=201)
    async def begin_export(
        payload: BackupExportRequest,
        principal: AdminPrincipal = Depends(require_program_user),
    ) -> dict[str, Any]:
        try:
            return await service.begin(
                principal_id=principal.id,
                installation_id=payload.installation_id,
            )
        except BackupExportBusyError:
            raise HTTPException(
                status_code=409, detail="Экспорт баз уже выполняется"
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/{export_id}/databases/{database_name}")
    async def stream_database(
        export_id: str,
        database_name: str,
        principal: AdminPrincipal = Depends(require_program_user),
    ) -> StreamingResponse:
        export = service._exports.get(export_id)
        if export is None or database_name not in service.database_names:
            raise HTTPException(status_code=404)
        if export["principal_id"] != principal.id:
            raise HTTPException(status_code=403)
        if service.conflict_checker() or service._active_streams >= service.max_concurrent:
            raise HTTPException(status_code=409, detail="Экспорт баз уже выполняется")
        return StreamingResponse(
            service.stream_database(export_id, database_name, principal.id),
            media_type="application/x-krit-backup-stream",
        )

    return router
