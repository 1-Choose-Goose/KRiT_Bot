from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from krit_bot.config import extract_first_token
from krit_bot.db import Base, Person, PersonRole, build_session_factory
from krit_bot.handler import EchoHandler, parse_message_created
from krit_bot.learning_models import PersonMaxIdentity


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.contact_requests: list[int] = []

    async def send_text(self, *, user_id: int, text: str, attachments=None) -> dict:
        self.sent.append((user_id, text))
        return {}

    async def request_contact(self, *, user_id: int, text: str) -> dict:
        self.contact_requests.append(user_id)
        return {}

    def verify_contact(self, *, vcf_info: str, signature: str) -> bool:
        return signature == "valid"


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


def test_parse_message_created() -> None:
    parsed = parse_message_created(update(42))
    assert parsed is not None
    assert parsed.user_id == 42
    assert parsed.display_name == "Иван Иванов"


def test_token_file_uses_only_first_non_empty_line() -> None:
    raw = "max-token\n\nANOTHER_SECRET=must-not-be-used\n"
    assert extract_first_token(raw) == "max-token"


async def test_only_authorized_user_receives_echo() -> None:
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

    assert api.sent == [(42, "Эхо: Раз")]
    assert api.contact_requests == [99]
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

    assert api.sent == [(42, "Эхо: Привет")]
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
        (42, "Эхо: Тест"),
    ]
    await engine.dispose()
