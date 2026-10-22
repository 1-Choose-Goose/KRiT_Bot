import asyncio
import hashlib
import hmac
import json
import secrets
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import structlog
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db import Person, PersonRole, normalize_phone, utcnow
from .learning_models import (
    GroupMembership,
    Lesson,
    LessonParticipant,
    PersonMaxIdentity,
)
from .max_api import MaxApiClient
from .online_models import (
    OnlineAssignment,
    OnlineAssignmentRecipient,
    OnlineAssignmentSubmission,
    OnlineAuditEvent,
    OnlineAuthChallenge,
    OnlineBoardOperation,
    OnlineBoardPermission,
    OnlineBoardSnapshot,
    OnlineChatMessage,
    OnlineLessonAsset,
    OnlineLessonParticipant,
    OnlineLessonSession,
    OnlinePoll,
    OnlinePollResponse,
    OnlineWebSession,
)

log = structlog.get_logger()
OTP_TTL_SECONDS = 300
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_WINDOW_LIMIT = 5
SESSION_COOKIE_PATH = "/"
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ASSET_EXTENSIONS = {".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".txt"}
ASSET_MEDIA_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/png",
    "image/jpeg",
    "image/webp",
    "text/plain",
}


def _valid_asset_signature(suffix: str, header: bytes) -> bool:
    if suffix == ".pdf":
        return header.startswith(b"%PDF-")
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if suffix in {".docx", ".pptx"}:
        return header.startswith(b"PK\x03\x04")
    if suffix == ".txt":
        try:
            header.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return False


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(settings: Settings) -> bytes:
    configured = settings.online_otp_pepper or settings.jwt_secret
    if configured is None:
        raise RuntimeError("ONLINE_OTP_PEPPER or JWT_SECRET must be configured")
    return configured.get_secret_value().encode("utf-8")


def _otp_hash(settings: Settings, challenge_id: str, code: str) -> str:
    digest = hmac.new(_secret(settings), f"{challenge_id}:{code}".encode(), hashlib.sha256)
    return digest.hexdigest()


def _public_person(person: Person) -> dict[str, Any]:
    return {"id": person.id, "full_name": person.full_name}


class ChallengePayload(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class VerifyPayload(BaseModel):
    challenge_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    code: str = Field(pattern=r"^\d{6}$")


class PermissionPayload(BaseModel):
    can_draw: bool
    invite: bool = False


class BoardOperationPayload(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    page_id: str = Field(default="main", min_length=1, max_length=64)
    kind: Literal[
        "add",
        "update",
        "delete",
        "clear",
        "page_add",
        "page_update",
        "page_delete",
        "page_activate",
        "page_reorder",
    ]
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def payload_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > 128_000:
            raise ValueError("operation payload is too large")
        return value


class RoomStatusPayload(BaseModel):
    status: Literal["active", "completed", "archived"]


class DrawingModePayload(BaseModel):
    mode: Literal["teacher_only", "selected", "collaboration", "locked"]


class ChatPayload(BaseModel):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("message is empty")
        return value


class HandPayload(BaseModel):
    raised: bool


class AssignmentPayload(BaseModel):
    kind: Literal["lesson", "homework"] = "lesson"
    title: str = Field(min_length=1, max_length=250)
    instructions: str = Field(min_length=1, max_length=20_000)
    recipient_ids: list[int] = Field(min_length=1, max_length=100)
    due_at: datetime | None = None


class SubmissionPayload(BaseModel):
    answer_text: str | None = Field(default=None, max_length=50_000)
    board_payload: dict[str, Any] | None = None
    submit: bool = True


class ReviewPayload(BaseModel):
    status: Literal["checked", "returned"]
    teacher_comment: str | None = Field(default=None, max_length=20_000)
    grade: str | None = Field(default=None, max_length=32)


class PollPayload(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=2, max_length=10)
    show_results: bool = False

    @field_validator("options")
    @classmethod
    def valid_options(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value]
        if any(not item or len(item) > 250 for item in cleaned):
            raise ValueError("invalid poll option")
        return cleaned


class PollResponsePayload(BaseModel):
    option_index: int = Field(ge=0, le=9)


@dataclass
class OnlinePrincipal:
    web_session: OnlineWebSession
    person: Person
    roles: frozenset[str]


class RoomHub:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = defaultdict(set)

    async def connect(self, room_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[room_id].add(websocket)

    def disconnect(self, room_id: int, websocket: WebSocket) -> None:
        self._connections[room_id].discard(websocket)
        if not self._connections[room_id]:
            self._connections.pop(room_id, None)

    async def broadcast(self, room_id: int, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for websocket in tuple(self._connections.get(room_id, ())):
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(room_id, websocket)

    async def close_room(self, room_id: int) -> None:
        sockets = tuple(self._connections.pop(room_id, ()))
        for websocket in sockets:
            try:
                await websocket.close(code=4000, reason="Занятие завершено")
            except Exception:
                pass


async def _roles(session: AsyncSession, person_id: int) -> frozenset[str]:
    query = select(PersonRole.role).where(PersonRole.person_id == person_id)
    return frozenset((await session.scalars(query)).all())


async def _session_principal(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    raw_token: str | None,
) -> OnlinePrincipal | None:
    if not raw_token:
        return None
    async with sessions() as session:
        item = await session.get(OnlineWebSession, _hash(raw_token))
        if item is None or item.revoked_at is not None or _aware(item.expires_at) <= utcnow():
            return None
        person = await session.get(Person, item.person_id)
        if (
            person is None
            or not person.active
            or not person.bot_access_enabled
            or person.archived_at is not None
        ):
            item.revoked_at = utcnow()
            await session.commit()
            return None
        item.last_seen_at = utcnow()
        roles = await _roles(session, person.id)
        await session.commit()
        return OnlinePrincipal(item, person, roles)


async def _lesson_role(
    session: AsyncSession, person_id: int, lesson: Lesson
) -> Literal["teacher", "student"] | None:
    if lesson.teacher_id == person_id:
        return "teacher"
    participant = await session.scalar(
        select(LessonParticipant.id).where(
            LessonParticipant.lesson_id == lesson.id,
            LessonParticipant.person_id == person_id,
            LessonParticipant.cancelled_at.is_(None),
            LessonParticipant.attendance_status != "excused",
        )
    )
    if participant is not None:
        return "student"
    participant_count = await session.scalar(
        select(func.count(LessonParticipant.id)).where(
            LessonParticipant.lesson_id == lesson.id
        )
    )
    if int(participant_count or 0) > 0:
        return None
    if lesson.group_id is None:
        return None
    at = _aware(lesson.start_at)
    membership = await session.scalar(
        select(GroupMembership.id).where(
            GroupMembership.group_id == lesson.group_id,
            GroupMembership.person_id == person_id,
            GroupMembership.start_at <= at,
            or_(GroupMembership.end_at.is_(None), GroupMembership.end_at > at),
        )
    )
    return "student" if membership is not None else None


async def _room_access(
    session: AsyncSession, room_id: int, principal: OnlinePrincipal
) -> tuple[OnlineLessonSession, Lesson, Literal["teacher", "student"]]:
    room = await session.get(OnlineLessonSession, room_id)
    if room is None:
        raise HTTPException(404, "Онлайн-занятие не найдено")
    lesson = await session.get(Lesson, room.lesson_id)
    if (
        lesson is None
        or lesson.delivery_mode != "online"
        or lesson.status == "cancelled"
    ):
        raise HTTPException(404, "Занятие не найдено")
    role = await _lesson_role(session, principal.person.id, lesson)
    if role is None:
        raise HTTPException(404, "Онлайн-занятие не найдено")
    return room, lesson, role


async def _can_draw(
    session: AsyncSession,
    room: OnlineLessonSession,
    principal: OnlinePrincipal,
    role: str,
) -> bool:
    if role == "teacher":
        return room.status in {"waiting", "active"}
    if room.status != "active" or room.student_drawing_mode in {"teacher_only", "locked"}:
        return False
    if room.student_drawing_mode == "collaboration":
        return True
    permission = await session.scalar(
        select(OnlineBoardPermission).where(
            OnlineBoardPermission.online_session_id == room.id,
            OnlineBoardPermission.person_id == principal.person.id,
        )
    )
    return bool(permission and permission.can_draw and permission.revoked_at is None)


def _ensure_room_open(room: OnlineLessonSession) -> None:
    if room.status not in {"waiting", "active"}:
        raise HTTPException(409, "Занятие завершено, временные данные уже очищаются")


def _operation_view(item: OnlineBoardOperation) -> dict[str, Any]:
    return {
        "operation_id": item.operation_id,
        "revision": item.revision,
        "page_id": item.page_id,
        "kind": item.kind,
        "payload": item.payload,
        "actor_person_id": item.actor_person_id,
        "created_at": item.created_at,
    }


def _apply_operation_to_snapshot(state: dict[str, Any], item: OnlineBoardOperation) -> None:
    pages = state.setdefault("pages", {})
    page_meta = state.setdefault("page_meta", {"main": {"kind": "blank"}})
    page_order = state.setdefault("page_order", ["main"])
    page = pages.setdefault(item.page_id, {})
    payload = item.payload
    object_payload = payload.get("object")
    if item.kind in {"add", "update"} and isinstance(object_payload, dict):
        object_id = object_payload.get("id")
        if isinstance(object_id, str):
            page[object_id] = object_payload
    elif item.kind == "delete" and isinstance(payload.get("object_id"), str):
        page.pop(payload["object_id"], None)
    elif item.kind == "clear":
        page.clear()
    elif item.kind == "page_add":
        pages.setdefault(item.page_id, {})
        metadata = payload.get("page")
        page_meta[item.page_id] = metadata if isinstance(metadata, dict) else {"kind": "blank"}
        if item.page_id not in page_order:
            page_order.append(item.page_id)
    elif item.kind == "page_update" and isinstance(payload.get("page"), dict):
        page_meta[item.page_id] = payload["page"]
    elif item.kind == "page_delete" and item.page_id != "main":
        pages.pop(item.page_id, None)
        page_meta.pop(item.page_id, None)
        if item.page_id in page_order:
            page_order.remove(item.page_id)
    elif item.kind == "page_activate":
        state["active_page_id"] = item.page_id
    elif item.kind == "page_reorder" and isinstance(payload.get("page_order"), list):
        ordered = [page_id for page_id in payload["page_order"] if page_id in pages]
        state["page_order"] = ordered + [page_id for page_id in pages if page_id not in ordered]
    state["revision"] = item.revision


async def _maybe_snapshot(
    session: AsyncSession,
    settings: Settings,
    room: OnlineLessonSession,
) -> None:
    if room.board_revision % settings.online_snapshot_every_operations:
        return
    previous = await session.scalar(
        select(OnlineBoardSnapshot)
        .where(OnlineBoardSnapshot.online_session_id == room.id)
        .order_by(OnlineBoardSnapshot.revision.desc())
        .limit(1)
    )
    state: dict[str, Any] = (
        json.loads(json.dumps(previous.state))
        if previous is not None
        else {
            "revision": 0,
            "active_page_id": "main",
            "pages": {"main": {}},
            "page_meta": {"main": {"kind": "blank"}},
            "page_order": ["main"],
        }
    )
    from_revision = previous.revision if previous is not None else 0
    operations = list(
        (
            await session.scalars(
                select(OnlineBoardOperation)
                .where(
                    OnlineBoardOperation.online_session_id == room.id,
                    OnlineBoardOperation.revision > from_revision,
                    OnlineBoardOperation.revision <= room.board_revision,
                )
                .order_by(OnlineBoardOperation.revision)
            )
        ).all()
    )
    for operation in operations:
        _apply_operation_to_snapshot(state, operation)
    session.add(
        OnlineBoardSnapshot(
            online_session_id=room.id,
            revision=room.board_revision,
            state=state,
        )
    )
    prune_before = room.board_revision - settings.online_operation_retention
    if prune_before > 0:
        await session.execute(
            delete(OnlineBoardOperation).where(
                OnlineBoardOperation.online_session_id == room.id,
                OnlineBoardOperation.revision <= prune_before,
            )
        )
        await session.execute(
            delete(OnlineBoardSnapshot).where(
                OnlineBoardSnapshot.online_session_id == room.id,
                OnlineBoardSnapshot.revision < prune_before,
            )
        )


def _remove_room_directory(root: Path, room_id: int) -> None:
    storage_root = root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    candidate = storage_root / str(room_id)
    if candidate.is_symlink():
        candidate.unlink()
        return
    resolved = candidate.resolve()
    if resolved.parent != storage_root:
        raise RuntimeError("online room storage escaped configured root")
    if resolved.exists():
        shutil.rmtree(resolved)


async def cleanup_online_room(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    room_id: int,
) -> dict[str, int]:
    async with sessions() as session:
        room = await session.get(OnlineLessonSession, room_id)
        if room is None or room.cleaned_at is not None:
            return {"files": 0, "records": 0}
        if room.status not in {"completed", "archived"}:
            raise RuntimeError("active online room cannot be cleaned")
        record_count = 0
        for model in (
            OnlineBoardOperation,
            OnlineBoardSnapshot,
            OnlineBoardPermission,
            OnlineLessonParticipant,
            OnlineChatMessage,
            OnlineAssignment,
            OnlinePoll,
            OnlineLessonAsset,
            OnlineAuditEvent,
        ):
            record_count += int(
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.online_session_id == room_id)
                )
                or 0
            )
        asset_count = int(
            await session.scalar(
                select(func.count())
                .select_from(OnlineLessonAsset)
                .where(OnlineLessonAsset.online_session_id == room_id)
            )
            or 0
        )
    await anyio.to_thread.run_sync(_remove_room_directory, settings.online_upload_root, room_id)
    async with sessions() as session:
        room = await session.scalar(
            select(OnlineLessonSession).where(OnlineLessonSession.id == room_id).with_for_update()
        )
        if room is None or room.cleaned_at is not None:
            return {"files": 0, "records": 0}
        if room.status not in {"completed", "archived"}:
            raise RuntimeError("online room became active during cleanup")
        assignment_ids = select(OnlineAssignment.id).where(
            OnlineAssignment.online_session_id == room_id
        )
        poll_ids = select(OnlinePoll.id).where(OnlinePoll.online_session_id == room_id)
        await session.execute(
            delete(OnlineAssignmentSubmission).where(
                OnlineAssignmentSubmission.assignment_id.in_(assignment_ids)
            )
        )
        await session.execute(
            delete(OnlineAssignmentRecipient).where(
                OnlineAssignmentRecipient.assignment_id.in_(assignment_ids)
            )
        )
        await session.execute(
            delete(OnlinePollResponse).where(OnlinePollResponse.poll_id.in_(poll_ids))
        )
        for model in (
            OnlineBoardOperation,
            OnlineBoardSnapshot,
            OnlineBoardPermission,
            OnlineLessonParticipant,
            OnlineChatMessage,
            OnlineAssignment,
            OnlinePoll,
            OnlineLessonAsset,
            OnlineAuditEvent,
        ):
            await session.execute(delete(model).where(model.online_session_id == room_id))
        room.cleaned_at = utcnow()
        room.board_revision = 0
        room.active_page_id = "main"
        session.add(
            OnlineAuditEvent(
                online_session_id=room.id,
                actor_person_id=None,
                event_type="room.cleaned",
                details={"files": asset_count, "records": record_count},
            )
        )
        await session.commit()
    log.info("online_room_cleaned", room_id=room_id, files=asset_count, records=record_count)
    return {"files": asset_count, "records": record_count}


def _remove_orphan_files(
    root: Path,
    known_files: dict[int, set[str]],
    valid_room_ids: set[int],
    older_than: float,
) -> int:
    storage_root = root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for candidate in storage_root.iterdir():
        if candidate.is_symlink():
            candidate.unlink()
            removed += 1
            continue
        if not candidate.is_dir() or not candidate.name.isdecimal():
            continue
        room_id = int(candidate.name)
        if room_id not in valid_room_ids and candidate.stat().st_mtime < older_than:
            shutil.rmtree(candidate)
            removed += 1
            continue
        for file_path in candidate.iterdir():
            if (
                file_path.is_file()
                and file_path.name not in known_files.get(room_id, set())
                and file_path.stat().st_mtime < older_than
            ):
                file_path.unlink()
                removed += 1
        _remove_empty_directory(candidate)
    return removed


async def cleanup_orphan_online_files(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    async with sessions() as session:
        valid_room_ids = set(
            (
                await session.scalars(
                    select(OnlineLessonSession.id).where(OnlineLessonSession.cleaned_at.is_(None))
                )
            ).all()
        )
        rows = (
            await session.execute(
                select(OnlineLessonAsset.online_session_id, OnlineLessonAsset.stored_name)
            )
        ).all()
    known_files: dict[int, set[str]] = defaultdict(set)
    for room_id, stored_name in rows:
        known_files[room_id].add(stored_name)
    grace_seconds = max(120, settings.online_cleanup_interval_seconds * 2)
    removed = await anyio.to_thread.run_sync(
        _remove_orphan_files,
        settings.online_upload_root,
        known_files,
        valid_room_ids,
        time.time() - grace_seconds,
    )
    if removed:
        log.info("online_orphan_files_cleaned", files=removed)
    return removed


async def reset_online_presence(
    sessions: async_sessionmaker[AsyncSession],
) -> int:
    """Close presence rows left open when the previous process stopped abruptly."""
    async with sessions() as session:
        result = await session.execute(
            update(OnlineLessonParticipant)
            .where(OnlineLessonParticipant.left_at.is_(None))
            .values(left_at=utcnow())
        )
        await session.commit()
    reset_count = int(result.rowcount or 0)
    if reset_count:
        log.info("online_presence_reset", participants=reset_count)
    return reset_count


async def run_online_maintenance(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    while True:
        try:
            cutoff = utcnow() - timedelta(hours=settings.online_abandoned_room_hours)
            async with sessions() as session:
                abandoned = list(
                    (
                        await session.scalars(
                            select(OnlineLessonSession).where(
                                OnlineLessonSession.status.in_(("waiting", "active")),
                                OnlineLessonSession.updated_at < cutoff,
                                ~OnlineLessonSession.id.in_(
                                    select(OnlineLessonParticipant.online_session_id).where(
                                        OnlineLessonParticipant.left_at.is_(None)
                                    )
                                ),
                            )
                        )
                    ).all()
                )
                for room in abandoned:
                    room.status = "archived"
                    room.ended_at = utcnow()
                pending_ids = list(
                    (
                        await session.scalars(
                            select(OnlineLessonSession.id).where(
                                OnlineLessonSession.status.in_(("completed", "archived")),
                                OnlineLessonSession.cleaned_at.is_(None),
                            )
                        )
                    ).all()
                )
                await session.commit()
            for room_id in pending_ids:
                await cleanup_online_room(sessions, settings, room_id)
            await cleanup_orphan_online_files(sessions, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("online_maintenance_failed")
        await asyncio.sleep(settings.online_cleanup_interval_seconds)


async def _save_operation(
    session: AsyncSession,
    settings: Settings,
    room: OnlineLessonSession,
    principal: OnlinePrincipal,
    role: str,
    payload: BoardOperationPayload,
) -> tuple[OnlineBoardOperation, bool]:
    existing = await session.scalar(
        select(OnlineBoardOperation).where(
            OnlineBoardOperation.online_session_id == room.id,
            OnlineBoardOperation.operation_id == payload.operation_id,
        )
    )
    if existing is not None:
        return existing, True
    if not await _can_draw(session, room, principal, role):
        raise HTTPException(403, "Преподаватель не разрешил работу на доске")
    if payload.kind.startswith("page_") and role != "teacher":
        raise HTTPException(403, "Страницами управляет только преподаватель")
    if payload.kind in {"page_add", "page_update"}:
        page_data = payload.payload.get("page")
        if not isinstance(page_data, dict) or page_data.get("kind") not in {
            "blank",
            "image",
            "pdf",
        }:
            raise HTTPException(422, "Некорректный тип страницы")
        asset_id = page_data.get("asset_id")
        if page_data.get("kind") in {"image", "pdf"}:
            asset = await session.get(OnlineLessonAsset, asset_id)
            if asset is None or asset.online_session_id != room.id:
                raise HTTPException(422, "Материал не принадлежит занятию")
    if payload.kind == "page_reorder":
        page_order = payload.payload.get("page_order")
        if (
            not isinstance(page_order, list)
            or len(page_order) > 500
            or any(not isinstance(page_id, str) for page_id in page_order)
        ):
            raise HTTPException(422, "Некорректный порядок страниц")
    locked_room = await session.scalar(
        select(OnlineLessonSession).where(OnlineLessonSession.id == room.id).with_for_update()
    )
    if locked_room is None:
        raise HTTPException(404, "Онлайн-занятие не найдено")
    locked_room.board_revision += 1
    locked_room.updated_at = utcnow()
    item = OnlineBoardOperation(
        online_session_id=room.id,
        operation_id=payload.operation_id,
        revision=locked_room.board_revision,
        page_id=payload.page_id,
        kind=payload.kind,
        payload=payload.payload,
        actor_person_id=principal.person.id,
    )
    if payload.kind == "page_activate":
        locked_room.active_page_id = payload.page_id
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await _maybe_snapshot(session, settings, locked_room)
    await session.commit()
    return item, False


def create_online_router(
    sessions: async_sessionmaker[AsyncSession],
    max_api: MaxApiClient,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/online", tags=["online-learning"])
    hub = RoomHub()
    upload_lock = asyncio.Lock()
    pending_room_bytes: dict[int, int] = defaultdict(int)
    pending_total_bytes = 0

    async def reserve_upload(room_id: int, declared_size: int) -> int:
        nonlocal pending_total_bytes
        reserved = max(1, declared_size)
        if reserved > settings.online_upload_limit_bytes:
            limit_mib = settings.online_upload_limit_bytes // (1024 * 1024)
            raise HTTPException(413, f"Файл превышает лимит {limit_mib} МБ")
        async with upload_lock:
            settings.online_upload_root.mkdir(parents=True, exist_ok=True)
            async with sessions() as session:
                room_used = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(OnlineLessonAsset.size_bytes), 0)).where(
                            OnlineLessonAsset.online_session_id == room_id
                        )
                    )
                    or 0
                )
                total_used = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(OnlineLessonAsset.size_bytes), 0))
                    )
                    or 0
                )
            if (
                room_used + pending_room_bytes[room_id] + reserved
                > settings.online_room_storage_limit_bytes
            ):
                raise HTTPException(413, "В комнате исчерпан лимит материалов 100 МБ")
            if (
                total_used + pending_total_bytes + reserved
                > settings.online_total_storage_limit_bytes
            ):
                raise HTTPException(507, "Временное хранилище онлайн-занятий заполнено")
            free_bytes = shutil.disk_usage(settings.online_upload_root).free
            if free_bytes - pending_total_bytes - reserved < settings.online_min_free_disk_bytes:
                raise HTTPException(507, "На сервере недостаточно свободного места для загрузки")
            pending_room_bytes[room_id] += reserved
            pending_total_bytes += reserved
        return reserved

    async def release_upload(room_id: int, reserved: int) -> None:
        nonlocal pending_total_bytes
        async with upload_lock:
            pending_room_bytes[room_id] = max(0, pending_room_bytes[room_id] - reserved)
            pending_total_bytes = max(0, pending_total_bytes - reserved)

    async def principal(
        raw_token: str | None = Cookie(default=None, alias=settings.online_cookie_name),
    ) -> OnlinePrincipal:
        result = await _session_principal(sessions, settings, raw_token)
        if result is None:
            raise HTTPException(401, "Требуется вход через MAX")
        return result

    CurrentPrincipal = Annotated[OnlinePrincipal, Depends(principal)]

    def require_csrf(request: Request, current: OnlinePrincipal) -> None:
        if request.method not in STATE_CHANGING_METHODS:
            return
        supplied = request.headers.get("X-Online-CSRF", "")
        if not supplied or not hmac.compare_digest(_hash(supplied), current.web_session.csrf_hash):
            raise HTTPException(403, "Недействительный CSRF-токен")
        origin = request.headers.get("Origin")
        if origin and settings.online_allowed_origins:
            if origin.rstrip("/") not in settings.online_allowed_origins:
                raise HTTPException(403, "Недопустимый источник запроса")

    @router.post("/auth/challenges", status_code=status.HTTP_202_ACCEPTED)
    async def request_challenge(payload: ChallengePayload, request: Request) -> dict[str, Any]:
        challenge_id = str(uuid.uuid4())
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = utcnow()
        request_ip_hash = _hash(request.client.host if request.client else "unknown")
        person: Person | None = None
        identity: PersonMaxIdentity | None = None
        try:
            phone = normalize_phone(payload.phone)
        except ValueError:
            phone = ""
        async with sessions() as session:
            matches = []
            if phone:
                matches = list(
                    (
                        await session.execute(
                            select(Person, PersonMaxIdentity)
                            .join(
                                PersonMaxIdentity,
                                PersonMaxIdentity.person_id == Person.id,
                            )
                            .where(
                                PersonMaxIdentity.verified_phone == phone,
                                PersonMaxIdentity.verified_at.is_not(None),
                                PersonMaxIdentity.max_user_id.is_not(None),
                                Person.active.is_(True),
                                Person.bot_access_enabled.is_(True),
                                Person.archived_at.is_(None),
                            )
                        )
                    ).all()
                )
            recent_ip_count = await session.scalar(
                select(func.count(OnlineAuthChallenge.id)).where(
                    OnlineAuthChallenge.request_ip_hash == request_ip_hash,
                    OnlineAuthChallenge.created_at >= now - timedelta(minutes=15),
                )
            )
            if len(matches) == 1 and int(recent_ip_count or 0) < OTP_WINDOW_LIMIT:
                person, identity = matches[0]
                recent_person = await session.scalar(
                    select(func.count(OnlineAuthChallenge.id)).where(
                        OnlineAuthChallenge.person_id == person.id,
                        OnlineAuthChallenge.created_at >= now - timedelta(minutes=15),
                    )
                )
                last_sent = await session.scalar(
                    select(func.max(OnlineAuthChallenge.delivered_at)).where(
                        OnlineAuthChallenge.person_id == person.id
                    )
                )
                if int(recent_person or 0) >= OTP_WINDOW_LIMIT or (
                    last_sent is not None
                    and now - _aware(last_sent) < timedelta(seconds=OTP_RESEND_SECONDS)
                ):
                    person = None
                    identity = None
            if person is not None:
                await session.execute(
                    update(OnlineAuthChallenge)
                    .where(
                        OnlineAuthChallenge.person_id == person.id,
                        OnlineAuthChallenge.used_at.is_(None),
                    )
                    .values(used_at=now)
                )
            item = OnlineAuthChallenge(
                id=challenge_id,
                person_id=person.id if person else None,
                code_hash=_otp_hash(settings, challenge_id, code),
                request_ip_hash=request_ip_hash,
                expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
            )
            session.add(item)
            await session.commit()
            if person is not None and identity is not None and identity.max_user_id is not None:
                message = (
                    "КРиТ — вход в онлайн-занятие\n\n"
                    f"Ваш код подтверждения: {code}\n\n"
                    "Срок действия: 5 минут.\n\n"
                    "Никому не сообщайте этот код.\n\n"
                    "Если вы не запрашивали вход, проигнорируйте сообщение."
                )
                try:
                    await max_api.send_text(user_id=identity.max_user_id, text=message)
                except Exception as error:
                    log.warning(
                        "online_otp_delivery_failed",
                        challenge_id=challenge_id,
                        error=str(error),
                    )
                else:
                    item.delivered_at = utcnow()
                    await session.commit()
        return {
            "challenge_id": challenge_id,
            "expires_in": OTP_TTL_SECONDS,
            "resend_after": OTP_RESEND_SECONDS,
        }

    @router.post("/auth/challenges/verify")
    async def verify_challenge(
        payload: VerifyPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        now = utcnow()
        async with sessions() as session:
            item = await session.get(OnlineAuthChallenge, payload.challenge_id)
            valid = bool(
                item
                and item.person_id is not None
                and item.delivered_at is not None
                and item.used_at is None
                and item.attempts < item.max_attempts
                and _aware(item.expires_at) > now
                and hmac.compare_digest(
                    item.code_hash,
                    _otp_hash(settings, payload.challenge_id, payload.code),
                )
            )
            if not valid:
                if item is not None and item.used_at is None:
                    item.attempts += 1
                    if item.attempts >= item.max_attempts:
                        item.used_at = now
                    await session.commit()
                raise HTTPException(401, "Код недействителен или истёк")
            person = await session.get(Person, item.person_id)
            if (
                person is None
                or not person.active
                or not person.bot_access_enabled
                or person.archived_at is not None
            ):
                item.used_at = now
                await session.commit()
                raise HTTPException(401, "Код недействителен или истёк")
            item.used_at = now
            raw_token = secrets.token_urlsafe(48)
            csrf_token = secrets.token_urlsafe(32)
            web_session = OnlineWebSession(
                token_hash=_hash(raw_token),
                person_id=person.id,
                csrf_hash=_hash(csrf_token),
                user_agent_hash=_hash(request.headers.get("user-agent", "")),
                expires_at=now + timedelta(hours=settings.online_session_hours),
            )
            session.add(web_session)
            roles = sorted(await _roles(session, person.id))
            await session.commit()
        response.set_cookie(
            settings.online_cookie_name,
            raw_token,
            max_age=settings.online_session_hours * 3600,
            httponly=True,
            secure=settings.online_cookie_secure,
            samesite="strict",
            path=SESSION_COOKIE_PATH,
        )
        return {"person": _public_person(person), "roles": roles, "csrf_token": csrf_token}

    @router.get("/auth/me")
    async def auth_me(response: Response, current: CurrentPrincipal) -> dict[str, Any]:
        # Rotate the non-authentication CSRF token when a page restores an existing
        # HttpOnly session. This keeps the session usable without exposing its cookie.
        csrf_token = secrets.token_urlsafe(32)
        async with sessions() as session:
            item = await session.get(OnlineWebSession, current.web_session.token_hash)
            if item is None or item.revoked_at is not None:
                raise HTTPException(401, "Требуется вход через MAX")
            item.csrf_hash = _hash(csrf_token)
            await session.commit()
        response.headers["Cache-Control"] = "no-store"
        return {
            "person": _public_person(current.person),
            "roles": sorted(current.roles),
            "csrf_token": csrf_token,
        }

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        response: Response,
        current: CurrentPrincipal,
    ) -> Response:
        require_csrf(request, current)
        async with sessions() as session:
            item = await session.get(OnlineWebSession, current.web_session.token_hash)
            if item is not None:
                item.revoked_at = utcnow()
                await session.commit()
        response.delete_cookie(settings.online_cookie_name, path=SESSION_COOKIE_PATH)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/lessons")
    async def list_lessons(
        current: CurrentPrincipal,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        now = utcnow()
        start = _aware(date_from) if date_from else now - timedelta(days=30)
        end = _aware(date_to) if date_to else now + timedelta(days=90)
        participant_lessons = select(LessonParticipant.lesson_id).where(
            LessonParticipant.person_id == current.person.id,
            LessonParticipant.cancelled_at.is_(None),
        )
        query = (
            select(Lesson)
            .where(
                Lesson.start_at >= start,
                Lesson.start_at < end,
                Lesson.delivery_mode == "online",
                Lesson.status != "cancelled",
                or_(Lesson.teacher_id == current.person.id, Lesson.id.in_(participant_lessons)),
            )
            .order_by(Lesson.start_at)
        )
        async with sessions() as session:
            items = list(
                (await session.scalars(query.offset((page - 1) * page_size).limit(page_size))).all()
            )
            rooms = {
                room.lesson_id: room
                for room in (
                    await session.scalars(
                        select(OnlineLessonSession).where(
                            OnlineLessonSession.lesson_id.in_([item.id for item in items] or [-1])
                        )
                    )
                ).all()
            }
        return {
            "data": [
                {
                    "id": item.id,
                    "subject": item.subject_name_snapshot,
                    "teacher": item.teacher_name_snapshot,
                    "role": ("teacher" if item.teacher_id == current.person.id else "student"),
                    "start_at": item.start_at,
                    "end_at": item.end_at,
                    "status": item.status,
                    "delivery_mode": item.delivery_mode,
                    "online_room": (
                        {"id": rooms[item.id].id, "status": rooms[item.id].status}
                        if item.id in rooms
                        else None
                    ),
                }
                for item in items
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "has_more": len(items) == page_size,
            },
        }

    @router.post("/lessons/{lesson_id}/room", status_code=status.HTTP_201_CREATED)
    async def create_room(
        lesson_id: int,
        request: Request,
        response: Response,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            lesson = await session.get(Lesson, lesson_id)
            lesson_role = await _lesson_role(session, current.person.id, lesson) if lesson else None
            if lesson is None or lesson_role != "teacher":
                raise HTTPException(404, "Занятие не найдено")
            if lesson.delivery_mode != "online" or lesson.status not in {
                "planned",
                "scheduled",
                "in_progress",
            }:
                raise HTTPException(409, "Онлайн-комната доступна только для онлайн-занятия")
            existing = await session.scalar(
                select(OnlineLessonSession).where(OnlineLessonSession.lesson_id == lesson_id)
            )
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return {"id": existing.id, "lesson_id": lesson_id, "status": existing.status}
            item = OnlineLessonSession(
                lesson_id=lesson_id,
                room_key=str(uuid.uuid4()),
                status="waiting",
                created_by_person_id=current.person.id,
            )
            session.add(item)
            await session.flush()
            session.add(
                OnlineAuditEvent(
                    online_session_id=item.id,
                    actor_person_id=current.person.id,
                    event_type="room.created",
                    details={"lesson_id": lesson_id},
                )
            )
            await session.commit()
            return {"id": item.id, "lesson_id": lesson_id, "status": item.status}

    @router.get("/rooms/{room_id}")
    async def room_details(room_id: int, current: CurrentPrincipal) -> dict[str, Any]:
        async with sessions() as session:
            room, lesson, role = await _room_access(session, room_id, current)
            participants = list(
                (
                    await session.execute(
                        select(LessonParticipant, Person)
                        .join(Person, Person.id == LessonParticipant.person_id)
                        .where(
                            LessonParticipant.lesson_id == lesson.id,
                            LessonParticipant.cancelled_at.is_(None),
                        )
                        .order_by(Person.full_name)
                    )
                ).all()
            )
            permissions = {
                item.person_id: item
                for item in (
                    await session.scalars(
                        select(OnlineBoardPermission).where(
                            OnlineBoardPermission.online_session_id == room.id
                        )
                    )
                ).all()
            }
            can_draw = await _can_draw(session, room, current, role)
        return {
            "id": room.id,
            "lesson_id": lesson.id,
            "subject": lesson.subject_name_snapshot,
            "teacher": lesson.teacher_name_snapshot,
            "start_at": lesson.start_at,
            "end_at": lesson.end_at,
            "status": room.status,
            "role": role,
            "can_draw": can_draw,
            "student_drawing_mode": room.student_drawing_mode,
            "active_page_id": room.active_page_id,
            "board_revision": room.board_revision,
            "participants": [
                {
                    "person_id": person.id,
                    "full_name": person.full_name,
                    "can_draw": bool(
                        person.id in permissions
                        and permissions[person.id].can_draw
                        and permissions[person.id].revoked_at is None
                    ),
                    "invited": bool(
                        person.id in permissions
                        and permissions[person.id].invited_at is not None
                        and not permissions[person.id].can_draw
                    ),
                }
                for _, person in participants
            ],
        }

    @router.put("/rooms/{room_id}/status")
    async def set_room_status(
        room_id: int,
        payload: RoomStatusPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель управляет занятием")
            room.status = payload.status
            room.updated_at = utcnow()
            if payload.status == "active" and room.started_at is None:
                room.started_at = utcnow()
            if payload.status in {"completed", "archived"}:
                room.ended_at = utcnow()
            session.add(
                OnlineAuditEvent(
                    online_session_id=room.id,
                    actor_person_id=current.person.id,
                    event_type=f"room.{payload.status}",
                    details={},
                )
            )
            await session.commit()
        await hub.broadcast(room_id, {"type": "room.status", "status": payload.status})
        cleanup_result = None
        if payload.status in {"completed", "archived"}:
            await hub.close_room(room_id)
            cleanup_result = await cleanup_online_room(sessions, settings, room_id)
        return {"id": room_id, "status": payload.status, "cleanup": cleanup_result}

    @router.put("/rooms/{room_id}/permissions/{person_id}")
    async def set_permission(
        room_id: int,
        person_id: int,
        payload: PermissionPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, lesson, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель управляет доступом")
            target_role = await _lesson_role(session, person_id, lesson)
            if target_role != "student":
                raise HTTPException(422, "Ученик не назначен на это занятие")
            item = await session.scalar(
                select(OnlineBoardPermission).where(
                    OnlineBoardPermission.online_session_id == room.id,
                    OnlineBoardPermission.person_id == person_id,
                )
            )
            now = utcnow()
            if item is None:
                item = OnlineBoardPermission(
                    online_session_id=room.id,
                    person_id=person_id,
                    can_draw=payload.can_draw,
                    changed_by_person_id=current.person.id,
                )
                session.add(item)
            is_invitation = payload.invite and payload.can_draw
            item.can_draw = payload.can_draw and not is_invitation
            item.invited_at = now if is_invitation else None
            item.granted_at = now if item.can_draw else item.granted_at
            item.revoked_at = None if payload.can_draw else now
            item.changed_by_person_id = current.person.id
            item.updated_at = now
            room.student_drawing_mode = "selected"
            session.add(
                OnlineAuditEvent(
                    online_session_id=room.id,
                    actor_person_id=current.person.id,
                    event_type="permission.granted" if payload.can_draw else "permission.revoked",
                    details={"person_id": person_id, "invite": payload.invite},
                )
            )
            await session.commit()
        message = {
            "type": "permission.changed",
            "person_id": person_id,
            "can_draw": item.can_draw,
            "invited": is_invitation,
        }
        await hub.broadcast(room_id, message)
        return message

    @router.put("/rooms/{room_id}/board-invitation/accept")
    async def accept_board_invitation(
        room_id: int,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "student":
                raise HTTPException(403, "Приглашение предназначено ученику")
            item = await session.scalar(
                select(OnlineBoardPermission).where(
                    OnlineBoardPermission.online_session_id == room.id,
                    OnlineBoardPermission.person_id == current.person.id,
                )
            )
            if (
                item is None
                or item.invited_at is None
                or _aware(item.invited_at) < utcnow() - timedelta(minutes=10)
                or item.revoked_at is not None
            ):
                raise HTTPException(409, "Приглашение уже недействительно")
            item.can_draw = True
            item.invited_at = None
            item.granted_at = utcnow()
            item.updated_at = utcnow()
            session.add(
                OnlineAuditEvent(
                    online_session_id=room.id,
                    actor_person_id=current.person.id,
                    event_type="permission.accepted",
                    details={},
                )
            )
            await session.commit()
        message = {
            "type": "permission.changed",
            "person_id": current.person.id,
            "can_draw": True,
            "invited": False,
        }
        await hub.broadcast(room_id, message)
        return message

    @router.put("/rooms/{room_id}/drawing-mode")
    async def set_drawing_mode(
        room_id: int,
        payload: DrawingModePayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, str]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель управляет режимом доски")
            room.student_drawing_mode = payload.mode
            room.updated_at = utcnow()
            session.add(
                OnlineAuditEvent(
                    online_session_id=room.id,
                    actor_person_id=current.person.id,
                    event_type="board.mode_changed",
                    details={"mode": payload.mode},
                )
            )
            await session.commit()
        message = {"type": "drawing.mode", "mode": payload.mode}
        await hub.broadcast(room_id, message)
        return {"mode": payload.mode}

    @router.get("/rooms/{room_id}/assets")
    async def list_assets(room_id: int, current: CurrentPrincipal) -> list[dict[str, Any]]:
        async with sessions() as session:
            await _room_access(session, room_id, current)
            items = list(
                (
                    await session.scalars(
                        select(OnlineLessonAsset)
                        .where(OnlineLessonAsset.online_session_id == room_id)
                        .order_by(OnlineLessonAsset.created_at)
                    )
                ).all()
            )
        return [
            {
                "id": item.id,
                "name": item.original_name,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "created_at": item.created_at,
                "download_url": f"/assets/{item.id}/content",
            }
            for item in items
        ]

    @router.post("/rooms/{room_id}/assets", status_code=status.HTTP_201_CREATED)
    async def upload_asset(
        room_id: int,
        request: Request,
        current: CurrentPrincipal,
        upload: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель загружает материалы")
        original_name = Path(upload.filename or "").name.strip()
        suffix = Path(original_name).suffix.lower()
        media_type = (upload.content_type or "application/octet-stream").lower()
        if (
            not original_name
            or suffix not in ASSET_EXTENSIONS
            or media_type not in ASSET_MEDIA_TYPES
        ):
            raise HTTPException(415, "Допустимы PDF, DOCX, PPTX, PNG, JPG, WebP и TXT")
        reserved = await reserve_upload(
            room_id, upload.size if upload.size is not None else settings.online_upload_limit_bytes
        )
        asset_id = str(uuid.uuid4())
        stored_name = f"{asset_id}{suffix}"
        room_root = settings.online_upload_root.resolve() / str(room_id)
        room_root.mkdir(parents=True, exist_ok=True)
        destination = room_root / stored_name
        digest = hashlib.sha256()
        size = 0
        header = b""
        try:
            with destination.open("xb") as target:
                while chunk := await upload.read(1024 * 1024):
                    if not header:
                        header = chunk[:4096]
                    size += len(chunk)
                    if size > settings.online_upload_limit_bytes:
                        raise HTTPException(413, "Файл превышает допустимый размер")
                    digest.update(chunk)
                    target.write(chunk)
            if not header or not _valid_asset_signature(suffix, header):
                raise HTTPException(415, "Содержимое файла не соответствует его формату")
        except BaseException:
            destination.unlink(missing_ok=True)
            await anyio.to_thread.run_sync(_remove_empty_directory, room_root)
            await release_upload(room_id, reserved)
            raise
        finally:
            await upload.close()
        try:
            async with sessions() as session:
                item = OnlineLessonAsset(
                    id=asset_id,
                    online_session_id=room.id,
                    uploader_person_id=current.person.id,
                    original_name=original_name[:255],
                    stored_name=stored_name,
                    media_type=media_type,
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                )
                session.add(item)
                session.add(
                    OnlineAuditEvent(
                        online_session_id=room.id,
                        actor_person_id=current.person.id,
                        event_type="asset.uploaded",
                        details={"asset_id": asset_id, "size_bytes": size},
                    )
                )
                await session.commit()
        except BaseException:
            destination.unlink(missing_ok=True)
            await anyio.to_thread.run_sync(_remove_empty_directory, room_root)
            raise
        finally:
            await release_upload(room_id, reserved)
        result = {
            "id": asset_id,
            "name": item.original_name,
            "media_type": media_type,
            "size_bytes": size,
            "download_url": f"/assets/{asset_id}/content",
        }
        await hub.broadcast(room_id, {"type": "asset.created", "asset": result})
        return result

    @router.get("/assets/{asset_id}/content")
    async def download_asset(asset_id: str, current: CurrentPrincipal) -> FileResponse:
        async with sessions() as session:
            item = await session.get(OnlineLessonAsset, asset_id)
            if item is None:
                raise HTTPException(404, "Материал не найден")
            await _room_access(session, item.online_session_id, current)
        path = (
            settings.online_upload_root.resolve() / str(item.online_session_id) / item.stored_name
        )
        if not path.is_file():
            raise HTTPException(404, "Файл материала отсутствует")
        safe_media_type = (
            item.media_type
            if item.media_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
            else "application/octet-stream"
        )
        return FileResponse(
            path,
            media_type=safe_media_type,
            filename=item.original_name,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @router.get("/rooms/{room_id}/board")
    async def board_state(
        room_id: int,
        current: CurrentPrincipal,
        since_revision: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            snapshot = await session.scalar(
                select(OnlineBoardSnapshot)
                .where(
                    OnlineBoardSnapshot.online_session_id == room.id,
                    OnlineBoardSnapshot.revision > since_revision,
                )
                .order_by(OnlineBoardSnapshot.revision.desc())
                .limit(1)
            )
            operation_start = snapshot.revision if snapshot is not None else since_revision
            operations = list(
                (
                    await session.scalars(
                        select(OnlineBoardOperation)
                        .where(
                            OnlineBoardOperation.online_session_id == room.id,
                            OnlineBoardOperation.revision > operation_start,
                        )
                        .order_by(OnlineBoardOperation.revision)
                        .limit(5000)
                    )
                ).all()
            )
            can_draw = await _can_draw(session, room, current, role)
        return {
            "revision": room.board_revision,
            "active_page_id": room.active_page_id,
            "can_draw": can_draw,
            "snapshot": snapshot.state if snapshot is not None else None,
            "operations": [_operation_view(item) for item in operations],
        }

    @router.post("/rooms/{room_id}/board/operations", status_code=status.HTTP_201_CREATED)
    async def add_board_operation(
        room_id: int,
        payload: BoardOperationPayload,
        request: Request,
        response: Response,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            try:
                item, duplicate = await _save_operation(
                    session, settings, room, current, role, payload
                )
            except IntegrityError:
                await session.rollback()
                item = await session.scalar(
                    select(OnlineBoardOperation).where(
                        OnlineBoardOperation.online_session_id == room_id,
                        OnlineBoardOperation.operation_id == payload.operation_id,
                    )
                )
                if item is None:
                    raise HTTPException(409, "Конфликт версии доски") from None
                duplicate = True
        if duplicate:
            response.status_code = status.HTTP_200_OK
        result = {**_operation_view(item), "duplicate": duplicate}
        if not duplicate:
            await hub.broadcast(room_id, {"type": "board.operation", "operation": result})
        return result

    @router.put("/rooms/{room_id}/hand")
    async def set_hand(
        room_id: int,
        payload: HandPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "student":
                raise HTTPException(403, "Поднять руку может ученик")
            item = await session.scalar(
                select(OnlineLessonParticipant).where(
                    OnlineLessonParticipant.online_session_id == room.id,
                    OnlineLessonParticipant.person_id == current.person.id,
                )
            )
            if item is None:
                item = OnlineLessonParticipant(
                    online_session_id=room.id, person_id=current.person.id
                )
                session.add(item)
            item.hand_raised_at = utcnow() if payload.raised else None
            await session.commit()
        message = {
            "type": "hand.changed",
            "person_id": current.person.id,
            "full_name": current.person.full_name,
            "raised": payload.raised,
        }
        await hub.broadcast(room_id, message)
        return message

    @router.get("/rooms/{room_id}/chat")
    async def list_chat(room_id: int, current: CurrentPrincipal) -> list[dict[str, Any]]:
        async with sessions() as session:
            await _room_access(session, room_id, current)
            rows = (
                await session.execute(
                    select(OnlineChatMessage, Person)
                    .join(Person, Person.id == OnlineChatMessage.author_person_id)
                    .where(
                        OnlineChatMessage.online_session_id == room_id,
                        OnlineChatMessage.deleted_at.is_(None),
                    )
                    .order_by(OnlineChatMessage.id.desc())
                    .limit(200)
                )
            ).all()
        return [
            {
                "id": message.id,
                "author": _public_person(person),
                "body": message.body,
                "created_at": message.created_at,
            }
            for message, person in reversed(rows)
        ]

    @router.post("/rooms/{room_id}/chat", status_code=status.HTTP_201_CREATED)
    async def send_chat(
        room_id: int,
        payload: ChatPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role == "student" and not room.chat_enabled:
                raise HTTPException(403, "Чат временно отключён преподавателем")
            item = OnlineChatMessage(
                online_session_id=room.id,
                author_person_id=current.person.id,
                body=payload.body,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
        result = {
            "id": item.id,
            "author": _public_person(current.person),
            "body": item.body,
            "created_at": item.created_at,
        }
        await hub.broadcast(room_id, {"type": "chat.message", "message": result})
        return result

    @router.post("/rooms/{room_id}/assignments", status_code=status.HTTP_201_CREATED)
    async def create_assignment(
        room_id: int,
        payload: AssignmentPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        recipient_ids = sorted(set(payload.recipient_ids))
        async with sessions() as session:
            room, lesson, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель создаёт задания")
            for person_id in recipient_ids:
                if await _lesson_role(session, person_id, lesson) != "student":
                    raise HTTPException(422, "Один из получателей не назначен на занятие")
            item = OnlineAssignment(
                online_session_id=room.id,
                author_person_id=current.person.id,
                kind=payload.kind,
                title=" ".join(payload.title.split()),
                instructions=payload.instructions.strip(),
                due_at=_aware(payload.due_at) if payload.due_at else None,
            )
            session.add(item)
            await session.flush()
            session.add_all(
                [
                    OnlineAssignmentRecipient(assignment_id=item.id, person_id=person_id)
                    for person_id in recipient_ids
                ]
            )
            await session.commit()
        result = {"id": item.id, "title": item.title, "recipient_ids": recipient_ids}
        await hub.broadcast(room_id, {"type": "assignment.created", "assignment": result})
        return result

    @router.get("/rooms/{room_id}/assignments")
    async def list_assignments(room_id: int, current: CurrentPrincipal) -> list[dict[str, Any]]:
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            query = select(OnlineAssignment).where(OnlineAssignment.online_session_id == room.id)
            if role == "student":
                query = query.join(
                    OnlineAssignmentRecipient,
                    OnlineAssignmentRecipient.assignment_id == OnlineAssignment.id,
                ).where(OnlineAssignmentRecipient.person_id == current.person.id)
            items = list((await session.scalars(query.order_by(OnlineAssignment.id))).all())
            assignment_ids = [item.id for item in items]
            recipient_rows = list(
                (
                    await session.scalars(
                        select(OnlineAssignmentRecipient).where(
                            OnlineAssignmentRecipient.assignment_id.in_(assignment_ids or [-1])
                        )
                    )
                ).all()
            )
            submission_rows = list(
                (
                    await session.scalars(
                        select(OnlineAssignmentSubmission).where(
                            OnlineAssignmentSubmission.assignment_id.in_(assignment_ids or [-1])
                        )
                    )
                ).all()
            )
            recipients_by_assignment: dict[int, list[OnlineAssignmentRecipient]] = defaultdict(list)
            for recipient in recipient_rows:
                recipients_by_assignment[recipient.assignment_id].append(recipient)
            submissions = {
                (submission.assignment_id, submission.person_id): submission
                for submission in submission_rows
            }
            people = {
                person.id: person
                for person in (
                    await session.scalars(
                        select(Person).where(
                            Person.id.in_([entry.person_id for entry in recipient_rows] or [-1])
                        )
                    )
                ).all()
            }
            result: list[dict[str, Any]] = []
            for item in items:
                view: dict[str, Any] = {
                    "id": item.id,
                    "kind": item.kind,
                    "title": item.title,
                    "instructions": item.instructions,
                    "due_at": item.due_at,
                }
                recipients = recipients_by_assignment[item.id]
                if role == "student":
                    recipient = next(
                        entry for entry in recipients if entry.person_id == current.person.id
                    )
                    submission = submissions.get((item.id, current.person.id))
                    view["status"] = recipient.status
                    view["submission"] = (
                        {
                            "answer_text": submission.answer_text,
                            "board_payload": submission.board_payload,
                            "teacher_comment": submission.teacher_comment,
                            "grade": submission.grade,
                        }
                        if submission
                        else None
                    )
                else:
                    view["submissions"] = [
                        {
                            "person": _public_person(people[entry.person_id]),
                            "status": entry.status,
                            "answer_text": (
                                submissions[(item.id, entry.person_id)].answer_text
                                if (item.id, entry.person_id) in submissions
                                else None
                            ),
                            "board_payload": (
                                submissions[(item.id, entry.person_id)].board_payload
                                if (item.id, entry.person_id) in submissions
                                else None
                            ),
                            "teacher_comment": (
                                submissions[(item.id, entry.person_id)].teacher_comment
                                if (item.id, entry.person_id) in submissions
                                else None
                            ),
                            "grade": (
                                submissions[(item.id, entry.person_id)].grade
                                if (item.id, entry.person_id) in submissions
                                else None
                            ),
                        }
                        for entry in recipients
                    ]
                result.append(view)
        return result

    @router.put("/assignments/{assignment_id}/submission")
    async def save_submission(
        assignment_id: int,
        payload: SubmissionPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            assignment = await session.get(OnlineAssignment, assignment_id)
            recipient = await session.get(
                OnlineAssignmentRecipient, (assignment_id, current.person.id)
            )
            if assignment is None or recipient is None:
                raise HTTPException(404, "Задание не найдено")
            item = await session.scalar(
                select(OnlineAssignmentSubmission).where(
                    OnlineAssignmentSubmission.assignment_id == assignment_id,
                    OnlineAssignmentSubmission.person_id == current.person.id,
                )
            )
            if item is None:
                item = OnlineAssignmentSubmission(
                    assignment_id=assignment_id, person_id=current.person.id
                )
                session.add(item)
            item.answer_text = payload.answer_text
            item.board_payload = payload.board_payload
            item.updated_at = utcnow()
            if payload.submit:
                item.submitted_at = utcnow()
                recipient.status = "submitted"
            else:
                recipient.status = "in_progress"
            await session.commit()
        return {"assignment_id": assignment_id, "status": recipient.status}

    @router.put("/assignments/{assignment_id}/submissions/{person_id}/review")
    async def review_submission(
        assignment_id: int,
        person_id: int,
        payload: ReviewPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            assignment = await session.get(OnlineAssignment, assignment_id)
            if assignment is None:
                raise HTTPException(404, "Задание не найдено")
            room, _, role = await _room_access(session, assignment.online_session_id, current)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель проверяет работы")
            recipient = await session.get(OnlineAssignmentRecipient, (assignment_id, person_id))
            item = await session.scalar(
                select(OnlineAssignmentSubmission).where(
                    OnlineAssignmentSubmission.assignment_id == assignment_id,
                    OnlineAssignmentSubmission.person_id == person_id,
                )
            )
            if recipient is None or item is None:
                raise HTTPException(404, "Работа не найдена")
            recipient.status = payload.status
            item.teacher_comment = payload.teacher_comment
            item.grade = payload.grade
            item.reviewed_at = utcnow()
            item.updated_at = utcnow()
            await session.commit()
        await hub.broadcast(
            room.id,
            {"type": "assignment.reviewed", "assignment_id": assignment_id, "person_id": person_id},
        )
        return {"assignment_id": assignment_id, "person_id": person_id, "status": payload.status}

    @router.post("/rooms/{room_id}/polls", status_code=status.HTTP_201_CREATED)
    async def create_poll(
        room_id: int,
        payload: PollPayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            room, _, role = await _room_access(session, room_id, current)
            _ensure_room_open(room)
            if role != "teacher":
                raise HTTPException(403, "Только преподаватель создаёт опрос")
            item = OnlinePoll(
                online_session_id=room.id,
                author_person_id=current.person.id,
                question=" ".join(payload.question.split()),
                options=payload.options,
                show_results=payload.show_results,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
        result = {"id": item.id, "question": item.question, "options": item.options}
        await hub.broadcast(room_id, {"type": "poll.created", "poll": result})
        return result

    @router.put("/polls/{poll_id}/response")
    async def answer_poll(
        poll_id: int,
        payload: PollResponsePayload,
        request: Request,
        current: CurrentPrincipal,
    ) -> dict[str, Any]:
        require_csrf(request, current)
        async with sessions() as session:
            poll = await session.get(OnlinePoll, poll_id)
            if poll is None or poll.closed_at is not None:
                raise HTTPException(404, "Опрос не найден или завершён")
            await _room_access(session, poll.online_session_id, current)
            if payload.option_index >= len(poll.options):
                raise HTTPException(422, "Вариант ответа не существует")
            item = await session.get(OnlinePollResponse, (poll.id, current.person.id))
            if item is None:
                item = OnlinePollResponse(
                    poll_id=poll.id,
                    person_id=current.person.id,
                    option_index=payload.option_index,
                )
                session.add(item)
            else:
                item.option_index = payload.option_index
            await session.commit()
        return {"poll_id": poll_id, "answered": True}

    @router.websocket("/rooms/{room_id}/ws")
    async def room_websocket(websocket: WebSocket, room_id: int) -> None:
        origin = websocket.headers.get("origin")
        if settings.online_allowed_origins and (
            not origin or origin.rstrip("/") not in settings.online_allowed_origins
        ):
            await websocket.close(code=4403)
            return
        raw_token = websocket.cookies.get(settings.online_cookie_name)
        current = await _session_principal(sessions, settings, raw_token)
        if current is None:
            await websocket.close(code=4401)
            return
        async with sessions() as session:
            try:
                room, _, role = await _room_access(session, room_id, current)
            except HTTPException:
                await websocket.close(code=4404)
                return
            participant = await session.scalar(
                select(OnlineLessonParticipant).where(
                    OnlineLessonParticipant.online_session_id == room.id,
                    OnlineLessonParticipant.person_id == current.person.id,
                )
            )
            if participant is None:
                participant = OnlineLessonParticipant(
                    online_session_id=room.id, person_id=current.person.id
                )
                session.add(participant)
            else:
                participant.joined_at = utcnow()
                participant.left_at = None
            await session.commit()
        await hub.connect(room_id, websocket)
        await hub.broadcast(
            room_id,
            {
                "type": "presence.joined",
                "person": _public_person(current.person),
                "role": role,
            },
        )
        try:
            while True:
                message = await websocket.receive_json()
                refreshed = await _session_principal(sessions, settings, raw_token)
                if refreshed is None:
                    await websocket.close(code=4401)
                    return
                current = refreshed
                message_type = message.get("type")
                if message_type == "cursor":
                    cursor = message.get("cursor")
                    if isinstance(cursor, dict) and {"x", "y"} <= cursor.keys():
                        await hub.broadcast(
                            room_id,
                            {
                                "type": "cursor",
                                "person": _public_person(current.person),
                                "cursor": {"x": cursor["x"], "y": cursor["y"]},
                            },
                        )
                    continue
                if message_type != "board.operation":
                    await websocket.send_json({"type": "error", "code": "UNKNOWN_MESSAGE"})
                    continue
                try:
                    payload = BoardOperationPayload.model_validate(message.get("operation"))
                    async with sessions() as session:
                        room, _, role = await _room_access(session, room_id, current)
                        item, duplicate = await _save_operation(
                            session, settings, room, current, role, payload
                        )
                except (ValueError, HTTPException) as error:
                    detail = (
                        error.detail
                        if isinstance(error, HTTPException)
                        else "Некорректная операция"
                    )
                    await websocket.send_json(
                        {"type": "error", "code": "OPERATION_REJECTED", "message": detail}
                    )
                    continue
                result = {**_operation_view(item), "duplicate": duplicate}
                if not duplicate:
                    await hub.broadcast(room_id, {"type": "board.operation", "operation": result})
                else:
                    await websocket.send_json({"type": "board.ack", "operation": result})
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(room_id, websocket)
            async with sessions() as session:
                participant = await session.scalar(
                    select(OnlineLessonParticipant).where(
                        OnlineLessonParticipant.online_session_id == room_id,
                        OnlineLessonParticipant.person_id == current.person.id,
                    )
                )
                if participant is not None:
                    participant.left_at = utcnow()
                    await session.commit()
            await hub.broadcast(
                room_id,
                {"type": "presence.left", "person_id": current.person.id},
            )

    return router
