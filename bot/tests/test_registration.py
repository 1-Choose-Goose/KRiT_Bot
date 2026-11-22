from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

import krit_bot.registration as registration_module
from krit_bot.db import (
    AdminUser,
    Person,
    PersonRole,
    build_engine,
    build_session_factory,
    ensure_schema,
)
from krit_bot.learning_models import Subject
from krit_bot.registration import (
    RegistrationRecord,
    create_registration_router,
    registration_sort_key,
)


@pytest.mark.asyncio
async def test_registration_journals_share_database_and_keep_separate_numbers(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'records.db').as_posix()}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(full_name="Ученик", phone="+79000000000")
        parent = Person(full_name="Родитель из базы", phone="+79000000001")
        parent.role_links.append(PersonRole(role="parent"))
        admin = AdminUser(username="registrar", full_name="Сотрудник регистрации",
                          password_hash="test", role="superadmin")
        math = Subject(name="Математика", color="#2563eb")
        russian = Subject(name="Русский язык", color="#2563eb")
        session.add_all([person, parent, admin, math, russian])
        await session.commit()
        person_id = person.id
        parent_id = parent.id
        math_id, russian_id = math.id, russian.id

    async def authorized() -> int:
        return 1

    async def super_authorized():
        return SimpleNamespace(id=1)

    app = FastAPI()
    app.include_router(create_registration_router(sessions, authorized, super_authorized))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        year = datetime.now(ZoneInfo("Asia/Yekaterinburg")).year
        payload = {
            "counterparty": "Введено вручную",
            "subject": "Математика",
            "person_id": person_id,
            "customer_person_id": parent_id,
            "details": {"student_name": "Ученик", "monthly_price": "8000.00",
                        "subject_ids": str(math_id)},
        }
        first = await client.post("/api/v1/registration/contract", json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["registration_number"] == f"дг-{year}/1"
        assert first.json()["registration_date"] == first.json()["document_date"]
        assert first.json()["counterparty"] == "Родитель из базы"
        assert first.json()["details"]["executor"] == "Сотрудник регистрации"
        second = await client.post("/api/v1/registration/contract", json=payload)
        assert second.status_code == 201, second.text
        assert second.json()["registration_number"] == f"дг-{year}/2"
        incoming = await client.post(
            "/api/v1/registration/incoming",
            json={**payload, "customer_person_id": None, "details": {}},
        )
        assert incoming.status_code == 201, incoming.text
        assert incoming.json()["registration_number"] == f"вх-{year}/1"
        assert incoming.json()["details"]["received_date"] == datetime.now(
            ZoneInfo("Asia/Yekaterinburg")
        ).strftime("%d.%m.%Y")
        assert incoming.json()["details"]["executor"] == "Сотрудник регистрации"
        manual_received = await client.post(
            "/api/v1/registration/incoming",
            json={**payload, "customer_person_id": None,
                  "details": {"received_date": "01.01.2020"}},
        )
        assert manual_received.status_code == 201, manual_received.text
        assert manual_received.json()["details"]["received_date"] == (
            incoming.json()["details"]["received_date"]
        )
        linked_outgoing = await client.post(
            "/api/v1/registration/outgoing",
            json={"document_date": f"{year}-11-21", "counterparty": "Адресат",
                  "subject": "Ответ", "details": {"incoming_record_id": str(incoming.json()["id"])}}
        )
        assert linked_outgoing.status_code == 201, linked_outgoing.text
        assert linked_outgoing.json()["details"]["executor"] == "Сотрудник регистрации"
        independent_outgoing = await client.post(
            "/api/v1/registration/outgoing",
            json={"document_date": f"{year}-11-21", "counterparty": "Адресат",
                  "subject": "Самостоятельное письмо"}
        )
        assert independent_outgoing.status_code == 201, independent_outgoing.text
        incoming_list = await client.get("/api/v1/registration/incoming")
        registered_incoming = next(
            item for item in incoming_list.json() if item["id"] == incoming.json()["id"]
        )
        assert registered_incoming["details"]["reply_number"] == (
            linked_outgoing.json()["registration_number"]
        )
        unanswered = await client.post(
            "/api/v1/registration/incoming",
            json={"counterparty": "Другой отправитель", "subject": "Без ответа"},
        )
        assert unanswered.status_code == 201, unanswered.text
        incoming_list = await client.get("/api/v1/registration/incoming")
        assert next(item for item in incoming_list.json() if item["id"] == unanswered.json()["id"])[
            "details"
        ]["reply_number"] == ""
        corrected_incoming = await client.put(
            f"/api/v1/registration/{incoming.json()['id']}",
            json={"counterparty": "Отправитель", "subject": "Письмо",
                  "details": {"received_date": f"20.11.{year}"}},
        )
        assert corrected_incoming.status_code == 200, corrected_incoming.text
        assert corrected_incoming.json()["registration_date"] == f"{year}-11-20"
        listed = await client.get("/api/v1/registration/contract")
        assert [item["registration_number"] for item in listed.json()] == [
            f"дг-{year}/2", f"дг-{year}/1"
        ]
        payload["subject"] = "Русский язык"
        payload["details"]["subject_ids"] = str(russian_id)
        changed = await client.put(
            f"/api/v1/registration/{first.json()['id']}", json=payload
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["registration_number"] == f"дг-{year}/1"
        assert changed.json()["subject"] == "Русский язык"
        self_customer = await client.put(
            f"/api/v1/registration/{second.json()['id']}",
            json={**payload, "customer_person_id": person_id},
        )
        assert self_customer.status_code == 200, self_customer.text
        assert self_customer.json()["counterparty"] == "Ученик"
        privileged = await client.put(
            f"/api/v1/registration/{first.json()['id']}",
            json={**payload, "registration_number": "дг-исправлено-1",
                  "registration_date": "2026-11-20"},
        )
        assert privileged.status_code == 200, privileged.text
        assert privileged.json()["registration_number"] == "дг-исправлено-1"
        collision = await client.put(
            f"/api/v1/registration/{first.json()['id']}",
            json={**payload, "registration_number": second.json()["registration_number"]},
        )
        assert collision.status_code == 409
        corrected_legacy = await client.put(
            f"/api/v1/registration/{first.json()['id']}",
            json={**payload, "document_date": None},
        )
        assert corrected_legacy.status_code == 200, corrected_legacy.text
        assert corrected_legacy.json()["document_date"] is None
        manual_new_date = await client.post(
            "/api/v1/registration/contract", json={**payload, "document_date": "2026-11-20"}
        )
        assert manual_new_date.status_code == 422
        payload["registration_date"] = "2027-01-01"
        rejected = await client.post("/api/v1/registration/contract", json=payload)
        assert rejected.status_code == 422
        payload.pop("registration_date")
        payload["document_number"] = "ручной-номер"
        rejected = await client.post("/api/v1/registration/contract", json=payload)
        assert rejected.status_code == 422
        app.dependency_overrides[super_authorized] = lambda: (_ for _ in ()).throw(
            HTTPException(403)
        )
        denied = await client.put(
            f"/api/v1/registration/{first.json()['id']}", json={**payload, "document_number": None}
        )
        assert denied.status_code == 403
        denied = await client.delete(f"/api/v1/registration/{first.json()['id']}")
        assert denied.status_code == 403
        app.dependency_overrides.clear()
        deleted = await client.delete(f"/api/v1/registration/{first.json()['id']}")
        assert deleted.status_code == 204
        listed = await client.get("/api/v1/registration/contract")
        assert len(listed.json()) == 1

    async with sessions() as session:
        records = list(await session.scalars(select(RegistrationRecord)))
        assert len(records) == 6
    await engine.dispose()


def test_registration_sort_uses_numeric_number_even_after_admin_edit() -> None:
    records = [
        SimpleNamespace(kind="contract", registration_number=number, year=2026,
                        sequence=original, id=original)
        for original, number in enumerate(
            ("дг-2026/10", "дг-2026/2", "дг-2025/70"), start=1
        )
    ]
    assert [item.registration_number for item in sorted(
        records, key=registration_sort_key, reverse=True
    )] == [
        "дг-2026/10", "дг-2026/2", "дг-2025/70"
    ]


@pytest.mark.asyncio
async def test_numbering_restarts_in_new_year(tmp_path, monkeypatch) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'new-year.db').as_posix()}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        session.add(AdminUser(username="registrar", full_name="Сотрудник регистрации",
                              password_hash="test", role="superadmin"))
        await session.commit()
    current = [datetime(2026, 12, 31)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].replace(tzinfo=tz)

    monkeypatch.setattr(registration_module, "datetime", Clock)

    async def authorized() -> int:
        return 1

    app = FastAPI()
    app.include_router(create_registration_router(sessions, authorized, authorized))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        data = {"document_date": "2026-12-31", "counterparty": "Адресат",
                "subject": "Письмо"}
        first = await client.post("/api/v1/registration/outgoing", json=data)
        assert first.status_code == 201, first.text
        assert first.json()["registration_number"] == "исх-2026/1"
        current[0] = datetime(2027, 1, 1)
        second = await client.post("/api/v1/registration/outgoing", json=data)
        assert second.status_code == 201, second.text
        assert second.json()["registration_number"] == "исх-2027/1"
    await engine.dispose()
