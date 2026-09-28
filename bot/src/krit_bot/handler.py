from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import (
    bind_max_user_by_phone,
    claim_message,
    is_authorized,
    normalize_phone,
    register_access_attempt,
)
from .max_api import MaxApiClient

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    user_id: int
    text: str | None
    display_name: str | None
    username: str | None
    contact_vcf: str | None
    contact_hash: str | None


def parse_message_created(update: dict[str, Any]) -> IncomingMessage | None:
    if update.get("update_type") != "message_created":
        return None
    message = update.get("message") or {}
    sender = message.get("sender") or {}
    body = message.get("body") or {}
    user_id = sender.get("user_id")
    message_id = body.get("mid")
    text = body.get("text")
    if sender.get("is_bot") or not isinstance(user_id, int) or not message_id:
        return None
    attachments = body.get("attachments") or []
    contact_payload: dict[str, Any] = {}
    for attachment in attachments:
        if isinstance(attachment, dict) and attachment.get("type") == "contact":
            payload = attachment.get("payload")
            if isinstance(payload, dict):
                contact_payload = payload
                break
    clean_text = text.strip() if isinstance(text, str) and text.strip() else None
    contact_vcf = contact_payload.get("vcf_info")
    contact_hash = contact_payload.get("hash")
    if clean_text is None and not (
        isinstance(contact_vcf, str) and isinstance(contact_hash, str)
    ):
        return None
    display_name = (
        " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
        or None
    )
    return IncomingMessage(
        message_id=str(message_id),
        user_id=user_id,
        text=clean_text,
        display_name=display_name,
        username=sender.get("username"),
        contact_vcf=contact_vcf if isinstance(contact_vcf, str) else None,
        contact_hash=contact_hash if isinstance(contact_hash, str) else None,
    )


def phone_from_vcard(vcf_info: str) -> str | None:
    match = re.search(r"(?im)^TEL(?:;[^:]*)?:(.+)$", vcf_info)
    if match is None:
        return None
    try:
        return normalize_phone(match.group(1).strip())
    except ValueError:
        return None


class EchoHandler:
    def __init__(self, *, sessions: async_sessionmaker[AsyncSession], api: MaxApiClient) -> None:
        self._sessions = sessions
        self._api = api

    async def handle(self, update: dict[str, Any]) -> None:
        incoming = parse_message_created(update)
        if incoming is None:
            return

        async with self._sessions() as session:
            if not await claim_message(session, incoming.message_id):
                return
            if await is_authorized(session, incoming.user_id):
                await session.commit()
                if incoming.text:
                    await self._api.send_text(
                        user_id=incoming.user_id, text=f"Эхо: {incoming.text}"
                    )
                    log.info(
                        "echo_sent",
                        max_user_id=incoming.user_id,
                        message_id=incoming.message_id,
                    )
                return

            contact_is_valid = bool(
                incoming.contact_vcf
                and incoming.contact_hash
                and self._api.verify_contact(
                    vcf_info=incoming.contact_vcf,
                    signature=incoming.contact_hash,
                )
            )
            phone = phone_from_vcard(incoming.contact_vcf or "") if contact_is_valid else None
            if phone is not None:
                result = await bind_max_user_by_phone(
                    session, phone=phone, max_user_id=incoming.user_id
                )
                if result in {"linked", "already_linked"}:
                    await session.commit()
                    await self._api.send_text(
                        user_id=incoming.user_id,
                        text="Авторизация завершена. Добро пожаловать в «КРиТ»!",
                    )
                    log.info("user_authorized", max_user_id=incoming.user_id)
                    return

                await register_access_attempt(
                    session,
                    max_user_id=incoming.user_id,
                    display_name=incoming.display_name,
                    username=incoming.username,
                )
                await session.commit()
                message = (
                    "Этот номер уже привязан к другому аккаунту MAX. "
                    "Обратитесь к администратору."
                    if result == "belongs_to_another_user"
                    else "Номер не найден в базе «КРиТ». Обратитесь к администратору."
                )
                await self._api.send_text(user_id=incoming.user_id, text=message)
                return

            await register_access_attempt(
                session,
                max_user_id=incoming.user_id,
                display_name=incoming.display_name,
                username=incoming.username,
            )
            await session.commit()
            await self._api.request_contact(
                user_id=incoming.user_id,
                text=(
                    "Для авторизации нажмите кнопку ниже и передайте номер телефона, "
                    "который привязан к вашему аккаунту MAX и указан в карточке клиента «КРиТ»."
                ),
            )
            log.info("authorization_requested", max_user_id=incoming.user_id)
