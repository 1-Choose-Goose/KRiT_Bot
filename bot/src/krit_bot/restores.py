import hashlib
import json
import os
import shutil
import uuid
import zipfile
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pwdlib import PasswordHash
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import AdminPrincipal
from .db import AdminUser
from .learning_models import AuditEvent


class RestoreValidationError(ValueError):
    pass


class RestoreConflict(RuntimeError):
    pass


RestoreDispatcher = Callable[[str], Awaitable[dict[str, Any]]]
SafetyRollbackDispatcher = Callable[[], Awaitable[None]]
SafetyDeleteDispatcher = Callable[[], Awaitable[None]]
ConflictChecker = Callable[[], bool]
password_hash = PasswordHash.recommended()


class RestoreService:
    def __init__(
        self,
        *,
        root: Path,
        database_names: tuple[str, ...],
        dispatcher: RestoreDispatcher,
        safety_rollback_dispatcher: SafetyRollbackDispatcher | None = None,
        safety_delete_dispatcher: SafetyDeleteDispatcher | None = None,
        free_space: Callable[[], int] | None = None,
        conflict_checker: ConflictChecker | None = None,
    ) -> None:
        self.root = root
        self.database_names = database_names
        self.dispatcher = dispatcher
        self.safety_rollback_dispatcher = safety_rollback_dispatcher
        self.safety_delete_dispatcher = safety_delete_dispatcher
        self.free_space = free_space or (lambda: shutil.disk_usage(self.root).free)
        self.conflict_checker = conflict_checker or (lambda: False)
        self.safety_path = root / "pending-safety.json"

    def _operation_dir(self, operation_id: str) -> Path:
        if len(operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise RestoreValidationError("invalid operation id")
        return self.root / operation_id

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".part")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _read_state(self, operation_id: str) -> dict[str, Any]:
        path = self._operation_dir(operation_id) / "state.json"
        if not path.is_file():
            raise FileNotFoundError(operation_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def state(self, operation_id: str) -> dict[str, Any]:
        return self._read_state(operation_id)

    def pending_safety(self) -> dict[str, Any] | None:
        if not self.safety_path.is_file():
            return None
        return json.loads(self.safety_path.read_text(encoding="utf-8"))

    def _has_active_operation(self) -> bool:
        if not self.root.is_dir():
            return False
        for state_path in self.root.glob("*/state.json"):
            try:
                phase = json.loads(state_path.read_text(encoding="utf-8")).get("phase")
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if phase in {"created", "uploading", "uploaded", "applying"}:
                return True
        return False

    def has_active_operation(self) -> bool:
        return self._has_active_operation()

    async def create(
        self, metadata: dict[str, Any], *, target_has_business_data: bool
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.pending_safety() is not None:
            raise RestoreConflict("pending safety set must be resolved")
        if self.conflict_checker():
            raise RestoreConflict("a backup operation is active")
        if self._has_active_operation():
            raise RestoreConflict("another restore operation is active")
        names = [str(item.get("name")) for item in metadata.get("databases", [])]
        if names != list(self.database_names):
            raise RestoreValidationError("backup database set does not match allowlist")
        size = int(metadata.get("size") or 0)
        if size <= 0 or len(str(metadata.get("sha256") or "")) != 64:
            raise RestoreValidationError("invalid backup metadata")
        if self.free_space() < size * 2:
            raise RestoreValidationError("insufficient disk space")
        operation_id = uuid.uuid4().hex
        operation_dir = self._operation_dir(operation_id)
        operation_dir.mkdir(mode=0o700)
        state = {
            "id": operation_id,
            "phase": "created",
            "transferred": 0,
            "total": size,
            "error": None,
            "target_has_business_data": target_has_business_data,
            "metadata": metadata,
        }
        self._atomic_json(operation_dir / "state.json", state)
        return state

    async def _chunks(
        self, chunks: Iterable[bytes] | AsyncIterable[bytes]
    ) -> AsyncIterable[bytes]:
        if isinstance(chunks, AsyncIterable):
            async for chunk in chunks:
                yield chunk
        else:
            for chunk in chunks:
                yield chunk

    def _verify_content(self, archive_path: Path, state: dict[str, Any]) -> None:
        metadata = state["metadata"]
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            databases = manifest.get("databases", [])
            if [str(item.get("name")) for item in databases] != list(
                self.database_names
            ):
                raise RestoreValidationError("backup database set does not match allowlist")
            for item in databases:
                filename = str(item.get("filename") or "")
                pure = PurePosixPath(filename)
                if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
                    raise RestoreValidationError("unsafe backup member")
                content = archive.read(filename)
                if len(content) != int(item["size"]):
                    raise RestoreValidationError("database size mismatch")
                if hashlib.sha256(content).hexdigest() != str(item["sha256"]):
                    raise RestoreValidationError("database checksum mismatch")
                if filename.endswith(".dump") and not content.startswith(b"PGDMP"):
                    raise RestoreValidationError("invalid pg_dump custom format")
        if str(manifest.get("schema_version")) != str(metadata["schema_version"]):
            raise RestoreValidationError("schema version mismatch")

    async def upload(
        self,
        operation_id: str,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> dict[str, Any]:
        state = self._read_state(operation_id)
        if state["phase"] != "created":
            raise RestoreConflict("restore is not ready for upload")
        operation_dir = self._operation_dir(operation_id)
        partial = operation_dir / "upload.backup.part"
        destination = operation_dir / "upload.backup"
        digest = hashlib.sha256()
        transferred = 0
        state["phase"] = "uploading"
        self._atomic_json(operation_dir / "state.json", state)
        try:
            with partial.open("wb") as stream:
                async for chunk in self._chunks(chunks):
                    stream.write(chunk)
                    digest.update(chunk)
                    transferred += len(chunk)
                    if transferred > int(state["total"]):
                        raise RestoreValidationError("backup size mismatch")
                stream.flush()
                os.fsync(stream.fileno())
            if transferred != int(state["total"]):
                raise RestoreValidationError("backup size mismatch")
            if digest.hexdigest() != str(state["metadata"]["sha256"]):
                raise RestoreValidationError("backup checksum mismatch")
            self._verify_content(partial, state)
            os.replace(partial, destination)
            state["phase"] = "uploaded"
            state["transferred"] = transferred
            self._atomic_json(operation_dir / "state.json", state)
            return state
        except Exception as exc:
            partial.unlink(missing_ok=True)
            state["phase"] = "failed"
            state["error"] = type(exc).__name__
            state["transferred"] = transferred
            self._atomic_json(operation_dir / "state.json", state)
            raise

    async def apply(
        self,
        operation_id: str,
        *,
        allow_existing: bool,
        protected_admin_password_hash: str,
    ) -> dict[str, Any]:
        state = self._read_state(operation_id)
        if state["phase"] != "uploaded":
            raise RestoreConflict("restore is not ready to apply")
        if state["target_has_business_data"] and not allow_existing:
            raise RestoreConflict("strong confirmation is required for existing data")
        state["phase"] = "applying"
        operation_dir = self._operation_dir(operation_id)
        self._atomic_json(operation_dir / "state.json", state)
        credentials_path = operation_dir / "protected-admin.json"
        self._atomic_json(
            credentials_path,
            {"password_hash": protected_admin_password_hash},
        )
        credentials_path.chmod(0o600)
        asynchronous_handoff = False
        try:
            safety = await self.dispatcher(operation_id)
            if safety.pop("_async", False):
                asynchronous_handoff = True
                return state
            self._atomic_json(self.safety_path, safety)
            state["phase"] = "completed"
            state["safety_set"] = safety
            self._atomic_json(operation_dir / "state.json", state)
            return state
        except Exception as exc:
            state["phase"] = "failed"
            state["error"] = type(exc).__name__
            self._atomic_json(operation_dir / "state.json", state)
            raise
        finally:
            if not asynchronous_handoff:
                credentials_path.unlink(missing_ok=True)

    async def delete_safety(self) -> None:
        if self.pending_safety() is None:
            raise FileNotFoundError("pending safety set")
        if self.safety_delete_dispatcher is not None:
            await self.safety_delete_dispatcher()
            return
        safety_directory = self.root / "safety-pending"
        if safety_directory.is_dir():
            shutil.rmtree(safety_directory)
        self.safety_path.unlink()

    async def rollback_safety(self) -> None:
        if self.pending_safety() is None:
            raise FileNotFoundError("pending safety set")
        if self.safety_rollback_dispatcher is None:
            raise RuntimeError("safety rollback helper is unavailable")
        await self.safety_rollback_dispatcher()


class RestoreApplyPayload(BaseModel):
    password: str = ""
    confirmation_phrase: str = ""


class SafetyDeletePayload(BaseModel):
    confirmation_phrase: str


def create_restore_router(
    sessions: async_sessionmaker[AsyncSession],
    require_admin: Callable[..., Any],
    service: RestoreService,
    target_classifier: Callable[[], Awaitable[bool]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/administration/restores")

    async def require_superadmin(
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> AdminPrincipal:
        if principal.must_change_password or principal.role != "superadmin":
            raise HTTPException(status_code=403)
        return principal

    def translate_restore_error(exc: Exception) -> HTTPException:
        if isinstance(exc, RestoreConflict):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, RestoreValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, FileNotFoundError):
            return HTTPException(status_code=404)
        return HTTPException(status_code=503, detail="Операция восстановления не выполнена")

    @router.post("", status_code=201)
    async def create_restore(
        metadata: dict[str, Any],
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any]:
        try:
            return await service.create(
                metadata,
                target_has_business_data=await target_classifier(),
            )
        except Exception as exc:
            raise translate_restore_error(exc) from None

    @router.put("/{operation_id}/content")
    async def upload_restore(
        operation_id: str,
        request: Request,
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any]:
        try:
            return await service.upload(operation_id, request.stream())
        except Exception as exc:
            raise translate_restore_error(exc) from None

    @router.post("/{operation_id}/apply")
    async def apply_restore(
        operation_id: str,
        payload: RestoreApplyPayload,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any]:
        try:
            state = service.state(operation_id)
        except Exception as exc:
            raise translate_restore_error(exc) from None
        allow_existing = not bool(state["target_has_business_data"])
        async with sessions() as session:
            admin = await session.get(AdminUser, principal.id)
            protected_admin = await session.scalar(
                select(AdminUser)
                .where(AdminUser.is_protected.is_(True))
                .order_by(AdminUser.id)
            )
            preserved_admin = protected_admin or admin
            if not allow_existing:
                password_valid = admin is not None and password_hash.verify(
                    payload.password, admin.password_hash
                )
            else:
                password_valid = True
        if preserved_admin is None:
            raise HTTPException(status_code=503, detail="Защищённая запись не найдена")
        if not allow_existing:
            allow_existing = (
                password_valid and payload.confirmation_phrase == "ВОССТАНОВИТЬ"
            )
        try:
            result = await service.apply(
                operation_id,
                allow_existing=allow_existing,
                protected_admin_password_hash=preserved_admin.password_hash,
            )
        except Exception as exc:
            raise translate_restore_error(exc) from None
        return result

    @router.get("/{operation_id}")
    async def restore_status(
        operation_id: str,
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any]:
        try:
            return service.state(operation_id)
        except Exception as exc:
            raise translate_restore_error(exc) from None

    @router.get("/safety/pending")
    async def pending_safety(
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, Any] | None:
        return service.pending_safety()

    @router.post("/safety/delete")
    async def delete_safety(
        payload: SafetyDeletePayload,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, bool]:
        if payload.confirmation_phrase != "УДАЛИТЬ":
            raise HTTPException(status_code=409, detail="Требуется подтверждение удаления")
        try:
            pending = service.pending_safety()
            await service.delete_safety()
        except Exception as exc:
            raise translate_restore_error(exc) from None
        async with sessions() as session:
            session.add(
                AuditEvent(
                    actor_admin_id=principal.id,
                    action="restore_safety_deleted",
                    entity_type="restore_safety",
                    entity_id=None,
                    details={"safety_id": pending.get("id") if pending else None},
                )
            )
            await session.commit()
        return {"deleted": True}

    @router.post("/safety/rollback")
    async def rollback_safety(
        payload: SafetyDeletePayload,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, bool]:
        if payload.confirmation_phrase != "ВЕРНУТЬ":
            raise HTTPException(status_code=409, detail="Требуется подтверждение отката")
        async with sessions() as session:
            session.add(
                AuditEvent(
                    actor_admin_id=principal.id,
                    action="restore_safety_rollback_requested",
                    entity_type="restore_safety",
                    entity_id=None,
                    details={},
                )
            )
            await session.commit()
        try:
            await service.rollback_safety()
        except Exception as exc:
            raise translate_restore_error(exc) from None
        return {"requested": True}

    return router
