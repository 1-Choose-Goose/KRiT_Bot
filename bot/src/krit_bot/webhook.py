from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from .communications import create_communications_router, run_communications_maintenance
from .config import Settings
from .db import (
    AccessAttempt,
    AdminUser,
    BotState,
    Person,
    PersonRole,
    StudentGuardian,
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
from .syndication import (
    SyndicationWorker,
    VkApiClient,
    VkLongPollWorker,
    register_vk_event,
)

log = structlog.get_logger()
bearer = HTTPBearer(auto_error=False)
ROLES = {"student", "parent", "teacher"}
password_hash = PasswordHash.recommended()


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


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class TokenView(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


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


def create_app(settings: Settings) -> FastAPI:
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
    polling_task: asyncio.Task[None] | None = None
    syndication_task: asyncio.Task[None] | None = None
    vk_long_poll_task: asyncio.Task[None] | None = None
    learning_notifications_task: asyncio.Task[None] | None = None
    communications_task: asyncio.Task[None] | None = None
    syndication_worker: SyndicationWorker | None = None
    invalid_password_hash = password_hash.hash("invalid-password")

    async def require_management_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> int:
        configured = settings.jwt_secret
        if configured is None or credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            payload = jwt.decode(
                credentials.credentials,
                configured.get_secret_value(),
                algorithms=["HS256"],
                issuer="krit-bot",
                options={"require": ["exp", "sub", "iss"]},
            )
            admin_id = int(payload["sub"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc
        async with sessions() as session:
            admin = await session.get(AdminUser, admin_id)
        if admin is None or not admin.active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return admin_id

    async def ensure_bootstrap_admin() -> None:
        async with sessions() as session:
            if await session.scalar(select(AdminUser.id).limit(1)) is not None:
                return
            session.add(
                AdminUser(
                    username=settings.bootstrap_admin_username,
                    password_hash=password_hash.hash(
                        settings.bootstrap_admin_password.get_secret_value()
                    ),
                    active=True,
                )
            )
            await session.commit()

    def issue_token(admin: AdminUser) -> TokenView:
        configured = settings.jwt_secret
        if configured is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        lifetime = timedelta(hours=8)
        now = datetime.now(UTC)
        encoded = jwt.encode(
            {"sub": str(admin.id), "iss": "krit-bot", "iat": now, "exp": now + lifetime},
            configured.get_secret_value(),
            algorithm="HS256",
        )
        return TokenView(access_token=encoded, expires_in=int(lifetime.total_seconds()))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal polling_task, syndication_task, vk_long_poll_task, syndication_worker
        nonlocal learning_notifications_task, communications_task
        await ensure_schema(engine)
        await ensure_bootstrap_admin()
        try:
            if await ensure_max_webhook_subscription(api, settings):
                log.info(
                    "max_webhook_subscription_verified",
                    url=settings.max_webhook_url,
                    update_types=WEBHOOK_UPDATE_TYPES,
                )
        except Exception as exc:
            # MAX must not take down the management API when its subscription
            # endpoint is temporarily unavailable. The existing subscription
            # remains usable and the next application start retries the check.
            log.warning("max_webhook_subscription_failed", error=str(exc))
        learning_notifications_task = asyncio.create_task(
            LearningNotificationWorker(sessions=sessions, api=api).run(),
            name="learning-notifications",
        )
        communications_task = asyncio.create_task(
            run_communications_maintenance(sessions, center_timezone=settings.center_timezone),
            name="communications-maintenance",
        )
        if settings.bot_mode == "polling":
            me = await api.get_me()
            log.info("bot_started", bot_id=me.get("user_id"), username=me.get("username"))
            polling_task = asyncio.create_task(
                run_polling(
                    api=api,
                    handler=handler,
                    sessions=sessions,
                    poll_timeout=settings.polling_timeout_seconds,
                ),
                name="max-long-polling",
            )
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
        if polling_task is not None:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        if learning_notifications_task is not None:
            learning_notifications_task.cancel()
            await asyncio.gather(learning_notifications_task, return_exceptions=True)
        if communications_task is not None:
            communications_task.cancel()
            await asyncio.gather(communications_task, return_exceptions=True)
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
    app.include_router(
        create_communications_router(
            sessions,
            require_management_token,
            center_timezone=settings.center_timezone,
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/login", response_model=TokenView)
    async def login(payload: LoginPayload) -> TokenView:
        async with sessions() as session:
            admin = await session.scalar(
                select(AdminUser).where(AdminUser.username == payload.username)
            )
        comparison_hash = admin.password_hash if admin is not None else invalid_password_hash
        password_valid = password_hash.verify(payload.password, comparison_hash)
        if admin is None or not admin.active or not password_valid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
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
