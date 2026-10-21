from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .communication_models import (
    InteractionRequest,
    InteractionRequestLesson,
    InteractionResponse,
    MaxRegistrationPending,
    MaxSubscriptionPrompt,
)
from .communications import record_message, save_interaction_response
from .db import (
    AccessAttempt,
    Person,
    bind_max_user_by_phone,
    claim_message,
    is_authorized,
    normalize_phone,
    register_access_attempt,
    utcnow,
)
from .learning_models import Lesson, PersonMaxIdentity
from .max_api import MaxApiClient, MaxApiError

log = structlog.get_logger()

AUTHORIZATION_CONTACT_PROMPT = (
    "Для авторизации нажмите кнопку «Поделиться номером». "
    "Отправьте номер телефона, привязанный к вашему аккаунту MAX "
    "и указанный при записи в «КРиТ»."
)
ARCHIVED_ACCESS_MESSAGE = (
    "Доступ к боту приостановлен. Обратитесь к администратору «КРиТ»."
)
ARCHIVED_ACCESS_NOTIFICATION = "Доступ к боту приостановлен"
REGISTRATION_SUCCESS_MESSAGE = "Авторизация завершена. Добро пожаловать в «КРиТ»!"
SUBSCRIPTION_PROMPT_TEXT = (
    "Для завершения регистрации подпишитесь на канал КРиТ, "
    "затем нажмите «Проверить подписку»."
)
SUBSCRIPTION_CONFIRMED_MESSAGE = "Подписка подтверждена. Авторизация завершена."
REGISTRATION_AUTOMATIC_CHECK_OFFSETS = tuple(
    timedelta(minutes=minutes) for minutes in (1, 2, 5, 10, 15)
)


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
    message_id: str | None
    message_text: str | None


@dataclass(frozen=True, slots=True)
class BotStartedUser:
    user_id: int
    display_name: str | None
    username: str | None


def parse_bot_started(update: dict[str, Any]) -> BotStartedUser | None:
    if update.get("update_type") != "bot_started":
        return None
    user = update.get("user") or {}
    user_id = user.get("user_id")
    if not isinstance(user_id, int) or user.get("is_bot"):
        return None
    name = user.get("name")
    display_name = name.strip() if isinstance(name, str) and name.strip() else None
    if display_name is None:
        display_name = (
            " ".join(
                part
                for part in (user.get("first_name"), user.get("last_name"))
                if isinstance(part, str) and part.strip()
            ).strip()
            or None
        )
    username = user.get("username")
    return BotStartedUser(
        user_id=user_id,
        display_name=display_name,
        username=username if isinstance(username, str) else None,
    )


def parse_message_callback(update: dict[str, Any]) -> IncomingCallback | None:
    if update.get("update_type") != "message_callback":
        return None
    callback = update.get("callback") or {}
    user = callback.get("user") or update.get("user") or {}
    callback_id = callback.get("callback_id")
    payload = callback.get("payload")
    user_id = user.get("user_id")
    if not callback_id or not isinstance(payload, str) or not isinstance(user_id, int):
        return None
    message = update.get("message") or {}
    body = message.get("body") or {}
    raw_message_id = body.get("mid")
    message_id = raw_message_id if isinstance(raw_message_id, str) else None
    text = body.get("text")
    message_text = text if isinstance(text, str) else None
    return IncomingCallback(str(callback_id), user_id, payload, message_id, message_text)


def _callback_message_without_keyboard(callback: IncomingCallback) -> dict[str, Any]:
    message: dict[str, Any] = {"attachments": []}
    if callback.message_text is not None:
        message["text"] = callback.message_text
    return message


def _sent_message_id(response: dict[str, Any]) -> str | None:
    message = response.get("message")
    if not isinstance(message, dict):
        return None
    body = message.get("body")
    if not isinstance(body, dict):
        return None
    message_id = body.get("mid")
    return message_id if isinstance(message_id, str) and message_id else None


def _positive_callback_id(value: str) -> int | None:
    if not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


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

    async def _registration_context(
        self, session: AsyncSession, user_id: int, *, lock_pending: bool = False
    ) -> tuple[MaxRegistrationPending | None, Person | None]:
        pending_statement = select(MaxRegistrationPending).where(
            MaxRegistrationPending.max_user_id == user_id
        )
        if lock_pending:
            pending_statement = pending_statement.with_for_update()
        pending = await session.scalar(pending_statement)
        person = await session.get(Person, pending.person_id) if pending is not None else None
        if person is None:
            person = await session.scalar(
                select(Person)
                .join(PersonMaxIdentity, PersonMaxIdentity.person_id == Person.id)
                .where(PersonMaxIdentity.max_user_id == user_id)
            )
        if person is None:
            person = await session.scalar(
                select(Person).where(Person.max_user_id == user_id)
            )
        return pending, person

    async def _finalize_pending_registration(
        self,
        session: AsyncSession,
        pending: MaxRegistrationPending,
        user_id: int,
    ) -> bool:
        if pending.status != "pending":
            return False
        result = await bind_max_user_by_phone(
            session,
            phone=pending.verified_phone,
            max_user_id=user_id,
        )
        if result not in {"linked", "already_linked"}:
            pending.status = "failed"
            pending.next_automatic_check_at = None
            return False
        pending.status = "completed"
        pending.next_automatic_check_at = None
        identity = await session.get(PersonMaxIdentity, pending.person_id)
        if identity is not None:
            identity.channel_subscription_status = "member"
            identity.channel_subscription_checked_at = utcnow()
        return True

    async def _complete_pending_from_membership_event(self, user_id: int) -> bool:
        should_clear_prompt = False
        async with self._sessions() as session:
            pending, _ = await self._registration_context(
                session, user_id, lock_pending=True
            )
            if pending is None or pending.status != "pending":
                identity = await session.scalar(
                    select(PersonMaxIdentity).where(
                        PersonMaxIdentity.max_user_id == user_id
                    )
                )
                if identity is not None:
                    identity.channel_subscription_status = "member"
                    identity.channel_subscription_checked_at = utcnow()
                    await session.commit()
                    should_clear_prompt = True
                if should_clear_prompt:
                    await self._clear_subscription_prompt(user_id)
                return False
            completed = await self._finalize_pending_registration(
                session, pending, user_id
            )
            await session.commit()
        if completed:
            await self._clear_subscription_prompt(user_id)
            await self._api.send_text(
                user_id=user_id,
                text=REGISTRATION_SUCCESS_MESSAGE,
            )
            log.info("user_authorized_from_channel_event", max_user_id=user_id)
        return completed

    async def process_due_registration_checks(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> int:
        if self._required_channel_id is None:
            return 0
        checked_at = now or utcnow()
        async with self._sessions() as session:
            due_user_ids = list(
                await session.scalars(
                    select(MaxRegistrationPending.max_user_id)
                    .where(
                        MaxRegistrationPending.status == "pending",
                        MaxRegistrationPending.next_automatic_check_at.is_not(None),
                        MaxRegistrationPending.next_automatic_check_at <= checked_at,
                    )
                    .order_by(MaxRegistrationPending.next_automatic_check_at)
                    .limit(limit)
                )
            )

        processed = 0
        for user_id in due_user_ids:
            try:
                is_member = await self._is_channel_member(user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "registration_automatic_membership_check_failed",
                    max_user_id=user_id,
                )
                continue

            async with self._sessions() as session:
                pending, _ = await self._registration_context(
                    session, user_id, lock_pending=True
                )
                next_check_at = (
                    pending.next_automatic_check_at if pending is not None else None
                )
                if next_check_at is not None and next_check_at.tzinfo is None:
                    next_check_at = next_check_at.replace(tzinfo=UTC)
                if (
                    pending is None
                    or pending.status != "pending"
                    or next_check_at is None
                    or next_check_at > checked_at
                ):
                    continue
                expires_at = pending.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at < checked_at:
                    pending.status = "expired"
                    pending.next_automatic_check_at = None
                    await session.commit()
                    continue
                pending.automatic_check_count += 1
                pending.last_checked_at = checked_at
                if is_member:
                    await self._finalize_pending_registration(session, pending, user_id)
                elif pending.automatic_check_count < len(
                    REGISTRATION_AUTOMATIC_CHECK_OFFSETS
                ):
                    created_at = pending.created_at
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=UTC)
                    pending.next_automatic_check_at = (
                        created_at
                        + REGISTRATION_AUTOMATIC_CHECK_OFFSETS[
                            pending.automatic_check_count
                        ]
                    )
                else:
                    pending.next_automatic_check_at = None
                completed = pending.status == "completed"
                await session.commit()
            processed += 1
            if completed:
                await self._clear_subscription_prompt(user_id)
                await self._api.send_text(
                    user_id=user_id,
                    text=REGISTRATION_SUCCESS_MESSAGE,
                )
                log.info("user_authorized_automatically", max_user_id=user_id)
        return processed

    async def run_registration_checks(self, *, poll_seconds: float = 15.0) -> None:
        while True:
            try:
                await self.process_due_registration_checks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("registration_automatic_check_iteration_failed")
            await asyncio.sleep(poll_seconds)

    async def _deactivate_subscription_prompt(self, message_id: str) -> bool:
        try:
            result = await self._api.edit_message(
                message_id=message_id,
                text=SUBSCRIPTION_CONFIRMED_MESSAGE,
                attachments=[],
            )
        except MaxApiError:
            log.exception(
                "subscription_prompt_cleanup_failed",
                message_id=message_id,
            )
            return False
        if result.get("success") is not True:
            log.warning(
                "subscription_prompt_cleanup_rejected",
                message_id=message_id,
                response_message=result.get("message"),
            )
            return False
        return True

    async def _clear_subscription_prompt(
        self, user_id: int, *, updated_message_id: str | None = None
    ) -> None:
        async with self._sessions() as session:
            prompt = await session.scalar(
                select(MaxSubscriptionPrompt)
                .where(MaxSubscriptionPrompt.max_user_id == user_id)
                .with_for_update()
            )
            if prompt is None:
                return
            should_edit = prompt.message_id != updated_message_id
            if should_edit and not await self._deactivate_subscription_prompt(
                prompt.message_id
            ):
                return
            await session.delete(prompt)
            await session.commit()

    async def _send_subscription_prompt(self, user_id: int) -> None:
        buttons = []
        channel_link = await self._get_required_channel_link()
        if channel_link:
            buttons.append(
                [
                    {
                        "type": "link",
                        "text": "Подписаться на канал",
                        "url": channel_link,
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
        async with self._sessions() as session:
            prompt = await session.scalar(
                select(MaxSubscriptionPrompt)
                .where(MaxSubscriptionPrompt.max_user_id == user_id)
                .with_for_update()
            )
            if prompt is not None and not await self._deactivate_subscription_prompt(
                prompt.message_id
            ):
                return
            result = await self._api.send_text(
                user_id=user_id,
                text=SUBSCRIPTION_PROMPT_TEXT,
                attachments=[
                    {
                        "type": "inline_keyboard",
                        "payload": {"buttons": buttons},
                    }
                ],
            )
            message_id = _sent_message_id(result)
            if message_id is None:
                log.warning(
                    "subscription_prompt_message_id_missing",
                    max_user_id=user_id,
                )
                if prompt is not None:
                    await session.delete(prompt)
                await session.commit()
                return
            if prompt is None:
                prompt = MaxSubscriptionPrompt(
                    max_user_id=user_id,
                    message_id=message_id,
                )
                session.add(prompt)
            else:
                prompt.message_id = message_id
                prompt.updated_at = utcnow()
            await session.commit()

    async def _authorized_subscription_is_valid(
        self, session: AsyncSession, user_id: int
    ) -> bool:
        if self._required_channel_id is None:
            return True
        identity = await session.scalar(
            select(PersonMaxIdentity)
            .join(Person, Person.id == PersonMaxIdentity.person_id)
            .where(
                PersonMaxIdentity.max_user_id == user_id,
                Person.active.is_(True),
                Person.bot_access_enabled.is_(True),
                Person.archived_at.is_(None),
            )
        )
        if identity is None:
            return True
        subscribed = await self._is_channel_member(user_id)
        identity.channel_subscription_status = "member" if subscribed else "missing"
        identity.channel_subscription_checked_at = utcnow()
        await session.commit()
        if not subscribed:
            await self._send_subscription_prompt(user_id)
        else:
            await self._clear_subscription_prompt(user_id)
        return subscribed

    async def _handle_known_registration_state(
        self, session: AsyncSession, user_id: int
    ) -> bool:
        pending, person = await self._registration_context(
            session, user_id, lock_pending=True
        )
        if person is not None and person.archived_at is not None:
            await session.execute(
                delete(AccessAttempt).where(AccessAttempt.max_user_id == user_id)
            )
            await session.commit()
            await self._api.send_text(
                user_id=user_id,
                text=ARCHIVED_ACCESS_MESSAGE,
            )
            return True
        if person is not None and (not person.active or not person.bot_access_enabled):
            await session.execute(
                delete(AccessAttempt).where(AccessAttempt.max_user_id == user_id)
            )
            await session.commit()
            await self._api.send_text(
                user_id=user_id,
                text="Доступ к боту отключён. Обратитесь к администратору.",
            )
            return True
        if pending is None or pending.status != "pending":
            return False
        expires_at = pending.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < utcnow():
            pending.status = "expired"
            await session.commit()
            return False
        if not await self._is_channel_member(user_id):
            pending.last_checked_at = utcnow()
            await session.commit()
            await self._send_subscription_prompt(user_id)
            return True
        if not await self._finalize_pending_registration(session, pending, user_id):
            await session.commit()
            return False
        await session.commit()
        await self._clear_subscription_prompt(user_id)
        await self._api.send_text(
            user_id=user_id,
            text=REGISTRATION_SUCCESS_MESSAGE,
        )
        return True

    async def handle(self, update: dict[str, Any]) -> None:
        if (
            update.get("update_type") == "user_added"
            and self._required_channel_id is not None
            and update.get("chat_id") == self._required_channel_id
        ):
            user = update.get("user") or update.get("recipient") or {}
            user_id = update.get("user_id") or user.get("user_id")
            if isinstance(user_id, int):
                await self._complete_pending_from_membership_event(user_id)
            return
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
        started_user = parse_bot_started(update)
        if started_user is not None:
            async with self._sessions() as session:
                if await is_authorized(session, started_user.user_id):
                    if not await self._authorized_subscription_is_valid(
                        session, started_user.user_id
                    ):
                        return
                    await self._api.send_text(
                        user_id=started_user.user_id,
                        text="Вы уже авторизованы в «КРиТ».",
                    )
                    return
                if await self._handle_known_registration_state(
                    session, started_user.user_id
                ):
                    return
                await register_access_attempt(
                    session,
                    max_user_id=started_user.user_id,
                    display_name=started_user.display_name,
                    username=started_user.username,
                )
                await session.commit()
            await self._api.request_contact(
                user_id=started_user.user_id,
                text=AUTHORIZATION_CONTACT_PROMPT,
            )
            log.info("authorization_requested", max_user_id=started_user.user_id)
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
                if not await self._authorized_subscription_is_valid(
                    session, incoming.user_id
                ):
                    return
                person_id = await session.scalar(
                    select(PersonMaxIdentity.person_id).where(
                        PersonMaxIdentity.max_user_id == incoming.user_id
                    )
                )
                reason_saved = False
                if person_id is not None and incoming.text:
                    pending_reason = await session.scalar(
                        select(InteractionRequest)
                        .where(
                            InteractionRequest.expects_reason_from_person_id == person_id,
                            InteractionRequest.status.in_(["active", "answered"]),
                        )
                        .order_by(InteractionRequest.updated_at.desc())
                    )
                    message_type = (
                        "interaction_reason"
                        if pending_reason is not None
                        else "command"
                        if incoming.text.startswith("/")
                        else "text"
                    )
                    await record_message(
                        session,
                        person_id=person_id,
                        direction="inbound",
                        text=incoming.text,
                        delivery_status="received",
                        max_message_id=incoming.message_id,
                        message_type=message_type,
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
                            reason_saved = True
                await session.commit()
                if reason_saved:
                    await self._api.send_text(
                        user_id=incoming.user_id,
                        text="Спасибо! Причина сохранена.",
                    )
                return

            if await self._handle_known_registration_state(session, incoming.user_id):
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
                archived_person = await session.scalar(
                    select(Person).where(
                        Person.max_auth_phone == phone,
                        Person.archived_at.is_not(None),
                    )
                )
                if archived_person is not None:
                    await session.execute(
                        delete(AccessAttempt).where(
                            AccessAttempt.max_user_id == incoming.user_id
                        )
                    )
                    await session.commit()
                    await self._api.send_text(
                        user_id=incoming.user_id,
                        text=ARCHIVED_ACCESS_MESSAGE,
                    )
                    return
                await register_access_attempt(
                    session,
                    max_user_id=incoming.user_id,
                    display_name=incoming.display_name,
                    username=incoming.username,
                    verified_phone=phone,
                )
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
                            created_at = utcnow()
                            pending = MaxRegistrationPending(
                                person_id=candidate.id,
                                max_user_id=incoming.user_id,
                                verified_phone=phone,
                                required_channel_id=self._required_channel_id,
                                created_at=created_at,
                                expires_at=created_at + timedelta(days=7),
                                automatic_check_count=0,
                                next_automatic_check_at=(
                                    created_at
                                    + REGISTRATION_AUTOMATIC_CHECK_OFFSETS[0]
                                ),
                            )
                            session.add(pending)
                        else:
                            created_at = utcnow()
                            pending.person_id = candidate.id
                            pending.verified_phone = phone
                            pending.status = "pending"
                            pending.created_at = created_at
                            pending.expires_at = created_at + timedelta(days=7)
                            pending.automatic_check_count = 0
                            pending.next_automatic_check_at = (
                                created_at + REGISTRATION_AUTOMATIC_CHECK_OFFSETS[0]
                            )
                        await session.commit()
                        await self._send_subscription_prompt(incoming.user_id)
                        return
                result = await bind_max_user_by_phone(
                    session, phone=phone, max_user_id=incoming.user_id
                )
                if result in {"linked", "already_linked"}:
                    if self._required_channel_id is not None:
                        identity = await session.scalar(
                            select(PersonMaxIdentity).where(
                                PersonMaxIdentity.max_user_id == incoming.user_id
                            )
                        )
                        if identity is not None:
                            identity.channel_subscription_status = "member"
                            identity.channel_subscription_checked_at = utcnow()
                    await session.commit()
                    await self._api.send_text(
                        user_id=incoming.user_id,
                        text=REGISTRATION_SUCCESS_MESSAGE,
                    )
                    log.info("user_authorized", max_user_id=incoming.user_id)
                    return

                await session.commit()
                message = (
                    "Этот номер уже привязан к другому аккаунту MAX. Обратитесь к администратору."
                    if result == "belongs_to_another_user"
                    else (
                        "Номер не найден в базе «КРиТ». "
                        "Для регистрации обратитесь к администратору."
                    )
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
                text=AUTHORIZATION_CONTACT_PROMPT,
            )
            log.info("authorization_requested", max_user_id=incoming.user_id)

    async def _get_required_channel_link(self) -> str | None:
        if self._required_channel_link:
            return self._required_channel_link
        if self._required_channel_id is None:
            return None
        try:
            chat = await self._api.get_chat(chat_id=self._required_channel_id)
        except MaxApiError as exc:
            log.warning(
                "required_channel_link_unavailable",
                chat_id=self._required_channel_id,
                error=str(exc),
            )
            return None
        link = chat.get("link")
        if not isinstance(link, str) or not link.strip():
            return None
        self._required_channel_link = link.strip()
        return self._required_channel_link

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
        request_id = _positive_callback_id(parts[1]) if len(parts) > 1 else None
        if parts == ["registration", "check"]:
            await self._handle_registration_callback(callback)
            return
        async with self._sessions() as session:
            if not await self._authorized_subscription_is_valid(
                session, callback.user_id
            ):
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Сначала подтвердите подписку на канал",
                )
                return
            pending, person = await self._registration_context(
                session, callback.user_id, lock_pending=True
            )
            if person is not None and person.archived_at is not None:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification=ARCHIVED_ACCESS_NOTIFICATION,
                )
                await self._api.send_text(
                    user_id=callback.user_id,
                    text=ARCHIVED_ACCESS_MESSAGE,
                )
                return
            if pending is not None and pending.status == "pending":
                if not await self._is_channel_member(callback.user_id):
                    pending.last_checked_at = utcnow()
                    await session.commit()
                    await self._api.answer_callback(
                        callback_id=callback.callback_id,
                        notification="Сначала подтвердите подписку на канал",
                    )
                    await self._send_subscription_prompt(callback.user_id)
                    return
        if len(parts) == 3 and parts[0] == "reason" and parts[2] in {"write", "skip"}:
            if request_id is not None:
                await self._handle_reason_callback(callback, request_id, parts[2])
                return
        if len(parts) == 3 and parts[0] == "interaction" and parts[2] == "partial":
            if request_id is not None:
                await self._handle_partial_choice(callback, request_id)
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
        lesson_id = _positive_callback_id(parts[3]) if lesson_answer else None
        if (
            (not lesson_answer and not whole_answer)
            or request_id is None
            or (lesson_answer and lesson_id is None)
        ):
            await self._api.answer_callback(
                callback_id=callback.callback_id,
                notification="Кнопка больше не поддерживается",
            )
            return
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            callback_message = _callback_message_without_keyboard(callback)
            person_id = await self._authorized_callback_person_id(session, callback.user_id)
            if person_id is None:
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Доступ к боту отключён. Обратитесь к администратору.",
                )
                return
            request = await session.get(InteractionRequest, request_id)
            if request is None or request.recipient_person_id != person_id:
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
                if lesson_answer and lesson_answers is not None:
                    remaining = (
                        await session.execute(
                            select(InteractionRequestLesson.lesson_id, Lesson)
                            .join(Lesson, Lesson.id == InteractionRequestLesson.lesson_id)
                            .where(InteractionRequestLesson.request_id == request.id)
                            .order_by(Lesson.start_at)
                        )
                    ).all()
                    remaining_buttons = [
                        [
                            {
                                "type": "callback",
                                "text": f"Да · {lesson.start_at:%H:%M}",
                                "payload": (
                                    f"interaction:{request.id}:lesson:{lesson_id}:yes"
                                ),
                            },
                            {
                                "type": "callback",
                                "text": f"Нет · {lesson.start_at:%H:%M}",
                                "payload": (
                                    f"interaction:{request.id}:lesson:{lesson_id}:no"
                                ),
                            },
                        ]
                        for lesson_id, lesson in remaining
                        if str(lesson_id) not in lesson_answers
                    ]
                    callback_message = {
                        **(
                            {"text": callback.message_text}
                            if callback.message_text is not None
                            else {}
                        ),
                        "attachments": (
                            [
                                {
                                    "type": "inline_keyboard",
                                    "payload": {"buttons": remaining_buttons},
                                }
                            ]
                            if remaining_buttons
                            else []
                        )
                    }
                await record_message(
                    session,
                    person_id=person_id,
                    direction="inbound",
                    text=(
                        f"Ответ по занятию: {'Да' if answer == 'yes' else 'Нет'}"
                        if lesson_answer
                        else f"Ответ на запрос: {'Да' if answer == 'yes' else 'Нет'}"
                    ),
                    delivery_status="received",
                    message_type="interaction_callback",
                    interaction_request_id=request.id,
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
            message=callback_message,
        )

    async def _handle_partial_choice(self, callback: IncomingCallback, request_id: int) -> None:
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            person_id = await self._authorized_callback_person_id(session, callback.user_id)
            if person_id is None:
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Доступ к боту отключён. Обратитесь к администратору.",
                )
                return
            request = await session.get(InteractionRequest, request_id)
            if request is None or request.recipient_person_id != person_id:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Этот запрос предназначен другому получателю",
                )
                return
            await record_message(
                session,
                person_id=person_id,
                direction="inbound",
                text="Выбрано: ответить отдельно по каждому занятию",
                delivery_status="received",
                message_type="interaction_callback",
                interaction_request_id=request.id,
            )
            rows = (
                await session.execute(
                    select(InteractionRequestLesson.lesson_id, Lesson)
                    .join(Lesson, Lesson.id == InteractionRequestLesson.lesson_id)
                    .where(InteractionRequestLesson.request_id == request_id)
                    .order_by(Lesson.start_at)
                )
            ).all()
            await session.commit()
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
            callback_id=callback.callback_id,
            notification="Выберите ответ по каждому занятию",
            message=_callback_message_without_keyboard(callback),
        )

    async def _handle_reason_callback(
        self, callback: IncomingCallback, request_id: int, action: str
    ) -> None:
        async with self._sessions() as session:
            if not await claim_message(session, f"callback:{callback.callback_id}"):
                return
            person_id = await self._authorized_callback_person_id(session, callback.user_id)
            if person_id is None:
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Доступ к боту отключён. Обратитесь к администратору.",
                )
                return
            request = await session.get(InteractionRequest, request_id)
            if request is None or request.recipient_person_id != person_id:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Этот запрос предназначен другому получателю",
                )
                return
            request.expects_reason_from_person_id = person_id if action == "write" else None
            request.updated_at = utcnow()
            await record_message(
                session,
                person_id=person_id,
                direction="inbound",
                text=(
                    "Выбрано: указать причину"
                    if action == "write"
                    else "Выбрано: не указывать причину"
                ),
                delivery_status="received",
                message_type="interaction_callback",
                interaction_request_id=request.id,
            )
            await session.commit()
        await self._api.answer_callback(
            callback_id=callback.callback_id,
            notification=(
                "Напишите причину следующим сообщением"
                if action == "write"
                else "Ответ сохранён без причины"
            ),
            message=_callback_message_without_keyboard(callback),
        )

    @staticmethod
    async def _authorized_callback_person_id(
        session: AsyncSession, max_user_id: int
    ) -> int | None:
        return await session.scalar(
            select(PersonMaxIdentity.person_id)
            .join(Person, Person.id == PersonMaxIdentity.person_id)
            .where(
                PersonMaxIdentity.max_user_id == max_user_id,
                Person.active.is_(True),
                Person.bot_access_enabled.is_(True),
                Person.archived_at.is_(None),
            )
        )

    async def _handle_registration_callback(self, callback: IncomingCallback) -> None:
        async with self._sessions() as session:
            identity = await session.scalar(
                select(PersonMaxIdentity)
                .join(Person, Person.id == PersonMaxIdentity.person_id)
                .where(
                    PersonMaxIdentity.max_user_id == callback.user_id,
                    Person.active.is_(True),
                    Person.bot_access_enabled.is_(True),
                    Person.archived_at.is_(None),
                )
            )
            if identity is not None:
                subscribed = await self._is_channel_member(callback.user_id)
                identity.channel_subscription_status = (
                    "member" if subscribed else "missing"
                )
                identity.channel_subscription_checked_at = utcnow()
                await session.commit()
                if not subscribed:
                    await self._api.answer_callback(
                        callback_id=callback.callback_id,
                        notification="Подписка пока не найдена",
                    )
                    await self._send_subscription_prompt(callback.user_id)
                    return
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Подписка подтверждена",
                    message={
                        "text": SUBSCRIPTION_CONFIRMED_MESSAGE,
                        "attachments": [],
                    },
                )
                await self._clear_subscription_prompt(
                    callback.user_id,
                    updated_message_id=callback.message_id,
                )
                return
            pending, person = await self._registration_context(session, callback.user_id)
            if person is not None and person.archived_at is not None:
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification=ARCHIVED_ACCESS_NOTIFICATION,
                )
                await self._api.send_text(
                    user_id=callback.user_id,
                    text=ARCHIVED_ACCESS_MESSAGE,
                )
                return
            expires_at = pending.expires_at if pending is not None else None
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if (
                pending is None
                or pending.status != "pending"
                or expires_at is None
                or expires_at < utcnow()
            ):
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Снова отправьте контакт для регистрации",
                )
                await self._api.request_contact(
                    user_id=callback.user_id,
                    text=(
                        "Для продолжения регистрации повторно нажмите кнопку ниже "
                        "и передайте номер телефона, привязанный к вашему аккаунту MAX."
                    ),
                )
                return
            pending.last_checked_at = utcnow()
            if not await self._is_channel_member(callback.user_id):
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Подписка пока не найдена",
                )
                await self._send_subscription_prompt(callback.user_id)
                return
            if not await self._finalize_pending_registration(
                session, pending, callback.user_id
            ):
                await session.commit()
                await self._api.answer_callback(
                    callback_id=callback.callback_id,
                    notification="Не удалось завершить регистрацию",
                )
                return
            await session.commit()
        await self._api.answer_callback(
            callback_id=callback.callback_id,
            notification="Регистрация завершена",
            message={
                "text": SUBSCRIPTION_CONFIRMED_MESSAGE,
                "attachments": [],
            },
        )
        await self._clear_subscription_prompt(
            callback.user_id,
            updated_message_id=callback.message_id,
        )
        await self._api.send_text(
            user_id=callback.user_id,
            text=REGISTRATION_SUCCESS_MESSAGE,
        )
