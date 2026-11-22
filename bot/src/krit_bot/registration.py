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

from .db import AdminUser, Base, Person, PersonRole, utcnow
from .learning_models import Subject

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
    customer_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
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
    customer_person_id: int | None = None
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


def registration_sort_key(record: RegistrationRecord) -> tuple[int, int, str, int]:
    number = record.registration_number
    match = re.fullmatch(rf"{PREFIX[record.kind]}-(\d{{4}})/(\d+)", number)
    year, sequence = (
        (int(match.group(1)), int(match.group(2)))
        if match else (record.year, record.sequence)
    )
    return year, sequence, number, record.id


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

    async def resolve_customer(
        session: AsyncSession, kind: Kind, values: dict[str, Any], *,
        previous_id: int | None = None,
    ) -> dict[str, Any]:
        customer_id = values.get("customer_person_id")
        if kind != "contract":
            if customer_id is not None:
                raise HTTPException(422, "Заказчик из базы указывается только для договора")
            return values
        if customer_id is None:
            return values
        customer = await session.get(Person, customer_id)
        if customer is None:
            raise HTTPException(422, "Заказчик не найден в базе")
        if customer_id != values.get("person_id") and await session.get(
            PersonRole, (customer_id, "parent")
        ) is None:
            raise HTTPException(422, "Заказчик должен быть учеником или родителем из базы")
        if previous_id != customer_id:
            values["counterparty"] = customer.full_name
        return values

    async def resolve_incoming_reference(
        session: AsyncSession, details: dict[str, str]
    ) -> dict[str, str]:
        resolved = dict(details)
        source_id = resolved.get("incoming_record_id", "").strip()
        linked_fields = (
            "incoming_number", "incoming_document_number", "incoming_document_date"
        )
        if not source_id:
            if any(resolved.get(key) for key in linked_fields):
                raise HTTPException(422, "Выберите входящий документ из журнала")
            return resolved
        try:
            incoming_id = int(source_id)
        except ValueError as exc:
            raise HTTPException(422, "Неверная ссылка на входящий документ") from exc
        incoming = await session.get(RegistrationRecord, incoming_id)
        if incoming is None or incoming.kind != "incoming":
            raise HTTPException(422, "Входящий документ не найден")
        resolved.update({
            "incoming_record_id": str(incoming.id),
            "incoming_number": incoming.registration_number,
            "incoming_document_number": incoming.document_number or "",
            "incoming_document_date": (
                incoming.document_date.strftime("%d.%m.%Y")
                if incoming.document_date else ""
            ),
        })
        return resolved

    async def resolve_contract_subjects(
        session: AsyncSession, values: dict[str, Any], *, creating: bool
    ) -> dict[str, Any]:
        raw_ids = values["details"].get("subject_ids", "").strip()
        if not raw_ids:
            if creating:
                raise HTTPException(422, "Выберите предметы из справочника")
            return values
        parts = [part.strip() for part in raw_ids.split(",")]
        if not parts or len(parts) > 20 or any(not part.isdigit() for part in parts):
            raise HTTPException(422, "Неверный список предметов")
        identifiers = list(dict.fromkeys(int(part) for part in parts))
        subjects = {
            item.id: item for item in (
                await session.scalars(select(Subject).where(Subject.id.in_(identifiers)))
            ).all()
        }
        if len(subjects) != len(identifiers) or any(
            not subjects[identifier].active for identifier in identifiers
        ):
            raise HTTPException(422, "Предмет не найден в действующем справочнике")
        names = ", ".join(subjects[identifier].name for identifier in identifiers)
        if len(names) > 500:
            raise HTTPException(422, "Слишком длинный список предметов")
        values["subject"] = names
        values["details"] = {
            **values["details"], "subject_ids": ",".join(map(str, identifiers))
        }
        return values

    def validate_document(kind: Kind, payload: RecordInput, *, creating: bool) -> None:
        if kind != "incoming" and payload.document_number:
            raise HTTPException(422, "Регистрационный номер присваивается программой")
        if creating and kind == "outgoing" and payload.document_date is None:
            raise HTTPException(422, "Укажите дату подписания документа")
        if creating and kind == "contract" and payload.document_date is not None:
            raise HTTPException(422, "Дату договора присваивает программа")
        if creating and kind == "outgoing" and payload.details.get("sent_date"):
            raise HTTPException(422, "Дату отправки присваивает программа")
        if kind == "incoming" and not creating:
            received = payload.details.get("received_date", "").strip()
            try:
                datetime.strptime(received, "%d.%m.%Y")
            except ValueError as exc:
                raise HTTPException(422, "Укажите дату получения как ДД.ММ.ГГГГ") from exc

    @router.get("/{kind}", response_model=list[RecordView])
    async def list_records(kind: Kind) -> list[RecordView]:
        async with sessions() as session:
            result = await session.scalars(
                select(RegistrationRecord)
                .where(RegistrationRecord.kind == kind)
            )
            records = sorted(result, key=registration_sort_key, reverse=True)
            replies: dict[int, list[str]] = {}
            if kind == "incoming":
                outgoing = await session.scalars(
                    select(RegistrationRecord).where(RegistrationRecord.kind == "outgoing")
                )
                for answer in sorted(outgoing, key=registration_sort_key):
                    source_id = str(answer.details.get("incoming_record_id") or "")
                    if source_id.isdigit():
                        replies.setdefault(int(source_id), []).append(answer.registration_number)
            views = [RecordView.model_validate(record) for record in records]
            for view in views:
                if view.id in replies:
                    view.details = {**view.details, "reply_number": ", ".join(replies[view.id])}
            return views

    @router.post("/{kind}", response_model=RecordView, status_code=201)
    async def create_record(
        kind: Kind, payload: RecordInput,
        admin_id: int = Depends(require_management_token),  # noqa: B008
    ) -> RegistrationRecord:
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
            values = payload.model_dump()
            values = await resolve_customer(session, kind, values)
            registrar = await session.get(AdminUser, admin_id)
            if registrar is None:
                raise HTTPException(403, "Сотрудник не найден")
            values["details"] = {**values["details"], "executor": registrar.full_name}
            if kind == "incoming":
                values["details"] = {
                    **values["details"], "received_date": registration_date.strftime("%d.%m.%Y"),
                    "reply_number": "",
                }
            if kind == "contract":
                values["document_date"] = registration_date
                values = await resolve_contract_subjects(session, values, creating=True)
            if kind == "outgoing":
                values["details"] = {
                    **values["details"], "sent_date": registration_date.strftime("%d.%m.%Y")
                }
                values["details"] = await resolve_incoming_reference(
                    session, values["details"]
                )
            record = RegistrationRecord(
                kind=kind,
                year=year,
                sequence=sequence,
                registration_number=f"{PREFIX[kind]}-{year}/{sequence}",
                registration_date=registration_date,
                **values,
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
            if "executor" not in editable["details"] and record.details.get("executor"):
                editable["details"]["executor"] = record.details["executor"]
            editable = await resolve_customer(
                session, record.kind, editable, previous_id=record.customer_person_id
            )
            if record.kind == "contract":
                editable = await resolve_contract_subjects(session, editable, creating=False)
            if record.kind == "incoming":
                editable["details"]["reply_number"] = record.details.get("reply_number", "")
            if record.kind == "outgoing":
                editable["details"] = await resolve_incoming_reference(
                    session, editable["details"]
                )
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
            if record.kind == "contract" and payload.document_date is not None:
                record.registration_date = payload.document_date
            elif record.kind == "incoming":
                record.registration_date = datetime.strptime(
                    payload.details["received_date"].strip(), "%d.%m.%Y"
                ).date()
            elif record.kind == "outgoing":
                sent_date = payload.details.get("sent_date", "").strip()
                try:
                    record.registration_date = datetime.strptime(
                        sent_date, "%d.%m.%Y"
                    ).date()
                except ValueError as exc:
                    raise HTTPException(422, "Укажите дату отправки как ДД.ММ.ГГГГ") from exc
            elif record.kind != "contract" and payload.registration_date is not None:
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
            if record.kind == "incoming":
                outgoing = await session.scalars(
                    select(RegistrationRecord).where(RegistrationRecord.kind == "outgoing")
                )
                for answer in outgoing:
                    if answer.details.get("incoming_record_id") == str(record.id):
                        answer.details = {
                            **answer.details, "incoming_record_id": "", "incoming_number": "",
                            "incoming_document_number": "", "incoming_document_date": "",
                        }
            await session.delete(record)
            await session.commit()

    return router
