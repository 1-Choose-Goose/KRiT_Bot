from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from krit_bot.config import Settings
from krit_bot.db import (
    EXPECTED_ALEMBIC_REVISION,
    Person,
    ProcessedMessage,
    build_engine,
    build_session_factory,
    claim_message,
    ensure_schema,
    verify_schema_current,
)
from krit_bot.learning import _raise_admin_condition, _resolve_admin_condition
from krit_bot.learning_models import (
    AdminNotification,
    ClubPresenceSession,
    Lesson,
    NotificationJob,
    PersonMaxIdentity,
    Room,
    Subject,
)
from krit_bot.learning_notifications import LearningNotificationWorker
from krit_bot.max_api import MaxApiClient, MaxApiError
from krit_bot.participant_state import apply_attendance_state
from krit_bot.webhook import create_app


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_participant_state_transitions_clear_stale_metadata() -> None:
    participant = SimpleNamespace(
        attendance_status="excused",
        arrived_at=None,
        left_at=None,
        late_minutes=None,
        early_leave_reason=None,
        cancelled_at=datetime.now(UTC),
        cancelled_by="administrator",
        cancelled_by_person_id=None,
        cancelled_by_admin_id=1,
        cancellation_reason="болезнь",
    )
    arrived = datetime.now(UTC)
    apply_attendance_state(participant, "late", arrived_at=arrived, late_minutes=7)
    assert participant.attendance_status == "late"
    assert participant.arrived_at == arrived
    assert participant.late_minutes == 7
    assert participant.cancelled_at is None
    assert participant.cancellation_reason is None

    left = arrived + timedelta(minutes=10)
    apply_attendance_state(
        participant,
        "left_early",
        arrived_at=arrived.replace(tzinfo=None),
        left_at=left,
        late_minutes=7,
        early_leave_reason="плохо себя чувствует",
    )
    assert participant.left_at == left
    assert participant.early_leave_reason == "плохо себя чувствует"

    apply_attendance_state(participant, "absent")
    assert participant.arrived_at is None
    assert participant.left_at is None
    assert participant.late_minutes is None
    assert participant.early_leave_reason is None


@pytest.mark.asyncio
async def test_claim_message_only_treats_integrity_error_as_duplicate(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        assert await claim_message(session, "same-message") is True
        await session.commit()
    async with sessions() as session:
        assert await claim_message(session, "same-message") is False

    class BrokenSession:
        rolled_back = False

        def add(self, _: ProcessedMessage) -> None:
            return None

        async def get(self, *_: object) -> None:
            return None

        async def flush(self) -> None:
            raise RuntimeError("database unavailable")

        async def rollback(self) -> None:
            self.rolled_back = True

    broken = BrokenSession()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await claim_message(broken, "new-message")  # type: ignore[arg-type]
    assert broken.rolled_back is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_max_contact_hmac_formats_are_compared_correctly() -> None:
    client = MaxApiClient(token="contact-secret", base_url="https://example.invalid")
    try:
        vcf = "BEGIN:VCARD\nTEL:+79000000000\nEND:VCARD"
        digest = hmac.new(b"contact-secret", vcf.encode(), hashlib.sha256).digest()
        signature_hex = digest.hex()
        signature_base64 = base64.b64encode(digest).decode("ascii")
        assert client.verify_contact(vcf_info=vcf, signature=signature_hex.upper())
        assert client.verify_contact(vcf_info=vcf, signature=signature_base64)
        changed_case = next(
            signature_base64[:index]
            + character.swapcase()
            + signature_base64[index + 1 :]
            for index, character in enumerate(signature_base64)
            if character.isalpha()
        )
        assert not client.verify_contact(vcf_info=vcf, signature=changed_case)
        assert not client.verify_contact(vcf_info=vcf, signature="invalid")
    finally:
        await client.close()


def test_postgresql_uses_fixed_one_time_bootstrap_instead_of_configured_default() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/db",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bootstrap_admin_username="ignored",
        bootstrap_admin_password=SecretStr("ignored"),
    )

    assert settings.initial_admin_credentials() == ("Choose_Goose", "123", True)


@pytest.mark.asyncio
async def test_schema_behind_head_is_rejected(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        await connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES ('old_revision')")
        )
    with pytest.raises(RuntimeError, match=EXPECTED_ALEMBIC_REVISION):
        await verify_schema_current(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_production_ensure_schema_only_verifies_revision(monkeypatch) -> None:
    verified: list[object] = []

    async def fake_verify(engine: object) -> None:
        verified.append(engine)

    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr("krit_bot.db.verify_schema_current", fake_verify)
    await ensure_schema(fake_engine)  # type: ignore[arg-type]
    assert verified == [fake_engine]


@pytest.mark.asyncio
async def test_admin_condition_can_resolve_and_reopen(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'conditions.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        assert await _raise_admin_condition(
            session,
            condition_key="lesson:7:no_active_students",
            kind="lesson_no_active_students",
            title="Нет участников",
            message="Первый случай",
            lesson_id=None,
        )
        assert not await _raise_admin_condition(
            session,
            condition_key="lesson:7:no_active_students",
            kind="lesson_no_active_students",
            title="Нет участников",
            message="Дубль",
            lesson_id=None,
        )
        first = await session.scalar(select(AdminNotification))
        assert first is not None
        first.read_at = datetime.now(UTC)
        await _resolve_admin_condition(session, "lesson:7:no_active_students")
        assert await _raise_admin_condition(
            session,
            condition_key="lesson:7:no_active_students",
            kind="lesson_no_active_students",
            title="Нет участников",
            message="Новый случай",
            lesson_id=None,
        )
        await session.commit()
        rows = list((await session.scalars(select(AdminNotification))).all())
        assert len(rows) == 2
        assert sum(item.read_at is None and item.resolved_at is None for item in rows) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_notification_worker_survives_unexpected_iteration_error() -> None:
    worker = LearningNotificationWorker(
        sessions=None,  # type: ignore[arg-type]
        api=None,  # type: ignore[arg-type]
        poll_seconds=0,
    )
    worker.recover_interrupted = AsyncMock(return_value=None)  # type: ignore[method-assign]
    worker.process_one = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("broken job"), asyncio.CancelledError()]
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert worker.process_one.await_count == 2


@pytest.mark.asyncio
async def test_notification_worker_repeats_stale_recovery_while_running() -> None:
    worker = LearningNotificationWorker(
        sessions=None,  # type: ignore[arg-type]
        api=None,  # type: ignore[arg-type]
        poll_seconds=0,
        recovery_interval_seconds=0,
    )
    worker.recover_interrupted = AsyncMock(return_value=None)  # type: ignore[method-assign]
    worker.process_one = AsyncMock(  # type: ignore[method-assign]
        side_effect=[False, asyncio.CancelledError()]
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.run()

    assert worker.recover_interrupted.await_count == 2


@pytest.mark.asyncio
async def test_two_max_failures_for_one_lesson_are_recorded_separately(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'two-failures.db'}")
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        subject = Subject(name="Физика", color="#2563eb")
        room = Room(name="Кабинет теста", capacity=5)
        recipients = [
            Person(full_name=f"Получатель {index} Тестовый", phone=f"+7900000300{index}")
            for index in (1, 2)
        ]
        session.add_all([subject, room, *recipients])
        await session.flush()
        lesson = Lesson(
            subject_id=subject.id,
            teacher_id=recipients[0].id,
            room_id=room.id,
            start_at=datetime.now(UTC) + timedelta(hours=1),
            end_at=datetime.now(UTC) + timedelta(hours=2),
            subject_name_snapshot=subject.name,
            teacher_name_snapshot=recipients[0].full_name,
            room_name_snapshot=room.name,
        )
        session.add(lesson)
        await session.flush()
        for index, recipient in enumerate(recipients, start=1):
            session.add(
                PersonMaxIdentity(
                    person_id=recipient.id,
                    verified_phone=recipient.phone,
                    max_user_id=300 + index,
                )
            )
            session.add(
                NotificationJob(
                    dedupe_key=f"failure:{index}",
                    event_type="test",
                    lesson_id=lesson.id,
                    recipient_person_id=recipient.id,
                    scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
                    payload={"text": "Проверка ошибки"},
                )
            )
        await session.commit()

    class FailingMax:
        async def send_text(self, **_: object) -> dict[str, object]:
            raise MaxApiError("permanent", status_code=401)

    worker = LearningNotificationWorker(
        sessions=sessions,
        api=FailingMax(),  # type: ignore[arg-type]
        max_attempts=1,
    )
    assert await worker.process_one()
    assert await worker.process_one()
    async with sessions() as session:
        alerts = list(
            (
                await session.scalars(
                    select(AdminNotification).where(
                        AdminNotification.lesson_id == lesson.id,
                        AdminNotification.kind == "max_notification_failed",
                    )
                )
            ).all()
        )
        assert len(alerts) == 2
        assert len({item.dedupe_key for item in alerts}) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_person_aggregate_rolls_back_on_related_conflict(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'aggregate.db'}"
    app = create_app(_settings(database_url))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _login(client)
            response = await client.post(
                "/api/v1/people/aggregate",
                headers=headers,
                json={
                    "person": {
                        "full_name": "Ученик Транзакционный Один",
                        "phone": "+79000001001",
                        "roles": ["student"],
                    },
                    "new_parents": [
                        {
                            "full_name": "Родитель Первый Один",
                            "phone": "+79000001002",
                            "max_auth_phone": "+79000001999",
                            "roles": ["parent"],
                        },
                        {
                            "full_name": "Родитель Второй Один",
                            "phone": "+79000001003",
                            "max_auth_phone": "+79000001999",
                            "roles": ["parent"],
                        },
                    ],
                },
            )
            assert response.status_code == 409, response.text
            people = (await client.get("/api/v1/people", headers=headers)).json()
            assert people == []


@pytest.mark.asyncio
async def test_bot_access_is_independent_from_learning_availability(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'bot-access.db'}"
    app = create_app(_settings(database_url))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _login(client)
            created = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Ученик Без Бота",
                    "phone": "+79000002001",
                    "roles": ["student"],
                    "active": True,
                    "bot_access_enabled": False,
                },
            )
            assert created.status_code == 201
            assert created.json()["active"] is True
            assert created.json()["bot_access_enabled"] is False
            references = await client.get("/api/v1/learning/reference-data", headers=headers)
            assert int(created.json()["id"]) in {
                int(item["id"]) for item in references.json()["students"]
            }


@pytest.mark.asyncio
async def test_stale_presence_is_not_current_and_must_be_closed(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}"
    app = create_app(_settings(database_url))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _login(client)
            created = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Ученик С Незакрытым Посещением",
                    "phone": "+79000004001",
                    "roles": ["student"],
                },
            )
            person_id = int(created.json()["id"])
            engine = build_engine(database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                session.add(
                    ClubPresenceSession(
                        person_id=person_id,
                        arrived_at=datetime.now(UTC) - timedelta(days=1),
                    )
                )
                await session.commit()

            today = (await client.get("/api/v1/learning/today", headers=headers)).json()
            assert today["present"] == []
            assert today["stale_presence"][0]["person_id"] == person_id
            arrival = await client.post(
                f"/api/v1/learning/presence/{person_id}/arrival", headers=headers
            )
            assert arrival.status_code == 409
            assert arrival.json()["detail"]["code"] == "stale_presence"
            invalid_departure = await client.post(
                f"/api/v1/learning/presence/{person_id}/departure",
                headers=headers,
                json={
                    "left_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "reason": "Ошибочное время",
                },
            )
            assert invalid_departure.status_code == 422
            corrected_departure = datetime.now(UTC) - timedelta(hours=20)
            departure = await client.post(
                f"/api/v1/learning/presence/{person_id}/departure",
                headers=headers,
                json={
                    "left_at": corrected_departure.isoformat(),
                    "reason": "Администратор не отметил уход вовремя",
                },
            )
            assert departure.status_code == 200, departure.text
            saved_departure = datetime.fromisoformat(departure.json()["left_at"])
            if saved_departure.tzinfo is None:
                saved_departure = saved_departure.replace(tzinfo=UTC)
            assert saved_departure == corrected_departure
            corrected_today = (
                await client.get("/api/v1/learning/today", headers=headers)
            ).json()
            assert corrected_today["stale_presence"] == []
            assert all(
                alert["kind"] != "stale_presence" for alert in corrected_today["alerts"]
            )
            active_alerts = (
                await client.get(
                    "/api/v1/learning/admin-notifications?unread_only=false",
                    headers=headers,
                )
            ).json()
            assert all(alert["kind"] != "stale_presence" for alert in active_alerts)
            new_arrival = await client.post(
                f"/api/v1/learning/presence/{person_id}/arrival", headers=headers
            )
            assert new_arrival.status_code == 201
            assert new_arrival.json()["already_present"] is False
            await engine.dispose()


@pytest.mark.asyncio
async def test_reference_updates_return_409_and_restore_reports_phone_conflict(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'conflicts.db'}"
    app = create_app(_settings(database_url))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await _login(client)
            first_person = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Клиент Архивный Первый",
                    "phone": "+79000005001",
                    "max_auth_phone": "+79000005999",
                    "roles": ["student"],
                },
            )
            first_id = int(first_person.json()["id"])
            assert (
                await client.post(f"/api/v1/people/{first_id}/archive", headers=headers)
            ).status_code == 200
            second_person = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Клиент Активный Второй",
                    "phone": "+79000005002",
                    "max_auth_phone": "+79000005999",
                    "roles": ["student"],
                },
            )
            assert second_person.status_code == 201
            restored = await client.post(
                f"/api/v1/people/{first_id}/restore", headers=headers
            )
            assert restored.status_code == 409
            assert restored.json()["detail"]["code"] == "max_auth_phone_conflict"

            endpoint_payloads = (
                (
                    "subjects",
                    {"name": "Предмет А", "color": "#2563eb", "teacher_ids": []},
                    {"name": "Предмет Б", "color": "#2563eb", "teacher_ids": []},
                ),
                (
                    "rooms",
                    {"name": "Кабинет А", "capacity": 10},
                    {"name": "Кабинет Б", "capacity": 10},
                ),
                (
                    "groups",
                    {"name": "Группа А", "default_duration_minutes": 60},
                    {"name": "Группа Б", "default_duration_minutes": 60},
                ),
            )
            for endpoint, first_payload, second_payload in endpoint_payloads:
                first = await client.post(
                    f"/api/v1/learning/{endpoint}", headers=headers, json=first_payload
                )
                second = await client.post(
                    f"/api/v1/learning/{endpoint}", headers=headers, json=second_payload
                )
                assert first.status_code == 201, first.text
                assert second.status_code == 201, second.text
                duplicate_payload = {**second_payload, "name": first_payload["name"]}
                duplicate = await client.put(
                    f"/api/v1/learning/{endpoint}/{second.json()['id']}",
                    headers=headers,
                    json=duplicate_payload,
                )
                assert duplicate.status_code == 409, duplicate.text
