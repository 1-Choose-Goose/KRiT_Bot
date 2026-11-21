from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

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

    app = FastAPI()
    app.include_router(create_registration_router(sessions, authorized))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "registration_date": "2026-11-21",
            "document_date": "2026-11-20",
            "counterparty": "Родитель",
            "subject": "Математика",
            "person_id": person_id,
            "details": {"student_name": "Ученик", "monthly_price": "8000.00"},
        }
        first = await client.post("/api/v1/registration/contract", json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["registration_number"] == "ДГ-2026-0001"
        second = await client.post("/api/v1/registration/contract", json=payload)
        assert second.status_code == 201, second.text
        assert second.json()["registration_number"] == "ДГ-2026-0002"
        incoming = await client.post("/api/v1/registration/incoming", json=payload)
        assert incoming.status_code == 201, incoming.text
        assert incoming.json()["registration_number"] == "ВХ-2026-0001"
        listed = await client.get("/api/v1/registration/contract")
        assert [item["registration_number"] for item in listed.json()] == [
            "ДГ-2026-0002", "ДГ-2026-0001"
        ]
        payload["subject"] = "Русский язык"
        changed = await client.put(
            f"/api/v1/registration/{first.json()['id']}", json=payload
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["registration_number"] == "ДГ-2026-0001"
        assert changed.json()["subject"] == "Русский язык"
        payload["registration_date"] = "2027-01-01"
        rejected = await client.put(
            f"/api/v1/registration/{first.json()['id']}", json=payload
        )
        assert rejected.status_code == 422

    async with sessions() as session:
        records = list(await session.scalars(select(RegistrationRecord)))
        assert len(records) == 3
    await engine.dispose()
