from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .learning_models import PersonMaxIdentity


log = structlog.get_logger()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


EXPECTED_ALEMBIC_REVISION = "20261021_prompt_cleanup_v12"
ADMIN_ROLES = frozenset({"superadmin", "director", "administrator"})
PROTECTED_ADMIN_USERNAME = "choose_goose"


class Person(Base):
    __tablename__ = "persons"
    __table_args__ = (
        Index(
            "uq_persons_active_max_auth_phone",
            "max_auth_phone",
            unique=True,
            sqlite_where=text("max_auth_phone IS NOT NULL AND active = 1 AND archived_at IS NULL"),
            postgresql_where=text(
                "max_auth_phone IS NOT NULL AND active IS TRUE AND archived_at IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(250), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    max_auth_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    bot_access_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    role_links: Mapped[list[PersonRole]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    guardian_links: Mapped[list[StudentGuardian]] = relationship(
        foreign_keys="StudentGuardian.student_id",
        back_populates="student",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    student_links: Mapped[list[StudentGuardian]] = relationship(
        foreign_keys="StudentGuardian.guardian_id",
        back_populates="guardian",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    max_identity: Mapped[PersonMaxIdentity | None] = relationship(
        "PersonMaxIdentity", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )


class PersonRole(Base):
    __tablename__ = "person_roles"

    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True, index=True)


class StudentGuardian(Base):
    __tablename__ = "student_guardians"
    __table_args__ = (CheckConstraint("student_id <> guardian_id"),)

    student_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    guardian_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    student: Mapped[Person] = relationship(
        foreign_keys=[student_id], back_populates="guardian_links", lazy="joined"
    )
    guardian: Mapped[Person] = relationship(
        foreign_keys=[guardian_id], back_populates="student_links", lazy="joined"
    )


class AccessAttempt(Base):
    __tablename__ = "access_attempts"

    max_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(250))
    username: Mapped[str | None] = mapped_column(String(250))
    verified_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('superadmin','director','administrator')",
            name="ck_admin_users_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(250), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="superadmin")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BackupExportReceipt(Base):
    __tablename__ = "backup_export_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','success','failed')",
            name="ck_backup_export_receipts_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    installation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    principal_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyndicationJob(Base):
    __tablename__ = "syndication_jobs"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "vk_community_id",
            "vk_post_id",
            "max_chat_id",
            name="uq_syndication_source_post_destination",
        ),
        CheckConstraint(
            "status IN ('pending','processing','published','retry','failed','skipped')",
            name="ck_syndication_job_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="vk")
    vk_community_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vk_post_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vk_event_id: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_event: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_message_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def build_engine(database_url: str) -> AsyncEngine:
    if database_url.startswith("sqlite"):
        raw_path = database_url.split("///", 1)[-1]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def verify_schema_current(engine: AsyncEngine) -> None:
    """Reject production startup unless the database is at the packaged Alembic head."""
    async with engine.connect() as connection:
        try:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception as exc:
            raise RuntimeError(
                "Схема базы данных не подготовлена. Выполните krit-migrate перед запуском."
            ) from exc
    if revision != EXPECTED_ALEMBIC_REVISION:
        raise RuntimeError(
            "Схема базы данных устарела: "
            f"ожидается {EXPECTED_ALEMBIC_REVISION}, получено {revision or 'нет версии'}. "
            "Остановите сервис и выполните krit-migrate."
        )


async def ensure_schema(engine: AsyncEngine) -> None:
    """Bootstrap isolated SQLite tests; production DDL belongs exclusively to Alembic."""
    if engine.dialect.name != "sqlite":
        await verify_schema_current(engine)
        return
    from . import communication_models as _communication_models  # noqa: F401
    from . import learning_models as _learning_models  # noqa: F401
    from . import online_models as _online_models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def is_authorized(session: AsyncSession, max_user_id: int) -> bool:
    from .learning_models import PersonMaxIdentity

    result = await session.scalar(
        select(PersonMaxIdentity.person_id)
        .join(Person, Person.id == PersonMaxIdentity.person_id)
        .where(
            PersonMaxIdentity.max_user_id == max_user_id,
            Person.active.is_(True),
            Person.bot_access_enabled.is_(True),
            Person.archived_at.is_(None),
        )
    )
    return result is not None


async def bind_max_user_by_phone(
    session: AsyncSession, *, phone: str, max_user_id: int
) -> Literal["linked", "already_linked", "not_found", "belongs_to_another_user"]:
    from .learning_models import PersonMaxIdentity

    existing_identity = await session.scalar(
        select(PersonMaxIdentity).where(PersonMaxIdentity.max_user_id == max_user_id)
    )
    if existing_identity is not None and existing_identity.verified_phone != phone:
        return "belongs_to_another_user"
    identity_person_id = await session.scalar(
        select(PersonMaxIdentity.person_id).where(PersonMaxIdentity.verified_phone == phone)
    )
    statement = (
        select(Person)
        .where(
            or_(Person.id == identity_person_id, Person.max_auth_phone == phone),
            Person.active.is_(True),
            Person.archived_at.is_(None),
        )
        .with_for_update()
    )
    person = await session.scalar(statement)
    if person is None:
        return "not_found"
    identity = await session.get(PersonMaxIdentity, person.id)
    if identity is not None and identity.max_user_id == max_user_id:
        return "already_linked"
    if identity is None:
        identity = PersonMaxIdentity(
            person_id=person.id,
            verified_phone=phone,
            max_user_id=max_user_id,
            verified_at=utcnow(),
        )
        session.add(identity)
    elif identity.max_user_id not in {None, max_user_id}:
        return "belongs_to_another_user"
    else:
        identity.max_user_id = max_user_id
        identity.verified_phone = phone
        identity.verified_at = utcnow()
        identity.updated_at = utcnow()
    # Person.max_user_id is retained as a transitional database column only.
    # Runtime authorization and delivery use PersonMaxIdentity exclusively.
    person.updated_at = utcnow()
    await session.flush()
    return "linked"


async def register_access_attempt(
    session: AsyncSession,
    *,
    max_user_id: int,
    display_name: str | None,
    username: str | None,
    verified_phone: str | None = None,
) -> None:
    now = utcnow()
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = pg_insert(AccessAttempt).values(
            max_user_id=max_user_id,
            display_name=display_name,
            username=username,
            verified_phone=verified_phone,
            attempts=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AccessAttempt.max_user_id],
            set_={
                "display_name": display_name,
                "username": username,
                "verified_phone": (
                    verified_phone
                    if verified_phone is not None
                    else AccessAttempt.verified_phone
                ),
                "attempts": AccessAttempt.attempts + 1,
                "last_seen_at": now,
            },
        )
        await session.execute(statement)
        return

    item = await session.get(AccessAttempt, max_user_id)
    if item is None:
        session.add(
            AccessAttempt(
                max_user_id=max_user_id,
                display_name=display_name,
                username=username,
                verified_phone=verified_phone,
            )
        )
    else:
        item.display_name = display_name
        item.username = username
        if verified_phone is not None:
            item.verified_phone = verified_phone
        item.attempts += 1
        item.last_seen_at = now


async def purge_archived_person(session: AsyncSession, person: Person) -> None:
    """Permanently remove an archived person and every person-owned record."""
    from .learning_models import PersonMaxIdentity

    if person.archived_at is None:
        raise ValueError("Перед удалением перенесите клиента в архив")

    linked_max_user_ids = set(
        await session.scalars(
            select(PersonMaxIdentity.max_user_id).where(
                PersonMaxIdentity.person_id == person.id
            )
        )
    )
    if person.max_user_id is not None:
        linked_max_user_ids.add(person.max_user_id)

    for table in reversed(Base.metadata.sorted_tables):
        if table is Person.__table__:
            continue
        person_foreign_keys = [
            foreign_key
            for foreign_key in table.foreign_keys
            if foreign_key.target_fullname == "persons.id"
        ]
        delete_columns = [
            foreign_key.parent
            for foreign_key in person_foreign_keys
            if (foreign_key.ondelete or "RESTRICT").upper() != "SET NULL"
        ]
        if delete_columns:
            await session.execute(
                delete(table).where(
                    or_(*(column == person.id for column in delete_columns))
                )
            )
        set_null_columns = [
            foreign_key.parent
            for foreign_key in person_foreign_keys
            if (foreign_key.ondelete or "").upper() == "SET NULL"
        ]
        for column in set_null_columns:
            await session.execute(
                update(table).where(column == person.id).values({column.key: None})
            )

    attempt_conditions = []
    if linked_max_user_ids:
        attempt_conditions.append(AccessAttempt.max_user_id.in_(linked_max_user_ids))
    linked_phones = {person.phone, person.max_auth_phone} - {None, ""}
    if linked_phones:
        attempt_conditions.append(AccessAttempt.verified_phone.in_(linked_phones))
    if attempt_conditions:
        await session.execute(
            delete(AccessAttempt).where(or_(*attempt_conditions))
        )
    await session.execute(delete(Person).where(Person.id == person.id))


async def claim_message(session: AsyncSession, message_id: str) -> bool:
    if await session.get(ProcessedMessage, message_id) is not None:
        return False
    session.add(ProcessedMessage(message_id=message_id))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    except Exception:
        await session.rollback()
        log.exception("message_claim_failed", message_id=message_id)
        raise
    return True


async def get_marker(session: AsyncSession) -> int | None:
    state = await session.get(BotState, "polling_marker")
    return int(state.value) if state and state.value else None


async def save_marker(session: AsyncSession, marker: int | None) -> None:
    if marker is None:
        return
    state = await session.get(BotState, "polling_marker")
    if state is None:
        session.add(BotState(key="polling_marker", value=str(marker)))
    else:
        state.value = str(marker)
        state.updated_at = utcnow()


def normalize_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError("Invalid phone number")
    return "+" + digits
