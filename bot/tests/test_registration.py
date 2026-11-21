from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

import krit_bot.registration as registration_module
from krit_bot.db import Person, build_engine, build_session_factory, ensure_schema
from krit_bot.registration import RegistrationRecord, create_registration_router


@pytest.mark.asyncio
async def test_registration_journals_share_database_and_keep_separate_numbers(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'records.db').as_posix()}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(full_name="Ученик", phone="+79000000000")
        session.add(person)
        await session.commit()
        person_id = person.id

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
            "document_date": "2026-11-20",
            "counterparty": "Родитель",
            "subject": "Математика",
            "person_id": person_id,
            "details": {"student_name": "Ученик", "monthly_price": "8000.00"},
        }
        first = await client.post("/api/v1/registration/contract", json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["registration_number"] == f"дг-{year}/1"
        second = await client.post("/api/v1/registration/contract", json=payload)
        assert second.status_code == 201, second.text
        assert second.json()["registration_number"] == f"дг-{year}/2"
        incoming = await client.post(
            "/api/v1/registration/incoming",
            json={**payload, "details": {"received_date": "21.11.2026"}},
        )
        assert incoming.status_code == 201, incoming.text
        assert incoming.json()["registration_number"] == f"вх-{year}/1"
        listed = await client.get("/api/v1/registration/contract")
        assert [item["registration_number"] for item in listed.json()] == [
            f"дг-{year}/2", f"дг-{year}/1"
        ]
        payload["subject"] = "Русский язык"
        changed = await client.put(
            f"/api/v1/registration/{first.json()['id']}", json=payload
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["registration_number"] == f"дг-{year}/1"
        assert changed.json()["subject"] == "Русский язык"
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
        missing_new_date = await client.post(
            "/api/v1/registration/contract", json={**payload, "document_date": None}
        )
        assert missing_new_date.status_code == 422
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
        assert len(records) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_numbering_restarts_in_new_year(tmp_path, monkeypatch) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'new-year.db').as_posix()}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
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
