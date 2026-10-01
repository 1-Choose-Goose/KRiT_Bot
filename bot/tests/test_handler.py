from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from krit_bot.communication_models import CommunicationMessage, MaxRegistrationPending
from krit_bot.config import extract_first_token
from krit_bot.db import Base, Person, PersonRole, build_session_factory
from krit_bot.handler import EchoHandler, parse_message_created
from krit_bot.learning_models import PersonMaxIdentity


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.contact_requests: list[int] = []
        self.members: set[int] = set()
        self.callback_answers: list[tuple[str, str]] = []

    async def send_text(self, *, user_id: int, text: str, attachments=None) -> dict:
        self.sent.append((user_id, text))
        return {}

    async def request_contact(self, *, user_id: int, text: str) -> dict:
        self.contact_requests.append(user_id)
        return {}

    def verify_contact(self, *, vcf_info: str, signature: str) -> bool:
        return signature == "valid"

    async def get_chat_members(self, *, chat_id: int, user_ids: list[int]) -> dict:
        return {
            "members": [
                {"user_id": user_id} for user_id in user_ids if user_id in self.members
            ]
        }

    async def answer_callback(self, *, callback_id: str, notification: str) -> dict:
        self.callback_answers.append((callback_id, notification))
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


def callback_update(user_id: int, payload: str, callback_id: str = "cb-1") -> dict:
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": user_id},
        },
    }


def test_parse_message_created() -> None:
    parsed = parse_message_created(update(42))
    assert parsed is not None
    assert parsed.user_id == 42
    assert parsed.display_name == "Иван Иванов"


def test_token_file_uses_only_first_non_empty_line() -> None:
    raw = "max-token\n\nANOTHER_SECRET=must-not-be-used\n"
    assert extract_first_token(raw) == "max-token"


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
    assert api.callback_answers[-1] == ("cb-missing", "Подписка пока не найдена")

    api.members.add(42)
    await handler.handle(callback_update(42, "registration:check", "cb-member"))
    async with sessions() as session:
        identity = await session.get(PersonMaxIdentity, person_id)
        pending = await session.scalar(select(MaxRegistrationPending))
        assert identity is not None
        assert identity.max_user_id == 42
        assert identity.channel_subscription_status == "member"
        assert pending is not None and pending.status == "completed"
    assert api.callback_answers[-1] == ("cb-member", "Регистрация завершена")
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
