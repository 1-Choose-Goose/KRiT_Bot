"""Registration journals stored alongside the management people database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Person, utcnow

Kind = Literal["incoming", "outgoing", "contract"]
PREFIX = {"incoming": "ВХ", "outgoing": "ИСХ", "contract": "ДГ"}


class RegistrationCounter(Base):
    __tablename__ = "registration_counters"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False)


class RegistrationRecord(Base):
    __tablename__ = "registration_records"
    __table_args__ = (UniqueConstraint("kind", "year", "sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    registration_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    registration_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_date: Mapped[date | None] = mapped_column(Date)
    document_number: Mapped[str | None] = mapped_column(String(100))
    counterparty: Mapped[str] = mapped_column(String(250), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecordInput(BaseModel):
    registration_date: date
    document_date: date | None = None
    document_number: str | None = Field(default=None, max_length=100)
    counterparty: str = Field(min_length=1, max_length=250)
    subject: str = Field(min_length=1, max_length=500)
    person_id: int | None = None
    details: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def clean(self) -> RecordInput:
        self.counterparty = self.counterparty.strip()
        self.subject = self.subject.strip()
        if not self.counterparty or not self.subject:
            raise ValueError("Укажите корреспондента и содержание документа")
        if any(len(str(value)) > 1000 for value in self.details.values()):
            raise ValueError("Слишком длинное дополнительное поле")
        if len(self.details) > 20:
            raise ValueError("Слишком много дополнительных полей")
        return self


class RecordView(RecordInput):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: Kind
    registration_number: str
    created_at: datetime
    updated_at: datetime


def create_registration_router(
    sessions: async_sessionmaker[AsyncSession],
    require_management_token: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/registration",
        dependencies=[Depends(require_management_token)],
    )

    async def validate_person(session: AsyncSession, person_id: int | None) -> None:
        if person_id is not None and await session.get(Person, person_id) is None:
            raise HTTPException(422, "Ученик не найден")

    @router.get("/{kind}", response_model=list[RecordView])
    async def list_records(kind: Kind) -> list[RegistrationRecord]:
        async with sessions() as session:
            result = await session.scalars(
                select(RegistrationRecord)
                .where(RegistrationRecord.kind == kind)
                .order_by(RegistrationRecord.registration_date.desc(), RegistrationRecord.id.desc())
            )
            return list(result)

    @router.post("/{kind}", response_model=RecordView, status_code=201)
    async def create_record(kind: Kind, payload: RecordInput) -> RegistrationRecord:
        year = payload.registration_date.year
        async with sessions() as session:
            await validate_person(session, payload.person_id)
            # The counter update is atomic in both PostgreSQL and modern SQLite.
            sequence = await session.scalar(
                text(
                    "INSERT INTO registration_counters (kind, year, next_value) "
                    "VALUES (:kind, :year, 1) "
                    "ON CONFLICT (kind, year) DO UPDATE SET "
                    "next_value = registration_counters.next_value + 1 "
                    "RETURNING next_value"
                ),
                {"kind": kind, "year": year},
            )
            record = RegistrationRecord(
                kind=kind,
                year=year,
                sequence=sequence,
                registration_number=f"{PREFIX[kind]}-{year}-{sequence:04d}",
                **payload.model_dump(),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    @router.put("/{record_id}", response_model=RecordView)
    async def update_record(record_id: int, payload: RecordInput) -> RegistrationRecord:
        async with sessions() as session:
            record = await session.get(RegistrationRecord, record_id)
            if record is None:
                raise HTTPException(404, "Запись не найдена")
            if payload.registration_date.year != record.year:
                raise HTTPException(422, "Год регистрации нельзя изменить")
            await validate_person(session, payload.person_id)
            for key, value in payload.model_dump().items():
                setattr(record, key, value)
            record.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Не удалось сохранить запись") from exc
            await session.refresh(record)
            return record

    return router
