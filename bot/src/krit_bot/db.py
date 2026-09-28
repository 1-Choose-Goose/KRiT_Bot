from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(250), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def is_authorized(session: AsyncSession, max_user_id: int) -> bool:
    result = await session.scalar(
        select(Person.id).where(
            Person.max_user_id == max_user_id,
            Person.active.is_(True),
            Person.archived_at.is_(None),
        )
    )
    return result is not None


async def bind_max_user_by_phone(
    session: AsyncSession, *, phone: str, max_user_id: int
) -> Literal["linked", "already_linked", "not_found", "belongs_to_another_user"]:
    existing = await session.scalar(select(Person).where(Person.max_user_id == max_user_id))
    if existing is not None and existing.phone != phone:
        return "belongs_to_another_user"
    statement = (
        select(Person)
        .where(
            Person.phone == phone,
            Person.active.is_(True),
            Person.archived_at.is_(None),
        )
        .with_for_update()
    )
    person = await session.scalar(statement)
    if person is None:
        return "not_found"
    if person.max_user_id == max_user_id:
        return "already_linked"
    if person.max_user_id is not None:
        return "belongs_to_another_user"
    person.max_user_id = max_user_id
    person.updated_at = utcnow()
    await session.flush()
    return "linked"


async def register_access_attempt(
    session: AsyncSession, *, max_user_id: int, display_name: str | None, username: str | None
) -> None:
    now = utcnow()
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = pg_insert(AccessAttempt).values(
            max_user_id=max_user_id,
            display_name=display_name,
            username=username,
            attempts=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AccessAttempt.max_user_id],
            set_={
                "display_name": display_name,
                "username": username,
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
            )
        )
    else:
        item.display_name = display_name
        item.username = username
        item.attempts += 1
        item.last_seen_at = now


async def claim_message(session: AsyncSession, message_id: str) -> bool:
    if await session.get(ProcessedMessage, message_id) is not None:
        return False
    session.add(ProcessedMessage(message_id=message_id))
    try:
        await session.flush()
    except Exception:
        await session.rollback()
        return False
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
