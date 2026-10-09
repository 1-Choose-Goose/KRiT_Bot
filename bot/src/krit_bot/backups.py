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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url

from .auth import AdminPrincipal
from .db import EXPECTED_ALEMBIC_REVISION

SubprocessRunner = Callable[[list[str], dict[str, str]], Awaitable[None]]
MetadataCollector = Callable[[str], Awaitable[dict[str, Any]]]
ConflictChecker = Callable[[], bool]
log = structlog.get_logger()


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

    async def create(
        self,
        *,
        kind: str = "manual",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        if kind not in {"manual", "automatic"}:
            raise ValueError("Invalid backup kind")
        if self.lock.locked() or self.conflict_checker():
            raise BackupBusyError
        async with self.lock:
            if self.conflict_checker():
                raise BackupBusyError
            self.root.mkdir(parents=True, exist_ok=True)
            backup_id = uuid.uuid4().hex
            created_at = created_at or datetime.now(UTC)
            final = self.root / f"{backup_id}.backup"
            partial = final.with_suffix(".backup.part")
            url = make_url(self.database_url)
            try:
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
                        "kind": kind,
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
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            info = {
                "id": backup_id,
                "kind": kind,
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

    def verify(self, backup_id: str) -> bool:
        found = self.get(backup_id)
        if found is None:
            return False
        metadata, archive_path = found
        try:
            if _sha256(archive_path) != metadata["sha256"]:
                return False
            with zipfile.ZipFile(archive_path) as archive:
                if archive.testzip() is not None:
                    return False
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("id") != backup_id:
                    return False
                for database in metadata.get("databases", []):
                    content = archive.read(str(database["filename"]))
                    if hashlib.sha256(content).hexdigest() != database["sha256"]:
                        return False
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
            return False
        return True

    def automatic_backups(self) -> list[dict[str, Any]]:
        backups: list[dict[str, Any]] = []
        if not self.root.exists():
            return backups
        for metadata_path in self.root.glob("*.json"):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                if data.get("kind") != "automatic" or self.get(metadata_path.stem) is None:
                    continue
                data["_created_at"] = datetime.fromisoformat(data["created_at"])
                backups.append(data)
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(backups, key=lambda item: item["_created_at"], reverse=True)

    def rotate_automatic(
        self,
        *,
        daily_retention: int = 7,
        weekly_retention: int = 4,
    ) -> list[str]:
        backups = self.automatic_backups()
        if not backups:
            return []
        keep: set[str] = {str(backups[0]["id"])}
        daily_seen: set[object] = set()
        weekly_seen: set[tuple[int, int]] = set()
        for item in backups:
            created_at = item["_created_at"]
            day = created_at.date()
            if len(daily_seen) < daily_retention and day not in daily_seen:
                daily_seen.add(day)
                keep.add(str(item["id"]))
            iso = created_at.isocalendar()
            week = (iso.year, iso.week)
            if len(weekly_seen) < weekly_retention and week not in weekly_seen:
                weekly_seen.add(week)
                keep.add(str(item["id"]))
        removed: list[str] = []
        for item in backups:
            backup_id = str(item["id"])
            if backup_id in keep:
                continue
            archive = self.root / f"{backup_id}.backup"
            metadata = self.root / f"{backup_id}.json"
            archive.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            removed.append(backup_id)
        return removed


class BackupScheduler:
    def __init__(
        self,
        service: BackupService,
        *,
        interval_seconds: float = 24 * 60 * 60,
        initial_delay_seconds: float = 5 * 60,
        daily_retention: int = 7,
        weekly_retention: int = 4,
    ) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.daily_retention = daily_retention
        self.weekly_retention = weekly_retention

    def seconds_until_due(self, now: datetime) -> float:
        backups = self.service.automatic_backups()
        if not backups:
            return self.initial_delay_seconds
        last_created = backups[0]["_created_at"]
        due_at = last_created + timedelta(seconds=self.interval_seconds)
        return max(0.0, (due_at - now).total_seconds())

    async def run_once_if_due(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if self.seconds_until_due(current) > 0:
            return False
        info = await self.service.create(kind="automatic", created_at=current)
        if not self.service.verify(str(info["id"])):
            raise RuntimeError("Automatic backup integrity verification failed")
        self.service.rotate_automatic(
            daily_retention=self.daily_retention,
            weekly_retention=self.weekly_retention,
        )
        return True

    async def run(self) -> None:
        while True:
            delay = self.seconds_until_due(datetime.now(UTC))
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.run_once_if_due()
            except asyncio.CancelledError:
                raise
            except BackupBusyError:
                await asyncio.sleep(60)
            except Exception:
                log.exception("automatic_backup_failed")
                await asyncio.sleep(5 * 60)


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
