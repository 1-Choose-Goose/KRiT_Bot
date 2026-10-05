import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url

from .auth import AdminPrincipal
from .db import EXPECTED_ALEMBIC_REVISION

SubprocessRunner = Callable[[list[str], dict[str, str]], Awaitable[None]]
MetadataCollector = Callable[[str], Awaitable[dict[str, Any]]]
ConflictChecker = Callable[[], bool]


class BackupBusyError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _run_subprocess(argv: list[str], environment: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, **environment},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(
            f"pg_dump failed with exit code {process.returncode}: "
            f"{stderr.decode(errors='replace')[:200]}"
        )


class BackupService:
    def __init__(
        self,
        *,
        database_url: str,
        database_names: tuple[str, ...],
        root: Path,
        subprocess_runner: SubprocessRunner | None = None,
        metadata_collector: MetadataCollector | None = None,
        conflict_checker: ConflictChecker | None = None,
    ) -> None:
        if not database_names or any(
            not name.replace("_", "").isalnum() for name in database_names
        ):
            raise ValueError("Invalid KRiT database allowlist")
        self.database_url = database_url
        self.database_names = database_names
        self.root = root
        self.subprocess_runner = subprocess_runner or _run_subprocess
        self.metadata_collector = metadata_collector
        self.conflict_checker = conflict_checker or (lambda: False)
        self.lock = asyncio.Lock()

    async def _sqlite_dump(self, source: Path, destination: Path) -> None:
        def copy() -> None:
            with closing(sqlite3.connect(source)) as origin, closing(
                sqlite3.connect(destination)
            ) as target:
                origin.backup(target)

        await asyncio.to_thread(copy)

    async def _postgres_dump(self, database_name: str, destination: Path) -> None:
        url = make_url(self.database_url)
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
            "--file",
            str(destination),
        ]
        await self.subprocess_runner(
            argv,
            {"PGPASSWORD": str(url.password or "")},
        )

    async def create(self) -> dict[str, Any]:
        if self.lock.locked() or self.conflict_checker():
            raise BackupBusyError
        async with self.lock:
            if self.conflict_checker():
                raise BackupBusyError
            self.root.mkdir(parents=True, exist_ok=True)
            backup_id = uuid.uuid4().hex
            created_at = datetime.now(UTC)
            final = self.root / f"{backup_id}.backup"
            partial = final.with_suffix(".backup.part")
            url = make_url(self.database_url)
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                staging = Path(temporary)
                database_files: list[tuple[Path, str]] = []
                if url.drivername.startswith("sqlite"):
                    source = Path(str(url.database))
                    destination = staging / "krit_bot.sqlite"
                    await self._sqlite_dump(source, destination)
                    database_files.append((destination, "krit_bot"))
                else:
                    for database_name in self.database_names:
                        destination = staging / f"{database_name}.dump"
                        await self._postgres_dump(database_name, destination)
                        database_files.append((destination, database_name))
                databases = []
                for path, database_name in database_files:
                    collected = (
                        await self.metadata_collector(database_name)
                        if self.metadata_collector is not None
                        else {}
                    )
                    databases.append({
                        "name": database_name,
                        "filename": path.name,
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                        "critical_counts": collected.get("critical_counts", {}),
                        "audit_watermark": collected.get("audit_watermark"),
                    })
                manifest = {
                    "format": 1,
                    "id": backup_id,
                    "created_at": created_at.isoformat(),
                    "schema_version": EXPECTED_ALEMBIC_REVISION,
                    "archive_sha256": None,
                    "databases": databases,
                }
                with zipfile.ZipFile(
                    partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
                ) as archive:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    )
                    for path, _database_name in database_files:
                        archive.write(path, path.name)
            os.replace(partial, final)
            info = {
                "id": backup_id,
                "size": final.stat().st_size,
                "sha256": _sha256(final),
                "schema_version": EXPECTED_ALEMBIC_REVISION,
                "created_at": created_at.isoformat(),
                "databases": databases,
            }
            metadata_partial = self.root / f"{backup_id}.json.part"
            metadata_partial.write_text(
                json.dumps(info, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            os.replace(metadata_partial, self.root / f"{backup_id}.json")
            return info

    def get(self, backup_id: str) -> tuple[dict[str, Any], Path] | None:
        if len(backup_id) != 32 or any(
            character not in "0123456789abcdef" for character in backup_id
        ):
            return None
        archive = self.root / f"{backup_id}.backup"
        metadata = self.root / f"{backup_id}.json"
        if not archive.is_file() or not metadata.is_file():
            return None
        return json.loads(metadata.read_text(encoding="utf-8")), archive


def create_backup_router(
    require_admin: Callable[..., Any],
    service: BackupService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/administration/backups")

    async def require_superadmin(
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> AdminPrincipal:
        if principal.must_change_password or principal.role != "superadmin":
            raise HTTPException(status_code=403)
        return principal

    @router.post("", status_code=201)
    async def create_backup(
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any]:
        try:
            return await service.create()
        except BackupBusyError:
            raise HTTPException(
                status_code=409, detail="Резервное копирование уже выполняется"
            ) from None
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Не удалось создать резервную копию. Повторите позже.",
            ) from None

    @router.get("/{backup_id}")
    async def download_backup(
        backup_id: str,
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> FileResponse:
        found = service.get(backup_id)
        if found is None:
            raise HTTPException(status_code=404)
        _metadata, path = found
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"KRiT-{backup_id}.backup",
        )

    return router
