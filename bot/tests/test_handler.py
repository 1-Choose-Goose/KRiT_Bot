from __future__ import annotations

from datetime import timedelta

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from krit_bot.communication_models import (
    CommunicationMessage,
    CommunicationThread,
    InteractionRequest,
    InteractionResponse,
    MaxRegistrationPending,
)
from krit_bot.config import Settings
from krit_bot.db import (
    AccessAttempt,
    Base,
    Person,
    PersonRole,
    build_session_factory,
    utcnow,
)
from krit_bot.handler import EchoHandler, parse_message_callback, parse_message_created
from krit_bot.learning_models import PersonMaxIdentity
from krit_bot.max_api import WEBHOOK_UPDATE_TYPES
from krit_bot.webhook import (
    ensure_max_webhook_subscription,
    run_max_webhook_subscription,
)


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_attachments: list[list[dict] | None] = []
        self.contact_requests: list[int] = []
        self.contact_request_texts: list[str] = []
        self.members: set[int] = set()
        self.member_checks: list[tuple[int, list[int]]] = []
        self.chat_link: str | None = None
        self.callback_answers: list[tuple[str, str, dict | None]] = []

    async def send_text(self, *, user_id: int, text: str, attachments=None) -> dict:
        self.sent.append((user_id, text))
        self.sent_attachments.append(attachments)
        return {}

    async def request_contact(self, *, user_id: int, text: str) -> dict:
        self.contact_requests.append(user_id)
        self.contact_request_texts.append(text)
        return {}

    def verify_contact(self, *, vcf_info: str, signature: str) -> bool:
        return signature == "valid"

    async def get_chat_members(self, *, chat_id: int, user_ids: list[int]) -> dict:
        self.member_checks.append((chat_id, user_ids))
        return {
            "members": [
                {"user_id": user_id} for user_id in user_ids if user_id in self.members
            ]
        }

    async def get_chat(self, *, chat_id: int) -> dict:
        return {"chat_id": chat_id, "type": "channel", "link": self.chat_link}

    async def answer_callback(
        self, *, callback_id: str, notification: str, message: dict | None = None
    ) -> dict:
        self.callback_answers.append((callback_id, notification, message))
        return {}


def update(user_id: int, text: str = "Привет", mid: str = "m1") -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "sender": {
                "user_id": user_id,
                "first_name": "Иван",
                "last_name": "Иванов",
                "username": "ivan",
                "is_bot": False,
            },
            "body": {"mid": mid, "seq": 1, "text": text},
        },
    }


def bot_started_update(user_id: int) -> dict:
    return {
        "update_type": "bot_started",
        "timestamp": 1573226679188,
        "chat_id": user_id,
        "user": {
            "user_id": user_id,
            "name": "Иван Иванов",
            "username": "ivan",
        },
    }


def contact_update(user_id: int, phone: str, mid: str = "contact-1") -> dict:
    item = update(user_id, text="", mid=mid)
    item["message"]["body"]["attachments"] = [
        {
            "type": "contact",
            "payload": {
                "vcf_info": f"BEGIN:VCARD\r\nTEL;TYPE=cell:{phone}\r\nEND:VCARD\r\n",
                "hash": "valid",
            },
        }
    ]
    return item


def callback_update(
    user_id: int,
    payload: str,
    callback_id: str = "cb-1",
    message_text: str = "Исходный текст опроса",
) -> dict:
    return {
        "update_type": "message_callback",
        "message": {"body": {"text": message_text}},
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": user_id},
        },
    }


def callback_update_with_root_user(
    user_id: int,
    payload: str,
    callback_id: str = "cb-root",
    message_text: str = "Исходный текст опроса",
) -> dict:
    return {
        "update_type": "message_callback",
        "user": {"user_id": user_id},
        "message": {"body": {"text": message_text}},
        "callback": {"callback_id": callback_id, "payload": payload},
    }


def test_parse_message_created() -> None:
    parsed = parse_message_created(update(42))
    assert parsed is not None
    assert parsed.user_id == 42
    assert parsed.display_name == "Иван Иванов"


async def test_bot_started_immediately_requests_authorization() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(bot_started_update(42))

    assert api.contact_requests == [42]
    assert api.contact_request_texts == [
        "Для авторизации нажмите кнопку «Поделиться номером». "
        "Отправьте номер телефона, привязанный к вашему аккаунту MAX "
        "и указанный при записи в «КРиТ»."
    ]
    async with sessions() as session:
        attempt = await session.get(AccessAttempt, 42)
        assert attempt is not None
        assert attempt.display_name == "Иван Иванов"
        assert attempt.username == "ivan"
    await engine.dispose()


async def test_bot_started_confirms_existing_authorization() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                max_user_id=42,
                verified_phone="+79990000000",
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(bot_started_update(42))

    assert api.sent == [(42, "Вы уже авторизованы в «КРиТ».")]
    assert api.contact_requests == []
    await engine.dispose()


def test_parse_message_callback_accepts_documented_root_user() -> None:
    update = callback_update_with_root_user(
        42, "interaction:1:yes", message_text="Подтвердите участие"
    )
    parsed = parse_message_callback(update)
    assert parsed is not None
    assert parsed.user_id == 42
    assert parsed.payload == "interaction:1:yes"
    assert parsed.message_text == "Подтвердите участие"


async def test_webhook_startup_subscribes_to_button_callbacks() -> None:
    class SubscriptionApi:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def subscribe_webhook(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            return {"success": True}

    api = SubscriptionApi()
    settings = Settings(
        max_bot_token=SecretStr("test-token"),
        max_webhook_secret=SecretStr("test-secret"),
        max_webhook_url="https://example.test/webhooks/max",
        bot_mode="webhook",
    )

    assert await ensure_max_webhook_subscription(api, settings)  # type: ignore[arg-type]
    assert api.calls == [
        {
            "url": "https://example.test/webhooks/max",
            "secret": "test-secret",
            "update_types": WEBHOOK_UPDATE_TYPES,
        }
    ]
    assert "message_callback" in api.calls[0]["update_types"]


async def test_webhook_subscription_retries_in_one_bounded_background_loop() -> None:
    class FlakySubscriptionApi:
        def __init__(self) -> None:
            self.calls = 0

        async def subscribe_webhook(self, **_kwargs) -> dict:
            self.calls += 1
            if self.calls < 3:
                raise OSError("temporary network failure")
            return {"success": True}

    api = FlakySubscriptionApi()
    settings = Settings(
        max_bot_token=SecretStr("test-token"),
        max_webhook_secret=SecretStr("test-secret"),
        max_webhook_url="https://example.test/webhooks/max",
        bot_mode="webhook",
    )

    assert await run_max_webhook_subscription(
        api, settings, max_attempts=3, base_delay_seconds=0
    )
    assert api.calls == 3


async def test_only_authorized_message_is_stored_for_the_admin() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(update(42, "Раз"))
    await handler.handle(update(99, "Два", "m2"))

    assert api.sent == []
    assert api.contact_requests == [99]
    async with sessions() as session:
        message = await session.scalar(select(CommunicationMessage))
        assert message is not None
        assert message.text == "Раз"
        assert message.direction == "inbound"
    await engine.dispose()


async def test_slash_command_is_stored_without_admin_notification() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
            )
        )
        await session.commit()
        person_id = person.id
    handler = EchoHandler(sessions=sessions, api=FakeApi())  # type: ignore[arg-type]

    await handler.handle(update(42, "/start"))

    async with sessions() as session:
        message = await session.scalar(select(CommunicationMessage))
        thread = await session.get(CommunicationThread, person_id)
        assert message is not None
        assert message.message_type == "command"
        assert thread is not None
        assert thread.admin_unread_count == 0
        assert thread.last_message_at is None
    await engine.dispose()


async def test_poll_button_with_root_user_saves_answer_and_acknowledges_callback() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
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
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(
        callback_update_with_root_user(42, f"interaction:{request_id}:yes", "poll-yes")
    )

    async with sessions() as session:
        response = await session.scalar(select(InteractionResponse))
        message = await session.scalar(select(CommunicationMessage))
        thread = await session.get(CommunicationThread, 1)
        assert response is not None and response.answer == "yes"
        assert message is not None and message.message_type == "interaction_callback"
        assert thread is not None and thread.admin_unread_count == 0
    assert api.callback_answers[-1] == (
        "poll-yes",
        "Ответ сохранён",
        {"text": "Исходный текст опроса", "attachments": []},
    )
    await engine.dispose()


async def test_written_absence_reason_is_saved_and_acknowledged() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
            )
        )
        request = InteractionRequest(
            request_type="lesson_confirmation",
            question="Вы придёте?",
            recipient_person_id=person.id,
            recipient_context="student",
            subject_person_id=person.id,
            status="answered",
            expects_reason_from_person_id=person.id,
        )
        session.add(request)
        await session.flush()
        session.add(
            InteractionResponse(
                request_id=request.id,
                respondent_person_id=person.id,
                respondent_context="student",
                answer="no",
            )
        )
        await session.commit()
        request_id = request.id
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(update(42, "Болит живот", mid="reason-1"))

    async with sessions() as session:
        request = await session.get(InteractionRequest, request_id)
        response = await session.scalar(
            select(InteractionResponse).where(InteractionResponse.request_id == request_id)
        )
        assert request is not None and request.expects_reason_from_person_id is None
        assert response is not None and response.reason == "Болит живот"
    assert api.sent == [(42, "Спасибо! Причина сохранена.")]
    await engine.dispose()


async def test_malformed_and_stale_callbacks_are_harmless() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    malformed = (
        ("reason:not-a-number:skip", "bad-reason"),
        ("interaction:not-a-number:partial", "bad-partial"),
        ("interaction:not-a-number:yes", "bad-whole"),
        ("interaction:1:lesson:not-a-number:yes", "bad-lesson"),
    )
    for payload, callback_id in malformed:
        await handler.handle(callback_update_with_root_user(42, payload, callback_id))

    await handler.handle(
        callback_update_with_root_user(42, "interaction:999999:yes", "stale")
    )

    assert api.callback_answers[:4] == [
        (callback_id, "Кнопка больше не поддерживается", None)
        for _payload, callback_id in malformed
    ]
    assert api.callback_answers[-1][0] == "stale"
    await engine.dispose()


async def test_disabled_person_cannot_answer_old_callback() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
            bot_access_enabled=False,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
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
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(
        callback_update_with_root_user(42, f"interaction:{request_id}:yes", "disabled")
    )
    await handler.handle(
        callback_update_with_root_user(
            42, f"interaction:{request_id}:partial", "disabled-partial"
        )
    )
    await handler.handle(
        callback_update_with_root_user(42, f"reason:{request_id}:skip", "disabled-reason")
    )

    async with sessions() as session:
        assert await session.scalar(select(InteractionResponse)) is None
    assert api.callback_answers[-3:] == [
        (
            callback_id,
            "Доступ к боту отключён. Обратитесь к администратору.",
            None,
        )
        for callback_id in ("disabled", "disabled-partial", "disabled-reason")
    ]
    await engine.dispose()


async def test_duplicate_message_is_ignored() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(update(42))
    await handler.handle(update(42))

    assert api.sent == []
    async with sessions() as session:
        assert await session.scalar(select(func.count(CommunicationMessage.id))) == 1
    await engine.dispose()


async def test_verified_contact_links_user_once() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        session.add(
            Person(
                full_name="Иван Иванов",
                phone="+79990000000",
                max_auth_phone="+79990000000",
                role_links=[PersonRole(role="student")],
                active=True,
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(contact_update(42, "79990000000"))
    await handler.handle(update(42, "Тест", "m2"))

    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, 1)
        assert identity is not None
        assert identity.max_user_id == 42
    assert api.sent == [
        (42, "Авторизация завершена. Добро пожаловать в «КРиТ»!"),
    ]
    await engine.dispose()


async def test_unknown_contact_tells_user_to_contact_administrator() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    api = FakeApi()
    handler = EchoHandler(sessions=sessions, api=api)  # type: ignore[arg-type]

    await handler.handle(contact_update(42, "79990000000"))

    assert api.sent == [
        (
            42,
            "Номер не найден в базе «КРиТ». Для регистрации обратитесь к администратору.",
        )
    ]
    async with sessions() as session:
        attempt = await session.get(AccessAttempt, 42)
        assert attempt is not None
        assert attempt.verified_phone == "+79990000000"
    await engine.dispose()


async def test_registration_waits_for_required_channel_membership() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.commit()
        person_id = person.id
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
        required_channel_link="https://max.ru/channel",
    )

    await handler.handle(contact_update(42, "79990000000"))
    async with sessions() as session:
        pending = await session.scalar(select(MaxRegistrationPending))
        assert pending is not None
        assert pending.person_id == person_id
        assert pending.status == "pending"
        assert await session.get(PersonMaxIdentity, person_id) is None
    assert "подпишитесь на канал" in api.sent[-1][1]

    await handler.handle(callback_update(42, "registration:check", "cb-missing"))
    assert api.callback_answers[-1] == ("cb-missing", "Подписка пока не найдена", None)

    api.members.add(42)
    await handler.handle(callback_update(42, "registration:check", "cb-member"))
    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, person_id)
        pending = await session.scalar(select(MaxRegistrationPending))
        assert identity is not None
        assert identity.max_user_id == 42
        assert identity.channel_subscription_status == "member"
        assert pending is not None and pending.status == "completed"
    assert api.callback_answers[-1] == ("cb-member", "Регистрация завершена", None)
    await engine.dispose()


async def test_unsubscribed_registration_repeats_prompt_for_every_interaction() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        session.add(
            Person(
                full_name="Иван Иванов",
                phone="+79990000000",
                max_auth_phone="+79990000000",
                role_links=[PersonRole(role="student")],
                active=True,
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
        required_channel_link="https://max.ru/channel",
    )

    await handler.handle(contact_update(42, "79990000000"))
    await handler.handle(update(42, "Ещё сообщение", "message-after-contact"))
    await handler.handle(bot_started_update(42))
    await handler.handle(callback_update(42, "registration:check", "cb-still-missing"))

    assert len(api.sent) == 4
    assert all("подпишитесь на канал" in text for _, text in api.sent)
    assert all(
        attachments
        and attachments[0]["payload"]["buttons"][-1][0]["payload"]
        == "registration:check"
        for attachments in api.sent_attachments
    )
    assert api.contact_requests == []
    assert api.callback_answers[-1] == (
        "cb-still-missing",
        "Подписка пока не найдена",
        None,
    )
    await engine.dispose()


async def test_authorized_user_must_restore_subscription_before_interacting() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.max_auth_phone,
                max_user_id=42,
                channel_subscription_status="member",
            )
        )
        await session.commit()
        person_id = person.id
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
        required_channel_link="https://max.ru/channel",
    )

    await handler.handle(update(42, "Сообщение", "authorized-but-unsubscribed"))
    await handler.handle(callback_update(42, "registration:check", "cb-resubscribe-missing"))

    assert len(api.sent) == 2
    assert all("подпишитесь на канал" in text for _, text in api.sent)
    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, person_id)
        assert identity is not None
        assert identity.channel_subscription_status == "missing"
        assert await session.scalar(select(func.count(CommunicationMessage.id))) == 0

    api.members.add(42)
    await handler.handle(callback_update(42, "registration:check", "cb-resubscribed"))

    assert api.callback_answers[-1] == (
        "cb-resubscribed",
        "Подписка подтверждена",
        None,
    )
    assert api.contact_requests == []
    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, person_id)
        assert identity is not None
        assert identity.channel_subscription_status == "member"
    await engine.dispose()


async def test_archived_pending_client_is_not_registered_as_access_attempt_again() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Архивный Клиент",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
            archived_at=utcnow(),
        )
        session.add(person)
        await session.flush()
        session.add(
            MaxRegistrationPending(
                person_id=person.id,
                max_user_id=42,
                verified_phone=person.max_auth_phone,
                required_channel_id=-100500,
                status="archived",
                expires_at=utcnow() + timedelta(days=7),
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(bot_started_update(42))
    await handler.handle(update(42, "Сообщение", "archived-message"))
    await handler.handle(callback_update(42, "registration:check", "archived-callback"))

    assert api.contact_requests == []
    assert api.sent == [
        (42, "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."),
        (42, "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."),
        (42, "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."),
    ]
    assert api.callback_answers[-1] == (
        "archived-callback",
        "Доступ к боту приостановлен",
        None,
    )
    async with sessions() as session:
        assert await session.get(AccessAttempt, 42) is None
    await engine.dispose()


async def test_archived_legacy_max_id_is_not_registered_as_access_attempt_again() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        session.add(
            Person(
                full_name="Архивный Клиент",
                phone="+79990000000",
                max_auth_phone="+79990000000",
                max_user_id=42,
                role_links=[PersonRole(role="student")],
                active=True,
                archived_at=utcnow(),
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(bot_started_update(42))
    await handler.handle(update(42, "Сообщение", "archived-legacy-message"))

    assert api.contact_requests == []
    assert api.sent == [
        (42, "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."),
        (42, "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."),
    ]
    async with sessions() as session:
        assert await session.get(AccessAttempt, 42) is None
    await engine.dispose()


async def test_registration_check_without_verified_phone_requests_contact_again() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(callback_update(42, "registration:check", "cb-no-phone"))

    assert api.callback_answers[-1] == (
        "cb-no-phone",
        "Снова отправьте контакт для регистрации",
        None,
    )
    assert api.contact_requests == [42]
    await engine.dispose()


async def test_channel_is_checked_only_after_verified_known_phone() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        session.add(
            Person(
                full_name="Иван Иванов",
                phone="+79990000000",
                max_auth_phone="+79990000000",
                role_links=[PersonRole(role="student")],
                active=True,
            )
        )
        await session.commit()
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(bot_started_update(42))
    await handler.handle(update(42, "Произвольное сообщение", "plain-message"))
    assert api.member_checks == []

    await handler.handle(contact_update(42, "79990000000", mid="verified-contact"))
    assert api.member_checks == [(-100500, [42])]
    await engine.dispose()


async def test_registration_uses_channel_link_from_max_and_marks_direct_member() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            max_auth_phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.commit()
        person_id = person.id
    api = FakeApi()
    api.chat_link = "https://max.ru/join/channel-from-api"
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(contact_update(42, "79990000000"))

    buttons = api.sent_attachments[-1][0]["payload"]["buttons"]
    assert buttons[0][0] == {
        "type": "link",
        "text": "Подписаться на канал",
        "url": "https://max.ru/join/channel-from-api",
    }

    api.members.add(43)
    await handler.handle(contact_update(43, "79990000000", mid="contact-43"))
    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, person_id)
        assert identity is not None
        assert identity.max_user_id == 43
        assert identity.channel_subscription_status == "member"
        assert identity.channel_subscription_checked_at is not None
    await engine.dispose()


async def test_user_removed_marks_subscription_missing_only_for_required_channel() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        person = Person(
            full_name="Иван Иванов",
            phone="+79990000000",
            role_links=[PersonRole(role="student")],
            active=True,
        )
        session.add(person)
        await session.flush()
        session.add(
            PersonMaxIdentity(
                person_id=person.id,
                verified_phone=person.phone,
                max_user_id=42,
                channel_subscription_status="member",
            )
        )
        await session.commit()
        person_id = person.id
    api = FakeApi()
    handler = EchoHandler(
        sessions=sessions,
        api=api,  # type: ignore[arg-type]
        required_channel_id=-100500,
    )

    await handler.handle(
        {"update_type": "user_removed", "chat_id": -999, "user_id": 42}
    )
    async with sessions() as session:
        assert (await session.get(PersonMaxIdentity, person_id)).channel_subscription_status == (
            "member"
        )

    await handler.handle(
        {"update_type": "user_removed", "chat_id": -100500, "user_id": 42}
    )
    async with sessions() as session:
        assert (await session.get(PersonMaxIdentity, person_id)).channel_subscription_status == (
            "missing"
        )
    await engine.dispose()
