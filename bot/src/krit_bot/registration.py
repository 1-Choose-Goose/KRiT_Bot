"""Registration journals stored alongside the management people database."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

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
PREFIX = {"incoming": "вх", "outgoing": "исх", "contract": "дг"}


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
    model_config = ConfigDict(extra="forbid")

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


class RecordUpdate(RecordInput):
    registration_number: str | None = Field(default=None, min_length=1, max_length=32)
    registration_date: date | None = None


class RecordView(RecordInput):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: Kind
    registration_number: str
    registration_date: date
    created_at: datetime
    updated_at: datetime


def create_registration_router(
    sessions: async_sessionmaker[AsyncSession],
    require_management_token: Callable[..., Any],
    require_superadmin_token: Callable[..., Any],
    *,
    center_timezone: str = "Asia/Yekaterinburg",
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/registration",
        dependencies=[Depends(require_management_token)],
    )

    async def validate_person(session: AsyncSession, person_id: int | None) -> None:
        if person_id is not None and await session.get(Person, person_id) is None:
            raise HTTPException(422, "Ученик не найден")

    def validate_document(kind: Kind, payload: RecordInput, *, creating: bool) -> None:
        if kind != "incoming" and payload.document_number:
            raise HTTPException(422, "Регистрационный номер присваивается программой")
        if creating and kind != "incoming" and payload.document_date is None:
            raise HTTPException(422, "Укажите дату подписания документа")
        if kind == "incoming":
            received = payload.details.get("received_date", "").strip()
            try:
                datetime.strptime(received, "%d.%m.%Y")
            except ValueError as exc:
                raise HTTPException(422, "Укажите дату получения как ДД.ММ.ГГГГ") from exc

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
        validate_document(kind, payload, creating=True)
        registration_date = datetime.now(ZoneInfo(center_timezone)).date()
        year = registration_date.year
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
                registration_number=f"{PREFIX[kind]}-{year}/{sequence}",
                registration_date=registration_date,
                **payload.model_dump(),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    @router.put("/{record_id}", response_model=RecordView)
    async def update_record(
        record_id: int, payload: RecordUpdate,
        admin: Any = Depends(require_superadmin_token),  # noqa: B008
    ) -> RegistrationRecord:
        async with sessions() as session:
            record = await session.get(RegistrationRecord, record_id)
            if record is None:
                raise HTTPException(404, "Запись не найдена")
            validate_document(record.kind, payload, creating=False)
            await validate_person(session, payload.person_id)
            editable = payload.model_dump(exclude={"registration_number", "registration_date"})
            for key, value in editable.items():
                setattr(record, key, value)
            if payload.registration_number is not None:
                new_number = payload.registration_number.strip()
                if not new_number:
                    raise HTTPException(422, "Укажите регистрационный номер")
                if new_number != record.registration_number:
                    history = json.loads(record.details.get("registration_number_history", "[]"))
                    history.append({"number": record.registration_number, "changed_by": admin.id,
                                    "changed_at": utcnow().isoformat()})
                    record.details = {**record.details, "registration_number_history":
                                      json.dumps(history, ensure_ascii=False)}
                    record.registration_number = new_number
                    numbered = re.fullmatch(
                        rf"{PREFIX[record.kind]}-(\d{{4}})/(\d+)", new_number
                    )
                    if numbered:
                        counter_key = (record.kind, int(numbered.group(1)))
                        with session.no_autoflush:
                            counter = await session.get(RegistrationCounter, counter_key)
                        proposed = int(numbered.group(2))
                        if counter is None:
                            session.add(RegistrationCounter(
                                kind=counter_key[0], year=counter_key[1], next_value=proposed,
                            ))
                        elif proposed > counter.next_value:
                            counter.next_value = proposed
            if payload.registration_date is not None:
                record.registration_date = payload.registration_date
            record.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Не удалось сохранить запись") from exc
            await session.refresh(record)
            return record

    @router.delete("/{record_id}", status_code=204)
    async def delete_record(
        record_id: int, _admin: Any = Depends(require_superadmin_token),  # noqa: B008
    ) -> None:
        async with sessions() as session:
            record = await session.get(RegistrationRecord, record_id)
            if record is None:
                raise HTTPException(404, "Запись не найдена")
            await session.delete(record)
            await session.commit()

    return router
