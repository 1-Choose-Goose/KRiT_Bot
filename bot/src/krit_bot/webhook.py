import asyncio
import json
import os
import secrets
import shutil
import signal
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import jwt
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from .administration import create_administration_router
from .auth import AdminPrincipal, decode_access_token, issue_access_token, normalize_admin_username
from .backups import BackupScheduler, BackupService, create_backup_router
from .communication_models import CommunicationMessage
from .communications import create_communications_router, run_communications_maintenance
from .config import Settings
from .db import (
    EXPECTED_ALEMBIC_REVISION,
    AccessAttempt,
    AdminUser,
    BotState,
    Person,
    PersonRole,
    StudentGuardian,
    SyndicationJob,
    build_engine,
    build_session_factory,
    ensure_schema,
    normalize_phone,
    utcnow,
)
from .handler import EchoHandler
from .learning import create_learning_router
from .learning_models import (
    AuditEvent,
    GroupMembership,
    Lesson,
    LessonParticipant,
    NotificationJob,
    PersonMaxIdentity,
    StudyGroup,
)
from .learning_notifications import LearningNotificationWorker
from .max_api import WEBHOOK_UPDATE_TYPES, MaxApiClient
from .polling import run_polling
from .restores import RestoreService, create_restore_router
from .syndication import (
    SyndicationWorker,
    VkApiClient,
    VkLongPollWorker,
    register_vk_event,
)
from .system_status import SystemStatusProvider

log = structlog.get_logger()
bearer = HTTPBearer(auto_error=False)
ROLES = {"student", "parent", "teacher"}
password_hash = PasswordHash.recommended()


class LoginRateLimiter:
    def __init__(
        self,
        *,
        window_seconds: float = 5 * 60,
        per_identity_limit: int = 5,
        per_address_limit: int = 30,
        max_buckets: int = 4096,
    ) -> None:
        self.window_seconds = window_seconds
        self.per_identity_limit = per_identity_limit
        self.per_address_limit = per_address_limit
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[tuple[str, ...], deque[float]] = OrderedDict()

    @property
    def bucket_count(self) -> int:
        return len(self._buckets)

    def _events(self, key: tuple[str, ...], now: float) -> deque[float]:
        events = self._buckets.pop(key, deque())
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if events:
            self._buckets[key] = events
        return events

    def _append(self, key: tuple[str, ...], now: float) -> None:
        events = self._events(key, now)
        events.append(now)
        self._buckets[key] = events
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)

    def retry_after(
        self,
        username: str,
        address: str,
        *,
        now: float | None = None,
    ) -> int | None:
        checked_at = time.monotonic() if now is None else now
        identity = self._events(("identity", address, username), checked_at)
        source = self._events(("address", address), checked_at)
        waits: list[float] = []
        if len(identity) >= self.per_identity_limit:
            waits.append(self.window_seconds - (checked_at - identity[0]))
        if len(source) >= self.per_address_limit:
            waits.append(self.window_seconds - (checked_at - source[0]))
        return max(1, int(max(waits) + 0.999)) if waits else None

    def record_failure(
        self,
        username: str,
        address: str,
        *,
        now: float | None = None,
    ) -> None:
        recorded_at = time.monotonic() if now is None else now
        self._append(("identity", address, username), recorded_at)
        self._append(("address", address), recorded_at)

    def record_success(self, username: str, address: str) -> None:
        self._buckets.pop(("identity", address, username), None)
        self._buckets.pop(("address", address), None)


async def ensure_max_webhook_subscription(
    api: MaxApiClient, settings: Settings
) -> bool:
    if settings.bot_mode != "webhook" or not settings.max_webhook_url:
        return False
    if settings.max_webhook_secret is None:
        raise RuntimeError("MAX_WEBHOOK_SECRET is required for webhook mode")
    result = await api.subscribe_webhook(
        url=settings.max_webhook_url,
        secret=settings.max_webhook_secret.get_secret_value(),
        update_types=WEBHOOK_UPDATE_TYPES,
    )
    if not result.get("success"):
        raise RuntimeError(
            "MAX rejected webhook subscription: "
            + str(result.get("message") or "unknown error")
        )
    return True


async def run_max_webhook_subscription(
    api: MaxApiClient,
    settings: Settings,
    *,
    max_attempts: int = 5,
    base_delay_seconds: float = 5.0,
) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            subscribed = await ensure_max_webhook_subscription(api, settings)
            if subscribed:
                log.info(
                    "max_webhook_subscription_verified",
                    url=settings.max_webhook_url,
                    update_types=WEBHOOK_UPDATE_TYPES,
                )
            return subscribed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "max_webhook_subscription_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                error_type=type(exc).__name__,
            )
            if attempt == max_attempts:
                return False
            delay = min(5 * 60, base_delay_seconds * (2 ** (attempt - 1)))
            await asyncio.sleep(delay)
    return False


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class TokenView(BaseModel):
    id: int
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str
    full_name: str
    role: str
    must_change_password: bool


class InitialPasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=7, max_length=256)


class PersonPayload(BaseModel):
    full_name: str = Field(min_length=3, max_length=250)
    phone: str = Field(min_length=10, max_length=32)
    max_auth_phone: str | None = Field(default=None, max_length=32)
    roles: list[str] = Field(default_factory=list, min_length=1)
    active: bool = True
    bot_access_enabled: bool | None = None


class ArchivePersonPayload(BaseModel):
    resolve_future_student_dependencies: bool = False


class PersonAggregatePayload(BaseModel):
    person: PersonPayload
    parent_ids: list[int] = Field(default_factory=list)
    student_ids: list[int] = Field(default_factory=list)
    new_parents: list[PersonPayload] = Field(default_factory=list)
    new_students: list[PersonPayload] = Field(default_factory=list)


class RelatedPersonView(BaseModel):
    id: int
    full_name: str
    phone: str
    max_auth_phone: str | None
    max_user_id: int | None
    active: bool
    bot_access_enabled: bool


class PersonView(BaseModel):
    id: int
    full_name: str
    phone: str
    max_auth_phone: str | None
    roles: list[str]
    active: bool
    bot_access_enabled: bool
    max_user_id: int | None
    archived_at: datetime | None
    guardians: list[RelatedPersonView]
    students: list[RelatedPersonView]


class AccessAttemptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_user_id: int
    display_name: str | None
    username: str | None
    attempts: int
    last_seen_at: Any


class ManagementSnapshot(BaseModel):
    status: str
    center_timezone: str
    people: list[PersonView]
    archived_people: list[PersonView]
    access_attempts: list[AccessAttemptView]


def as_related(person: Person) -> RelatedPersonView:
    return RelatedPersonView.model_validate(
        {
            "id": person.id,
            "full_name": person.full_name,
            "phone": person.phone,
            "max_auth_phone": person.max_auth_phone,
            "max_user_id": person.max_identity.max_user_id if person.max_identity else None,
            "active": person.active,
            "bot_access_enabled": person.bot_access_enabled,
        }
    )


def as_person_view(person: Person) -> PersonView:
    return PersonView(
        id=person.id,
        full_name=person.full_name,
        phone=person.phone,
        max_auth_phone=person.max_auth_phone,
        roles=sorted(link.role for link in person.role_links),
        active=person.active,
        bot_access_enabled=person.bot_access_enabled,
        max_user_id=person.max_identity.max_user_id if person.max_identity else None,
        archived_at=person.archived_at,
        guardians=[as_related(link.guardian) for link in person.guardian_links],
        students=[as_related(link.student) for link in person.student_links],
    )


def normalized_person_data(payload: PersonPayload) -> tuple[list[str], str, str | None]:
    roles = sorted(set(payload.roles))
    if not roles or not set(roles).issubset(ROLES):
        raise HTTPException(status_code=422, detail="Unknown role")
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid phone") from exc
    max_auth_phone = None
    if payload.max_auth_phone and payload.max_auth_phone.strip():
        try:
            max_auth_phone = normalize_phone(payload.max_auth_phone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid MAX authorization phone") from exc
    return roles, phone, max_auth_phone


def _new_person(payload: PersonPayload) -> Person:
    roles, phone, max_auth_phone = normalized_person_data(payload)
    person = Person(
        full_name=" ".join(payload.full_name.split()),
        phone=phone,
        max_auth_phone=max_auth_phone,
        active=payload.active,
        bot_access_enabled=(
            payload.active if payload.bot_access_enabled is None else payload.bot_access_enabled
        ),
    )
    person.role_links.extend(PersonRole(role=role) for role in roles)
    return person


def create_app(
    settings: Settings,
    *,
    restart_bot: Callable[[], Awaitable[None]] | None = None,
    restart_server: Callable[[], Awaitable[None]] | None = None,
    status_snapshot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    restore_dispatcher: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    safety_rollback_dispatcher: Callable[[], Awaitable[None]] | None = None,
    safety_delete_dispatcher: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)
    api = MaxApiClient(
        token=settings.max_bot_token.get_secret_value(),  # type: ignore[union-attr]
        base_url=settings.max_api_base_url,
    )
    handler = EchoHandler(
        sessions=sessions,
        api=api,
        required_channel_id=settings.max_required_channel_id,
        required_channel_link=settings.max_required_channel_link,
    )

    async def collect_backup_metadata(database_name: str) -> dict[str, Any]:
        if database_name != "krit_bot":
            return {"critical_counts": {}, "audit_watermark": None}
        async with sessions() as session:
            counts = {
                "persons": int(await session.scalar(select(func.count(Person.id))) or 0),
                "lessons": int(await session.scalar(select(func.count(Lesson.id))) or 0),
                "messages": int(
                    await session.scalar(select(func.count(CommunicationMessage.id))) or 0
                ),
                "administrators": int(
                    await session.scalar(select(func.count(AdminUser.id))) or 0
                ),
            }
            watermark = await session.scalar(select(func.max(AuditEvent.id)))
        return {"critical_counts": counts, "audit_watermark": watermark}

    backup_service = BackupService(
        database_url=settings.database_url,
        database_names=settings.krit_database_names,
        root=settings.backup_root,
        metadata_collector=collect_backup_metadata,
    )
    backup_scheduler = BackupScheduler(
        backup_service,
        interval_seconds=settings.backup_interval_seconds,
        initial_delay_seconds=settings.backup_initial_delay_seconds,
        daily_retention=settings.backup_daily_retention,
        weekly_retention=settings.backup_weekly_retention,
    )

    async def systemd_restore_dispatcher(operation_id: str) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "/usr/local/sbin/krit-restore-dispatch",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await process.communicate(operation_id.encode("ascii"))
        if process.returncode:
            raise RuntimeError("Privileged restore helper could not be started")
        return {"_async": True}

    async def systemd_safety_rollback_dispatcher() -> None:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "/usr/bin/systemctl",
            "start",
            "--no-block",
            "krit-restore-rollback.service",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("Privileged safety rollback helper could not be started")

    async def systemd_safety_delete_dispatcher() -> None:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "/usr/bin/systemctl",
            "start",
            "--no-block",
            "krit-restore-delete.service",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("Privileged safety deletion helper could not be started")

    restore_service = RestoreService(
        root=settings.restore_root,
        database_names=settings.krit_database_names,
        dispatcher=restore_dispatcher or systemd_restore_dispatcher,
        safety_rollback_dispatcher=(
            safety_rollback_dispatcher or systemd_safety_rollback_dispatcher
        ),
        safety_delete_dispatcher=(
            safety_delete_dispatcher
            if safety_delete_dispatcher is not None
            else (None if restore_dispatcher is not None else systemd_safety_delete_dispatcher)
        ),
    )
    backup_service.conflict_checker = restore_service.has_active_operation
    restore_service.conflict_checker = lambda: backup_service.lock.locked()
    polling_task: asyncio.Task[None] | None = None
    syndication_task: asyncio.Task[None] | None = None
    vk_long_poll_task: asyncio.Task[None] | None = None
    learning_notifications_task: asyncio.Task[None] | None = None
    communications_task: asyncio.Task[None] | None = None
    automatic_backup_task: asyncio.Task[None] | None = None
    max_subscription_task: asyncio.Task[bool] | None = None
    syndication_worker: SyndicationWorker | None = None
    invalid_password_hash = password_hash.hash("invalid-password")
    login_rate_limiter = LoginRateLimiter()

    async def require_admin(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> AdminPrincipal:
        configured = settings.jwt_secret
        if configured is None or credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            admin_id, token_version = decode_access_token(
                credentials.credentials,
                configured.get_secret_value(),
            )
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc
        async with sessions() as session:
            admin = await session.get(AdminUser, admin_id)
        if admin is None or not admin.active or admin.auth_version != token_version:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return AdminPrincipal.from_model(admin)

    async def require_management_token(
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> int:
        if principal.must_change_password:
            raise HTTPException(status_code=403, detail="password_change_required")
        return principal.id

    async def ensure_bootstrap_admin() -> None:
        async with sessions() as session:
            username, initial_password, must_change_password = (
                settings.initial_admin_credentials()
            )
            normalized_username = normalize_admin_username(username)
            existing_query = select(AdminUser.id).limit(1)
            if must_change_password:
                existing_query = existing_query.where(
                    AdminUser.username == normalized_username
                )
            if await session.scalar(existing_query) is not None:
                return
            session.add(
                AdminUser(
                    username=normalized_username,
                    full_name=(
                        "Суперадминистратор"
                        if must_change_password
                        else "Администратор"
                    ),
                    password_hash=password_hash.hash(initial_password),
                    role="superadmin",
                    active=True,
                    must_change_password=must_change_password,
                    auth_version=1,
                    is_protected=must_change_password,
                )
            )
            await session.commit()

    def issue_token(admin: AdminUser) -> TokenView:
        configured = settings.jwt_secret
        if configured is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        encoded, expires_in = issue_access_token(admin, configured.get_secret_value())
        return TokenView(
            id=admin.id,
            access_token=encoded,
            expires_in=expires_in,
            username=admin.username,
            full_name=admin.full_name,
            role=admin.role,
            must_change_password=admin.must_change_password,
        )

    async def stop_max_workers() -> None:
        nonlocal polling_task, learning_notifications_task, communications_task
        nonlocal max_subscription_task
        tasks = [
            task
            for task in (
                polling_task,
                learning_notifications_task,
                communications_task,
                max_subscription_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        polling_task = None
        learning_notifications_task = None
        communications_task = None
        max_subscription_task = None

    async def start_max_workers() -> None:
        nonlocal polling_task, learning_notifications_task, communications_task
        nonlocal max_subscription_task
        if settings.bot_mode == "webhook" and (
            max_subscription_task is None or max_subscription_task.done()
        ):
            max_subscription_task = asyncio.create_task(
                run_max_webhook_subscription(api, settings),
                name="max-webhook-subscription",
            )
        learning_notifications_task = asyncio.create_task(
            LearningNotificationWorker(sessions=sessions, api=api).run(),
            name="learning-notifications",
        )
        communications_task = asyncio.create_task(
            run_communications_maintenance(
                sessions, center_timezone=settings.center_timezone
            ),
            name="communications-maintenance",
        )
        if settings.bot_mode == "polling":
            me = await api.get_me()
            log.info(
                "bot_started", bot_id=me.get("user_id"), username=me.get("username")
            )
            polling_task = asyncio.create_task(
                run_polling(
                    api=api,
                    handler=handler,
                    sessions=sessions,
                    poll_timeout=settings.polling_timeout_seconds,
                ),
                name="max-long-polling",
            )

    async def restart_max_workers() -> None:
        await stop_max_workers()
        await start_max_workers()

    async def schedule_process_restart() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    restart_bot_callback = restart_bot or restart_max_workers
    restart_server_callback = restart_server or schedule_process_restart

    async def collect_database_status() -> dict[str, Any]:
        async with sessions() as session:
            if engine.dialect.name == "postgresql":
                database_version = await session.scalar(text("SELECT version()"))
                active_connections = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database()"
                    )
                )
                databases = []
                for database_name in ("krit_bot",):
                    size = await session.scalar(
                        text("SELECT pg_database_size(:database_name)"),
                        {"database_name": database_name},
                    )
                    databases.append(
                        {"name": database_name, "size_bytes": int(size or 0)}
                    )
                revision = await session.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            else:
                database_version = "SQLite " + str(
                    await session.scalar(text("SELECT sqlite_version()"))
                )
                active_connections = 1
                databases = [{"name": "krit_bot", "size_bytes": None}]
                revision = EXPECTED_ALEMBIC_REVISION
        return {
            "available": True,
            "version": database_version,
            "revision": revision,
            "active_connections": int(active_connections or 0),
            "databases": databases,
        }

    async def collect_worker_status() -> dict[str, Any]:
        worker_tasks = [learning_notifications_task, communications_task]
        if settings.bot_mode == "polling":
            worker_tasks.append(polling_task)
        return {
            "available": True,
            "mode": settings.bot_mode,
            "running": bool(worker_tasks)
            and all(task is not None and not task.done() for task in worker_tasks),
            "last_success_at": None,
            "last_error": None,
        }

    async def collect_queue_status() -> dict[str, Any]:
        counts = {"pending": 0, "processing": 0, "failed": 0}
        async with sessions() as session:
            notification_rows = await session.execute(
                select(NotificationJob.status, func.count(NotificationJob.id)).group_by(
                    NotificationJob.status
                )
            )
            syndication_rows = await session.execute(
                select(SyndicationJob.status, func.count(SyndicationJob.id)).group_by(
                    SyndicationJob.status
                )
            )
        for status_name, count in [*notification_rows.all(), *syndication_rows.all()]:
            if status_name in {"pending", "retry", "scheduled"}:
                counts["pending"] += int(count)
            elif status_name == "processing":
                counts["processing"] += int(count)
            elif status_name == "failed":
                counts["failed"] += int(count)
        return {"available": True, **counts}

    async def collect_backup_status() -> dict[str, Any]:
        metadata_files = list(settings.backup_root.glob("*.json"))
        latest = max(metadata_files, key=lambda item: item.stat().st_mtime, default=None)
        latest_data = (
            json.loads(latest.read_text(encoding="utf-8")) if latest is not None else None
        )
        try:
            free_bytes = shutil.disk_usage(settings.backup_root.parent).free
        except OSError:
            free_bytes = None
        return {
            "available": True,
            "last_backup_at": latest_data.get("created_at") if latest_data else None,
            "last_result": "success" if latest_data else "not_started",
            "trusted_count": len(metadata_files),
            "suspicious_count": 0,
            "safety_set_pending": restore_service.pending_safety() is not None,
            "free_bytes": free_bytes,
        }

    async def target_has_business_data() -> bool:
        async with sessions() as session:
            counts = [
                await session.scalar(select(func.count(Person.id))),
                await session.scalar(select(func.count(Lesson.id))),
                await session.scalar(select(func.count(CommunicationMessage.id))),
            ]
            admin_count = await session.scalar(select(func.count(AdminUser.id)))
        return any(int(value or 0) > 0 for value in counts) or int(admin_count or 0) > 1

    status_provider = SystemStatusProvider(
        data_root=Path.cwd(),
        database_collector=collect_database_status,
        worker_collector=collect_worker_status,
        queue_collector=collect_queue_status,
        backup_collector=collect_backup_status,
    )
    status_snapshot_callback = status_snapshot or status_provider.snapshot

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal polling_task, syndication_task, vk_long_poll_task, syndication_worker
        nonlocal learning_notifications_task, communications_task, automatic_backup_task
        await ensure_schema(engine)
        await ensure_bootstrap_admin()
        if settings.automatic_backups_enabled:
            automatic_backup_task = asyncio.create_task(
                backup_scheduler.run(), name="automatic-backups"
            )
        await start_max_workers()
        if settings.vk_syndication_enabled:
            required = {
                "VK_ACCESS_TOKEN": settings.vk_access_token,
                "VK_COMMUNITY_ID": settings.vk_community_id,
                "MAX_CHANNEL_ID": settings.max_channel_id,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise RuntimeError(
                    "VK syndication is enabled but settings are missing: " + ", ".join(missing)
                )
            chat = await api.get_chat(chat_id=settings.max_channel_id)  # type: ignore[arg-type]
            membership = await api.get_membership(
                chat_id=settings.max_channel_id  # type: ignore[arg-type]
            )
            if chat.get("type") != "channel" or chat.get("status") != "active":
                raise RuntimeError("Configured MAX destination is not an active channel")
            if not membership.get("is_admin") or "write" not in (
                membership.get("permissions") or []
            ):
                raise RuntimeError("MAX bot does not have channel write permission")
            vk_api = VkApiClient(
                token=settings.vk_access_token.get_secret_value(),  # type: ignore[union-attr]
                base_url=settings.vk_api_base_url,
                version=settings.vk_api_version,
            )
            syndication_worker = SyndicationWorker(
                sessions=sessions,
                vk=vk_api,
                max_api=api,
                poll_seconds=settings.syndication_poll_seconds,
                max_attempts=settings.syndication_max_attempts,
                download_limit_bytes=settings.syndication_download_limit_bytes,
            )
            syndication_task = asyncio.create_task(
                syndication_worker.run(), name="vk-to-max-syndication"
            )
            vk_long_poll_task = asyncio.create_task(
                VkLongPollWorker(
                    sessions=sessions,
                    vk=vk_api,
                    community_id=settings.vk_community_id,  # type: ignore[arg-type]
                    max_chat_id=settings.max_channel_id,  # type: ignore[arg-type]
                    wait_seconds=settings.vk_long_poll_wait_seconds,
                ).run(),
                name="vk-bots-long-poll",
            )
            log.info(
                "syndication_started",
                community_id=settings.vk_community_id,
                max_chat_id=settings.max_channel_id,
            )
        yield
        if vk_long_poll_task is not None:
            vk_long_poll_task.cancel()
            await asyncio.gather(vk_long_poll_task, return_exceptions=True)
        if syndication_task is not None:
            syndication_task.cancel()
            await asyncio.gather(syndication_task, return_exceptions=True)
        if syndication_worker is not None:
            await syndication_worker.close()
        if automatic_backup_task is not None:
            automatic_backup_task.cancel()
            await asyncio.gather(automatic_backup_task, return_exceptions=True)
            automatic_backup_task = None
        await stop_max_workers()
        await api.close()
        await engine.dispose()

    app = FastAPI(title="KRiT MAX Bot", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(
        create_learning_router(
            sessions,
            require_management_token,
            center_timezone=settings.center_timezone,
        )
    )
    app.include_router(create_backup_router(require_admin, backup_service))
    app.include_router(
        create_restore_router(
            sessions,
            require_admin,
            restore_service,
            target_has_business_data,
        )
    )
    app.include_router(
        create_communications_router(
            sessions,
            require_management_token,
            center_timezone=settings.center_timezone,
        )
    )
    app.include_router(
        create_administration_router(
            sessions,
            require_admin,
            restart_bot=restart_bot_callback,
            restart_server=restart_server_callback,
            status_snapshot=status_snapshot_callback,
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/login", response_model=TokenView)
    async def login(payload: LoginPayload, request: Request) -> TokenView:
        username = normalize_admin_username(payload.username)
        address = request.client.host if request.client is not None else "unknown"
        async with sessions() as session:
            admin = await session.scalar(
                select(AdminUser).where(
                    AdminUser.username == username
                )
            )
        comparison_hash = admin.password_hash if admin is not None else invalid_password_hash
        password_valid = password_hash.verify(payload.password, comparison_hash)
        if admin is not None and admin.active and password_valid:
            login_rate_limiter.record_success(username, address)
            return issue_token(admin)

        retry_after = login_rate_limiter.retry_after(username, address)
        if retry_after is None:
            login_rate_limiter.record_failure(username, address)
            retry_after = login_rate_limiter.retry_after(username, address)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много попыток входа. Повторите позже.",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    @app.get("/api/v1/auth/me")
    async def auth_me(
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> dict[str, Any]:
        return {
            "id": principal.id,
            "username": principal.username,
            "full_name": principal.full_name,
            "role": principal.role,
            "must_change_password": principal.must_change_password,
        }

    @app.post("/api/v1/auth/change-initial-password", response_model=TokenView)
    async def change_initial_password(
        payload: InitialPasswordPayload,
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> TokenView:
        async with sessions() as session:
            admin = await session.get(AdminUser, principal.id)
            if admin is None or not password_hash.verify(
                payload.current_password, admin.password_hash
            ):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
            if not admin.must_change_password:
                raise HTTPException(status_code=409, detail="Password change is not required")
            if password_hash.verify(payload.new_password, admin.password_hash):
                raise HTTPException(status_code=422, detail="New password must be different")
            admin.password_hash = password_hash.hash(payload.new_password)
            admin.must_change_password = False
            admin.auth_version += 1
            admin.updated_at = utcnow()
            session.add(
                AuditEvent(
                    actor_admin_id=admin.id,
                    action="admin_initial_password_changed",
                    entity_type="admin_user",
                    entity_id=admin.id,
                    details={"username": admin.username},
                )
            )
            await session.commit()
            await session.refresh(admin)
            return issue_token(admin)

    @app.get("/api/v1/status", dependencies=[Depends(require_management_token)])
    async def management_status() -> dict[str, str]:
        return {
            "status": "ok",
            "bot_mode": settings.bot_mode,
            "center_timezone": settings.center_timezone,
        }

    @app.get(
        "/api/v1/snapshot",
        response_model=ManagementSnapshot,
        dependencies=[Depends(require_management_token)],
    )
    async def management_snapshot() -> dict[str, Any]:
        async with sessions() as session:
            people = list(
                (
                    await session.scalars(
                        select(Person)
                        .where(Person.archived_at.is_(None))
                        .order_by(Person.full_name)
                    )
                ).all()
            )
            archived_people = list(
                (
                    await session.scalars(
                        select(Person)
                        .where(Person.archived_at.is_not(None))
                        .order_by(Person.archived_at.desc(), Person.full_name)
                    )
                ).all()
            )
            known_ids = (
                select(PersonMaxIdentity.max_user_id)
                .join(Person, Person.id == PersonMaxIdentity.person_id)
                .where(
                    PersonMaxIdentity.max_user_id.is_not(None),
                    Person.archived_at.is_(None),
                )
            )
            attempts = list(
                (
                    await session.scalars(
                        select(AccessAttempt)
                        .where(~AccessAttempt.max_user_id.in_(known_ids))
                        .order_by(AccessAttempt.last_seen_at.desc())
                        .limit(100)
                    )
                ).all()
            )
        return {
            "status": "ok",
            "center_timezone": settings.center_timezone,
            "people": [as_person_view(person) for person in people],
            "archived_people": [as_person_view(person) for person in archived_people],
            "access_attempts": attempts,
        }

    @app.get(
        "/api/v1/people",
        response_model=list[PersonView],
        dependencies=[Depends(require_management_token)],
    )
    async def list_people() -> list[PersonView]:
        async with sessions() as session:
            people = list(
                (
                    await session.scalars(
                        select(Person)
                        .where(Person.archived_at.is_(None))
                        .order_by(Person.full_name)
                    )
                ).all()
            )
            return [as_person_view(person) for person in people]

    @app.get(
        "/api/v1/people-archive",
        response_model=list[PersonView],
        dependencies=[Depends(require_management_token)],
    )
    async def list_archived_people() -> list[PersonView]:
        async with sessions() as session:
            people = list(
                (
                    await session.scalars(
                        select(Person)
                        .where(Person.archived_at.is_not(None))
                        .order_by(Person.archived_at.desc(), Person.full_name)
                    )
                ).all()
            )
            return [as_person_view(person) for person in people]

    async def learning_dependencies(session: Any, person_id: int) -> dict[str, list[int]]:
        now = utcnow()
        teacher_lessons = list(
            (
                await session.scalars(
                    select(Lesson.id).where(
                        Lesson.teacher_id == person_id,
                        Lesson.status.in_(["planned", "scheduled"]),
                        Lesson.end_at > now,
                    )
                )
            ).all()
        )
        default_groups = list(
            (
                await session.scalars(
                    select(StudyGroup.id).where(
                        StudyGroup.default_teacher_id == person_id,
                        StudyGroup.active.is_(True),
                    )
                )
            ).all()
        )
        student_lessons = list(
            (
                await session.scalars(
                    select(LessonParticipant.id)
                    .join(Lesson, Lesson.id == LessonParticipant.lesson_id)
                    .where(
                        LessonParticipant.person_id == person_id,
                        LessonParticipant.attendance_status != "excused",
                        Lesson.status.in_(["planned", "scheduled"]),
                        Lesson.end_at > now,
                    )
                )
            ).all()
        )
        memberships = list(
            (
                await session.scalars(
                    select(GroupMembership.id).where(
                        GroupMembership.person_id == person_id,
                        GroupMembership.start_at <= now,
                        or_(GroupMembership.end_at.is_(None), GroupMembership.end_at > now),
                    )
                )
            ).all()
        )
        return {
            "teacher_lessons": teacher_lessons,
            "default_groups": default_groups,
            "student_lessons": student_lessons,
            "memberships": memberships,
        }

    def dependency_conflict(
        dependencies: dict[str, list[int]], roles: set[str]
    ) -> dict[str, Any] | None:
        relevant = {
            "teacher_lessons": dependencies["teacher_lessons"] if "teacher" in roles else [],
            "default_groups": dependencies["default_groups"] if "teacher" in roles else [],
            "student_lessons": dependencies["student_lessons"] if "student" in roles else [],
            "memberships": dependencies["memberships"] if "student" in roles else [],
        }
        if not any(relevant.values()):
            return None
        return {
            "code": "person_has_future_learning_dependencies",
            "message": "Сначала разрешите будущие обязательства в учебном процессе",
            "dependencies": {
                key: {"count": len(ids), "ids": ids} for key, ids in relevant.items() if ids
            },
        }

    @app.post(
        "/api/v1/people",
        response_model=PersonView,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_management_token)],
    )
    async def create_person(payload: PersonPayload) -> PersonView:
        roles, phone, max_auth_phone = normalized_person_data(payload)
        async with sessions() as session:
            person = Person(
                full_name=" ".join(payload.full_name.split()),
                phone=phone,
                max_auth_phone=max_auth_phone,
                active=payload.active,
                bot_access_enabled=(
                    payload.active
                    if payload.bot_access_enabled is None
                    else payload.bot_access_enabled
                ),
            )
            current_roles = {link.role: link for link in person.role_links}
            for role, link in current_roles.items():
                if role not in roles:
                    await session.delete(link)
            for role in roles:
                if role not in current_roles:
                    person.role_links.append(PersonRole(role=role))
            session.add(person)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Этот телефон для авторизации MAX уже используется",
                ) from exc
            await session.refresh(person)
            return as_person_view(person)

    async def save_person_aggregate(
        session: Any,
        payload: PersonAggregatePayload,
        *,
        person_id: int | None = None,
    ) -> Person:
        if person_id is None:
            person = _new_person(payload.person)
            session.add(person)
            await session.flush()
        else:
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is not None:
                raise HTTPException(404)
            roles, phone, max_auth_phone = normalized_person_data(payload.person)
            person.full_name = " ".join(payload.person.full_name.split())
            person.phone = phone
            person.max_auth_phone = max_auth_phone
            person.active = payload.person.active
            person.bot_access_enabled = (
                payload.person.active
                if payload.person.bot_access_enabled is None
                else payload.person.bot_access_enabled
            )
            current_roles = {link.role: link for link in person.role_links}
            removed_roles = set(current_roles) - set(roles)
            if removed_roles & {"student", "teacher"}:
                dependencies = await learning_dependencies(session, person_id)
                conflict = dependency_conflict(dependencies, removed_roles)
                if conflict:
                    raise HTTPException(status_code=409, detail=conflict)
            for role, link in current_roles.items():
                if role not in roles:
                    await session.delete(link)
            for role in roles:
                if role not in current_roles:
                    person.role_links.append(PersonRole(role=role))
            person.updated_at = utcnow()

        main_roles = set(payload.person.roles)
        if (payload.parent_ids or payload.new_parents) and "student" not in main_roles:
            raise HTTPException(422, "Для связи с родителем нужна роль ученика")
        if (payload.student_ids or payload.new_students) and "parent" not in main_roles:
            raise HTTPException(422, "Для связи с учеником нужна роль родителя")

        await session.execute(
            delete(StudentGuardian).where(
                or_(
                    StudentGuardian.student_id == person.id,
                    StudentGuardian.guardian_id == person.id,
                )
            )
        )
        parent_ids = set(payload.parent_ids)
        student_ids = set(payload.student_ids)
        if person.id in parent_ids or person.id in student_ids:
            raise HTTPException(422, "Нельзя связать карточку с самой собой")

        for related_payload in payload.new_parents:
            if "parent" not in related_payload.roles:
                raise HTTPException(422, "Новая связанная карточка должна иметь роль родителя")
            related = _new_person(related_payload)
            session.add(related)
            await session.flush()
            parent_ids.add(related.id)
        for related_payload in payload.new_students:
            if "student" not in related_payload.roles:
                raise HTTPException(422, "Новая связанная карточка должна иметь роль ученика")
            related = _new_person(related_payload)
            session.add(related)
            await session.flush()
            student_ids.add(related.id)

        related_ids = parent_ids | student_ids
        if related_ids:
            related_rows = list(
                (
                    await session.scalars(
                        select(Person).where(
                            Person.id.in_(related_ids),
                            Person.archived_at.is_(None),
                        )
                    )
                ).all()
            )
            if {entry.id for entry in related_rows} != related_ids:
                raise HTTPException(422, "Одна из связанных карточек недоступна")
            role_rows = set(
                (
                    await session.execute(
                        select(PersonRole.person_id, PersonRole.role).where(
                            PersonRole.person_id.in_(related_ids)
                        )
                    )
                ).all()
            )
            if any((related_id, "parent") not in role_rows for related_id in parent_ids):
                raise HTTPException(422, "Связанная карточка не является родителем")
            if any((related_id, "student") not in role_rows for related_id in student_ids):
                raise HTTPException(422, "Связанная карточка не является учеником")

        session.add_all(
            [
                *(StudentGuardian(student_id=person.id, guardian_id=value) for value in parent_ids),
                *(
                    StudentGuardian(student_id=value, guardian_id=person.id)
                    for value in student_ids
                ),
            ]
        )
        await session.flush()
        return person

    @app.post(
        "/api/v1/people/aggregate",
        response_model=PersonView,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_management_token)],
    )
    async def create_person_aggregate(payload: PersonAggregatePayload) -> PersonView:
        async with sessions() as session:
            try:
                person = await save_person_aggregate(session, payload)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(
                    409, "Телефон для входа в MAX уже используется"
                ) from exc
            await session.refresh(
                person,
                attribute_names=["role_links", "guardian_links", "student_links", "max_identity"],
            )
            return as_person_view(person)

    @app.put(
        "/api/v1/people/{person_id}/aggregate",
        response_model=PersonView,
        dependencies=[Depends(require_management_token)],
    )
    async def update_person_aggregate(
        person_id: int, payload: PersonAggregatePayload
    ) -> PersonView:
        async with sessions() as session:
            try:
                person = await save_person_aggregate(session, payload, person_id=person_id)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(
                    409, "Телефон для входа в MAX уже используется"
                ) from exc
            await session.refresh(
                person,
                attribute_names=["role_links", "guardian_links", "student_links", "max_identity"],
            )
            return as_person_view(person)

    @app.post(
        "/api/v1/people/{student_id}/guardians/{guardian_id}",
        response_model=PersonView,
        dependencies=[Depends(require_management_token)],
    )
    async def link_student_guardian(student_id: int, guardian_id: int) -> PersonView:
        if student_id == guardian_id:
            raise HTTPException(status_code=422, detail="Нельзя связать карточку с самой собой")
        async with sessions() as session:
            student = await session.get(Person, student_id)
            guardian = await session.get(Person, guardian_id)
            if (
                student is None
                or guardian is None
                or student.archived_at is not None
                or guardian.archived_at is not None
            ):
                raise HTTPException(status_code=404)
            if "student" not in {link.role for link in student.role_links}:
                raise HTTPException(status_code=422, detail="Первая карточка не является учеником")
            if "parent" not in {link.role for link in guardian.role_links}:
                raise HTTPException(status_code=422, detail="Вторая карточка не является родителем")
            link = await session.get(StudentGuardian, (student_id, guardian_id))
            if link is None:
                student.guardian_links.append(StudentGuardian(guardian=guardian))
                await session.commit()
                await session.refresh(student)
            return as_person_view(student)

    @app.delete(
        "/api/v1/people/{student_id}/guardians/{guardian_id}",
        dependencies=[Depends(require_management_token)],
    )
    async def unlink_student_guardian(student_id: int, guardian_id: int) -> dict[str, bool]:
        async with sessions() as session:
            link = await session.get(StudentGuardian, (student_id, guardian_id))
            if link is None:
                raise HTTPException(status_code=404)
            await session.delete(link)
            await session.commit()
            return {"deleted": True}

    @app.put(
        "/api/v1/people/{person_id}",
        response_model=PersonView,
        dependencies=[Depends(require_management_token)],
    )
    async def update_person(person_id: int, payload: PersonPayload) -> PersonView:
        roles, phone, max_auth_phone = normalized_person_data(payload)
        async with sessions() as session:
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is not None:
                raise HTTPException(status_code=404)
            person.full_name = " ".join(payload.full_name.split())
            person.phone = phone
            person.max_auth_phone = max_auth_phone
            if person.guardian_links and "student" not in roles:
                raise HTTPException(status_code=409, detail="У клиента есть связанные родители")
            if person.student_links and "parent" not in roles:
                raise HTTPException(status_code=409, detail="У клиента есть связанные ученики")
            current_roles = {link.role: link for link in person.role_links}
            removed_roles = set(current_roles) - set(roles)
            if removed_roles & {"student", "teacher"}:
                dependencies = await learning_dependencies(session, person_id)
                conflict = dependency_conflict(dependencies, removed_roles)
                if conflict:
                    raise HTTPException(status_code=409, detail=conflict)
            for role, link in current_roles.items():
                if role not in roles:
                    await session.delete(link)
            for role in roles:
                if role not in current_roles:
                    person.role_links.append(PersonRole(role=role))
            person.active = payload.active
            person.bot_access_enabled = (
                payload.active
                if payload.bot_access_enabled is None
                else payload.bot_access_enabled
            )
            person.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Этот телефон для авторизации MAX уже используется",
                ) from exc
            await session.refresh(person)
            return as_person_view(person)

    @app.post(
        "/api/v1/people/{person_id}/archive",
        response_model=PersonView,
    )
    async def archive_person(
        person_id: int,
        payload: ArchivePersonPayload | None = None,
        admin_id: int = Depends(require_management_token),
    ) -> PersonView:
        async with sessions() as session:
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is not None:
                raise HTTPException(status_code=404)
            dependencies = await learning_dependencies(session, person_id)
            teacher_conflict = dependency_conflict(dependencies, {"teacher"})
            if teacher_conflict:
                raise HTTPException(status_code=409, detail=teacher_conflict)
            student_conflict = dependency_conflict(dependencies, {"student"})
            resolve_student = bool(payload and payload.resolve_future_student_dependencies)
            if student_conflict and not resolve_student:
                student_conflict["can_resolve_student_dependencies"] = True
                raise HTTPException(status_code=409, detail=student_conflict)

            archived_at = utcnow()
            if student_conflict:
                membership_ids = dependencies["memberships"]
                participant_ids = dependencies["student_lessons"]
                if membership_ids:
                    await session.execute(
                        update(GroupMembership)
                        .where(GroupMembership.id.in_(membership_ids))
                        .values(end_at=archived_at)
                    )
                if participant_ids:
                    await session.execute(
                        update(LessonParticipant)
                        .where(LessonParticipant.id.in_(participant_ids))
                        .values(
                            attendance_status="excused",
                            cancelled_at=archived_at,
                            cancelled_by="administrator",
                            cancelled_by_admin_id=admin_id,
                            cancellation_reason="Архивирование карточки клиента",
                        )
                    )
                    lesson_ids = list(
                        (
                            await session.scalars(
                                select(LessonParticipant.lesson_id).where(
                                    LessonParticipant.id.in_(participant_ids)
                                )
                            )
                        ).all()
                    )
                    await session.execute(
                        update(NotificationJob)
                        .where(
                            NotificationJob.lesson_id.in_(lesson_ids),
                            NotificationJob.status.in_(["pending", "retry"]),
                            or_(
                                NotificationJob.recipient_person_id == person_id,
                                NotificationJob.dedupe_key.like(
                                    f"lesson:%:reminder:%student:{person_id}"
                                ),
                            ),
                        )
                        .values(status="cancelled", updated_at=archived_at)
                    )
                session.add(
                    AuditEvent(
                        actor_admin_id=admin_id,
                        action="person.future_learning_resolved",
                        entity_type="person",
                        entity_id=person_id,
                        details={
                            "memberships": membership_ids,
                            "lesson_participants": participant_ids,
                        },
                    )
                )
            person.archived_at = archived_at
            person.updated_at = archived_at
            await session.commit()
            await session.refresh(person)
            return as_person_view(person)

    @app.post(
        "/api/v1/people/{person_id}/restore",
        response_model=PersonView,
        dependencies=[Depends(require_management_token)],
    )
    async def restore_person(person_id: int) -> PersonView:
        async with sessions() as session:
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is None:
                raise HTTPException(status_code=404)
            if person.max_auth_phone is not None:
                collision = await session.scalar(
                    select(Person.id).where(
                        Person.id != person.id,
                        Person.max_auth_phone == person.max_auth_phone,
                        Person.active.is_(True),
                        Person.archived_at.is_(None),
                    )
                )
                if collision is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "max_auth_phone_conflict",
                            "message": (
                                "Телефон для входа в MAX уже используется другим клиентом"
                            ),
                        },
                    )
            person.archived_at = None
            person.updated_at = utcnow()
            await session.commit()
            await session.refresh(person)
            return as_person_view(person)

    @app.delete(
        "/api/v1/people/{person_id}",
        dependencies=[Depends(require_management_token)],
    )
    async def delete_person(person_id: int) -> dict[str, bool]:
        async with sessions() as session:
            person = await session.get(Person, person_id)
            if person is None or person.archived_at is None:
                raise HTTPException(
                    status_code=409,
                    detail="Перед удалением перенесите клиента в архив",
                )
            await session.delete(person)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Клиента нельзя удалить: с ним связана история учебного процесса. "
                        "Оставьте карточку в архиве."
                    ),
                ) from exc
            return {"deleted": True}

    @app.get(
        "/api/v1/access-attempts",
        response_model=list[AccessAttemptView],
        dependencies=[Depends(require_management_token)],
    )
    async def list_access_attempts() -> list[AccessAttempt]:
        async with sessions() as session:
            known_ids = (
                select(PersonMaxIdentity.max_user_id)
                .join(Person, Person.id == PersonMaxIdentity.person_id)
                .where(
                    PersonMaxIdentity.max_user_id.is_not(None),
                    Person.archived_at.is_(None),
                )
            )
            return list(
                (
                    await session.scalars(
                        select(AccessAttempt)
                        .where(~AccessAttempt.max_user_id.in_(known_ids))
                        .order_by(AccessAttempt.last_seen_at.desc())
                        .limit(100)
                    )
                ).all()
            )

    @app.post("/webhooks/max", status_code=status.HTTP_200_OK)
    async def max_webhook(
        request: Request,
        provided_secret: str | None = Header(default=None, alias="X-Max-Bot-Api-Secret"),
    ) -> dict[str, bool]:
        expected = settings.max_webhook_secret
        if (
            expected is None
            or provided_secret is None
            or not secrets.compare_digest(provided_secret, expected.get_secret_value())
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        update: dict[str, Any] = await request.json()
        chat = update.get("chat") if isinstance(update.get("chat"), dict) else {}
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        recipient = message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
        candidate_id = update.get("chat_id") or chat.get("chat_id") or recipient.get("chat_id")
        if isinstance(candidate_id, int):
            details = {
                "update_type": update.get("update_type"),
                "type": chat.get("type") or recipient.get("chat_type"),
                "title": chat.get("title") or recipient.get("title"),
                "link": chat.get("link"),
            }
            async with sessions() as session:
                key = f"max_chat_candidate:{candidate_id}"
                state = await session.get(BotState, key)
                if state is None:
                    session.add(BotState(key=key, value=json.dumps(details, ensure_ascii=False)))
                else:
                    state.value = json.dumps(details, ensure_ascii=False)
                    state.updated_at = utcnow()
                await session.commit()
            log.info(
                "max_chat_event",
                chat_id=candidate_id,
                chat_type=details["type"],
                title=details["title"],
                update_type=details["update_type"],
            )
        await handler.handle(update)
        return {"ok": True}

    @app.post("/webhooks/vk", response_class=PlainTextResponse)
    async def vk_webhook(request: Request) -> str:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        expected_secret = settings.vk_callback_secret
        expected_group = settings.vk_community_id
        if expected_secret is None or expected_group is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        provided_secret = str(payload.get("secret") or "")
        if not secrets.compare_digest(provided_secret, expected_secret.get_secret_value()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            group_id = int(payload.get("group_id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        if group_id != expected_group:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        event_type = str(payload.get("type") or "")
        if event_type == "confirmation":
            confirmation = settings.vk_callback_confirmation
            if confirmation is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
            return confirmation.get_secret_value()
        if event_type != "wall_post_new":
            return "ok"
        if settings.max_channel_id is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        event_object = payload.get("object")
        if isinstance(event_object, dict) and isinstance(event_object.get("object"), dict):
            event_object = event_object["object"]
        if not isinstance(event_object, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        try:
            post_id = int(event_object.get("id", event_object.get("post_id")))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        owner_id = event_object.get("owner_id")
        try:
            if owner_id is not None and int(owner_id) != -expected_group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        stored_event = dict(payload)
        stored_event.pop("secret", None)
        job_id, created = await register_vk_event(
            sessions,
            community_id=group_id,
            post_id=post_id,
            max_chat_id=settings.max_channel_id,
            event_id=str(payload.get("event_id")) if payload.get("event_id") else None,
            raw_event=stored_event,
        )
        log.info(
            "vk_event_registered",
            community_id=group_id,
            post_id=post_id,
            job_id=job_id,
            duplicate=not created,
        )
        return "ok"

    return app
