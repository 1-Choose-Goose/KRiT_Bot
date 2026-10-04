from __future__ import annotations

import httpx
from pydantic import SecretStr
from sqlalchemy import select

import krit_bot.webhook as webhook_module
from krit_bot.communication_models import InteractionRequest, InteractionResponse
from krit_bot.config import Settings
from krit_bot.db import (
    Person,
    PersonRole,
    build_engine,
    build_session_factory,
    ensure_schema,
)
from krit_bot.learning_models import PersonMaxIdentity


async def test_max_button_callback_reaches_database_through_webhook(
    tmp_path, monkeypatch
) -> None:
    callback_answers: list[tuple[str, str | None, dict | None]] = []

    class FakeMaxApi:
        def __init__(self, **_kwargs) -> None:
            pass

        async def answer_callback(
            self,
            *,
            callback_id: str,
            notification: str | None = None,
            message: dict | None = None,
        ) -> dict:
            callback_answers.append((callback_id, notification, message))
            return {"success": True}

        async def send_text(self, **_kwargs) -> dict:
            return {"success": True}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(webhook_module, "MaxApiClient", FakeMaxApi)
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'callback.db').as_posix()}"
    engine = build_engine(database_url)
    await ensure_schema(engine)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Куц Олег Олегович",
            phone="+79000000101",
            role_links=[PersonRole(role="student")],
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=101,
            )
        )
        request = InteractionRequest(
            request_type="yes_no",
            question="Вы придёте?",
            recipient_person_id=person.id,
            recipient_context="student",
            subject_person_id=person.id,
        )
        session.add(request)
        await session.commit()
        request_id = request.id
    await engine.dispose()

    settings = Settings(
        database_url=database_url,
        max_bot_token=SecretStr("test-token"),
        max_webhook_secret=SecretStr("test-secret"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = webhook_module.create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/webhooks/max",
                headers={"X-Max-Bot-Api-Secret": "test-secret"},
                json={
                    "update_type": "message_callback",
                    "user": {"user_id": 101},
                    "message": {"body": {"text": "Вы придёте?"}},
                    "callback": {
                        "callback_id": "real-button-click",
                        "payload": f"interaction:{request_id}:yes",
                    },
                },
            )
            assert response.status_code == 200, response.text

    check_engine = build_engine(database_url)
    check_sessions = build_session_factory(check_engine)
    async with check_sessions() as session:
        saved = await session.scalar(select(InteractionResponse))
        assert saved is not None
        assert saved.answer == "yes"
    await check_engine.dispose()
    assert callback_answers == [
        (
            "real-button-click",
            "Ответ сохранён",
            {"text": "Вы придёте?", "attachments": []},
        )
    ]
