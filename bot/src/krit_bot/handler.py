from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .communication_models import (
    InteractionRequest,
    InteractionRequestLesson,
    InteractionResponse,
    MaxRegistrationPending,
)
from .communications import record_message, save_interaction_response
from .db import (
    Person,
    bind_max_user_by_phone,
    claim_message,
    is_authorized,
    normalize_phone,
    register_access_attempt,
    utcnow,
)
from .learning_models import Lesson, PersonMaxIdentity
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


@dataclass(frozen=True, slots=True)
class IncomingCallback:
    callback_id: str
    user_id: int
    payload: str


def parse_message_callback(update: dict[str, Any]) -> IncomingCallback | None:
    if update.get("update_type") != "message_callback":
        return None
    callback = update.get("callback") or {}
    user = callback.get("user") or {}
    callback_id = callback.get("callback_id")
    payload = callback.get("payload")
    user_id = user.get("user_id")
    if not callback_id or not isinstance(payload, str) or not isinstance(user_id, int):
        return None
    return IncomingCallback(str(callback_id), user_id, payload)


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
    if clean_text is None and not (isinstance(contact_vcf, str) and isinstance(contact_hash, str)):
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
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        api: MaxApiClient,
        required_channel_id: int | None = None,
        required_channel_link: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._api = api
        self._required_channel_id = required_channel_id
        self._required_channel_link = required_channel_link

    async def handle(self, update: dict[str, Any]) -> None:
        if (
            update.get("update_type") == "user_removed"
            and self._required_channel_id is not None
            and update.get("chat_id") == self._required_channel_id
        ):
            user = update.get("user") or update.get("recipient") or {}
            user_id = update.get("user_id") or user.get("user_id")
            if isinstance(user_id, int):
                async with self._sessions() as session:
                    identity = await session.scalar(
                        select(PersonMaxIdentity).where(
                            PersonMaxIdentity.max_user_id == user_id
                        )
                    )
                    if identity is not None:
                        identity.channel_subscription_status = "missing"
                        identity.channel_subscription_checked_at = utcnow()
                        await session.commit()
            return
        callback = parse_message_callback(update)
        if callback is not None:
            await self._handle_callback(callback)
            return
        incoming = parse_message_created(update)
        if incoming is None:
            return

        async with self._sessions() as session:
            if not await claim_message(session, incoming.message_id):
                return
            if await is_authorized(session, incoming.user_id):
                person_id = await session.scalar(
                    select(PersonMaxIdentity.person_id).where(
                        PersonMaxIdentity.max_user_id == incoming.user_id
                    )
                )
                if person_id is not None and incoming.text:
                    pending_reason = await session.scalar(
                        select(InteractionRequest)
                        .where(
                            InteractionRequest.expects_reason_from_person_id == person_id,
                            InteractionRequest.status.in_(["active", "answered"]),
                        )
                        .order_by(InteractionRequest.updated_at.desc())
                    )
                    await record_message(
                        session,
                        person_id=person_id,
                        direction="inbound",
                        text=incoming.text,
                        delivery_status="received",
                        max_message_id=incoming.message_id,
                        message_type=(
                            "interaction_reason" if pending_reason is not None else "text"
                        ),
                        interaction_request_id=(
                            pending_reason.id if pending_reason is not None else None
                        ),
                    )
                    if pending_reason is not None:
                        response = await session.scalar(
                            select(InteractionResponse).where(
                                InteractionResponse.request_id == pending_reason.id,
                                InteractionResponse.respondent_person_id == person_id,
                            )
                        )
                        if response is not None:
                            response.reason = incoming.text
                            response.updated_at = utcnow()
                            pending_reason.expects_reason_from_person_id = None
                await session.commit()
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
                candidate = await session.scalar(
                    select(Person).where(
                        Person.max_auth_phone == phone,
                        Person.active.is_(True),
                        Person.archived_at.is_(None),
                    )
                )
                if candidate is not None and self._required_channel_id is not None:
                    if not await self._is_channel_member(incoming.user_id):
                        pending = await session.scalar(
                            select(MaxRegistrationPending).where(
                                MaxRegistrationPending.max_user_id == incoming.user_id
                            )
                        )
                        if pending is None:
                            pending = MaxRegistrationPending(
                                person_id=candidate.id,
                                max_user_id=incoming.user_id,
                                verified_phone=phone,
                                required_channel_id=self._required_channel_id,
                                expires_at=utcnow() + timedelta(days=7),
                            )
                            session.add(pending)
                        else:
                            pending.person_id = candidate.id
                            pending.verified_phone = phone
                            pending.status = "pending"
                            pending.expires_at = utcnow() + timedelta(days=7)
                        await session.commit()
                        buttons = []
                        if self._required_channel_link:
                            buttons.append(
                                [
                                    {
                                        "type": "link",
                                        "text": "Подписаться на канал",
                                        "url": self._required_channel_link,
                                    }
                                ]
                            )
                        buttons.append(
                            [
                                {
                                    "type": "callback",
                                    "text": "Проверить подписку",
                                    "payload": "registration:check",
                                }
                            ]
                        )
                        await self._api.send_text(
                            user_id=incoming.user_id,
                            text=(
                                "Для завершения регистрации подпишитесь на канал КРиТ, "
                                "затем нажмите «Проверить подписку»."
                            ),
                            attachments=[
                                {
                                    "type": "inline_keyboard",
                                    "payload": {"buttons": buttons},
                                }
                            ],
                        )
                        return
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
                    "Этот номер уже привязан к другому аккаунту MAX. Обратитесь к администратору."
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

    async def _is_channel_member(self, user_id: int) -> bool:
        if self._required_channel_id is None:
            return True
        result = await self._api.get_chat_members(
            chat_id=self._required_channel_id, user_ids=[user_id]
        )
        members = result.get("members")
        if not isinstance(members, list):
            members = result.get("users")
        return bool(
            isinstance(members, list)
            and any(isinstance(item, dict) and item.get("user_id") == user_id for item in members)
        )

    async def _handle_callback(self, callback: IncomingCallback) -> None:
        parts = callback.payload.split(":")
        if parts == ["registration", "check"]:
            await self._handle_registration_callback(callback)
            return
        if len(parts) == 3 and parts[0] == "reason" and parts[2] in {"write", "skip"}:
            await self._handle_reason_callback(callback, int(parts[1]), parts[2])
            return
        if len(parts) == 3 and parts[0] == "interaction" and parts[2] == "partial":
            await self._handle_partial_choice(callback, int(parts[1]))
            return
        lesson_answer = (
            len(parts) == 5
            and parts[0] == "interaction"
            and parts[2] == "lesson"
            and parts[4] in {"yes", "no"}
        )
        whole_answer = (
            len(parts) == 3
            and parts[0] == "interaction"
            and parts[2] in {"yes", "no"}
        )
        if not lesson_answer and not whole_answer:
            await self._api.answer_callback(
                callback_id=callback.callback_id,
                notification="Кнопка больше не поддерживается",
            )
            return
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            person_id = await session.scalar(
                select(PersonMaxIdentity.person_id).where(
                    PersonMaxIdentity.max_user_id == callback.user_id
                )
            )
            request = await session.get(InteractionRequest, int(parts[1]))
            if person_id is None or request is None or request.recipient_person_id != person_id:
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Этот запрос предназначен другому получателю",
                )
                return
            try:
                answer = parts[4] if lesson_answer else parts[2]
                lesson_answers: dict[str, str] | None = None
                if lesson_answer:
                    lesson_id = int(parts[3])
                    current_response = await session.scalar(
                        select(InteractionResponse).where(
                            InteractionResponse.request_id == request.id,
                            InteractionResponse.respondent_person_id == person_id,
                            InteractionResponse.respondent_context == request.recipient_context,
                        )
                    )
                    lesson_answers = dict(
                        current_response.lesson_answers if current_response else {}
                    )
                    lesson_answers[str(lesson_id)] = answer
                await save_interaction_response(
                    session,
                    request=request,
                    respondent_person_id=person_id,
                    respondent_context=request.recipient_context,
                    answer="partial" if lesson_answer else answer,
                    lesson_answers=lesson_answers,
                )
            except (ValueError, PermissionError) as exc:
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id, notification=str(exc)
                )
                return
            await session.commit()
        if answer == "no":
            await self._api.send_text(
                user_id=callback.user_id,
                text="Хотите указать причину отсутствия?",
                attachments=[
                    {
                        "type": "inline_keyboard",
                        "payload": {
                            "buttons": [
                                [
                                    {
                                        "type": "callback",
                                        "text": "Указать",
                                        "payload": f"reason:{request.id}:write",
                                    },
                                    {
                                        "type": "callback",
                                        "text": "Пропустить",
                                        "payload": f"reason:{request.id}:skip",
                                    },
                                ]
                            ]
                        },
                    }
                ],
            )
        await self._api.answer_callback(
            callback_id=callback.callback_id,
            notification="Ответ сохранён",
        )

    async def _handle_partial_choice(self, callback: IncomingCallback, request_id: int) -> None:
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            person_id = await session.scalar(
                select(PersonMaxIdentity.person_id).where(
                    PersonMaxIdentity.max_user_id == callback.user_id
                )
            )
            request = await session.get(InteractionRequest, request_id)
            if person_id is None or request is None or request.recipient_person_id != person_id:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Этот запрос предназначен другому получателю",
                )
                return
            rows = (
                await session.execute(
                    select(InteractionRequestLesson.lesson_id, Lesson)
                    .join(Lesson, Lesson.id == InteractionRequestLesson.lesson_id)
                    .where(InteractionRequestLesson.request_id == request_id)
                    .order_by(Lesson.start_at)
                )
            ).all()
        buttons = []
        lines = ["Отметьте каждое занятие:"]
        for lesson_id, lesson in rows:
            lines.append(f"{lesson.subject_name_snapshot} · {lesson.start_at:%d.%m %H:%M}")
            buttons.append(
                [
                    {
                        "type": "callback",
                        "text": f"Да · {lesson.start_at:%H:%M}",
                        "payload": f"interaction:{request_id}:lesson:{lesson_id}:yes",
                    },
                    {
                        "type": "callback",
                        "text": f"Нет · {lesson.start_at:%H:%M}",
                        "payload": f"interaction:{request_id}:lesson:{lesson_id}:no",
                    },
                ]
            )
        await self._api.send_text(
            user_id=callback.user_id,
            text="\n".join(lines),
            attachments=[{"type": "inline_keyboard", "payload": {"buttons": buttons}}],
        )
        await self._api.answer_callback(
            callback_id=callback.callback_id, notification="Выберите ответ по каждому занятию"
        )

    async def _handle_reason_callback(
        self, callback: IncomingCallback, request_id: int, action: str
    ) -> None:
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            person_id = await session.scalar(
                select(PersonMaxIdentity.person_id).where(
                    PersonMaxIdentity.max_user_id == callback.user_id
                )
            )
            request = await session.get(InteractionRequest, request_id)
            if person_id is None or request is None or request.recipient_person_id != person_id:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Этот запрос предназначен другому получателю",
                )
                return
            request.expects_reason_from_person_id = person_id if action == "write" else None
            request.updated_at = utcnow()
            await session.commit()
        await self._api.answer_callback(
            callback_id=callback.callback_id,
            notification=(
                "Напишите причину следующим сообщением"
                if action == "write"
                else "Ответ сохранён без причины"
            ),
        )

    async def _handle_registration_callback(self, callback: IncomingCallback) -> None:
        async with self._sessions() as session:
            pending = await session.scalar(
                select(MaxRegistrationPending).where(
                    MaxRegistrationPending.max_user_id == callback.user_id,
                    MaxRegistrationPending.status == "pending",
                )
            )
            if pending is None or pending.expires_at < utcnow():
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Снова отправьте контакт для регистрации",
                )
                return
            pending.last_checked_at = utcnow()
            if not await self._is_channel_member(callback.user_id):
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Подписка пока не найдена",
                )
                return
            result = await bind_max_user_by_phone(
                session,
                phone=pending.verified_phone,
                max_user_id=callback.user_id,
            )
            if result not in {"linked", "already_linked"}:
                pending.status = "failed"
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Не удалось завершить регистрацию",
                )
                return
            pending.status = "completed"
            identity = await session.get(PersonMaxIdentity, pending.person_id)
            if identity is not None:
                identity.channel_subscription_status = "member"
                identity.channel_subscription_checked_at = utcnow()
            await session.commit()
        await self._api.answer_callback(
            callback_id=callback.callback_id,
            notification="Регистрация завершена",
        )
        await self._api.send_text(
            user_id=callback.user_id,
            text="Авторизация завершена. Добро пожаловать в «КРиТ»!",
        )
