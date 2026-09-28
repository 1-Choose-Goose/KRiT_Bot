from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import SyndicationJob, utcnow
from .max_api import MaxApiClient, MaxApiError

log = structlog.get_logger()


class VkApiError(RuntimeError):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class PermanentSyndicationError(RuntimeError):
    pass


@dataclass(slots=True)
class PreparedPost:
    text: str
    media: list[tuple[str, str, str]]
    unsupported: list[str]
    source_created_at: datetime | None


class VkApiClient:
    def __init__(self, *, token: str, base_url: str, version: str) -> None:
        self._token = token
        self._version = version
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30, connect=10)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_wall_post(self, *, community_id: int, post_id: int) -> dict[str, Any]:
        payload = await self._method(
            "wall.getById",
            {"posts": f"-{community_id}_{post_id}"},
        )
        body = payload.get("response", {})
        items = body.get("items", []) if isinstance(body, dict) else body
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise VkApiError("VK post was not found", transient=False)
        return items[0]

    async def get_long_poll_server(self, *, community_id: int) -> dict[str, str]:
        payload = await self._method(
            "groups.getLongPollServer",
            {"group_id": community_id},
        )
        result = payload.get("response")
        if not isinstance(result, dict):
            raise VkApiError("VK Long Poll session is missing", transient=True)
        values = {name: result.get(name) for name in ("server", "key", "ts")}
        if not all(isinstance(value, (str, int)) and str(value) for value in values.values()):
            raise VkApiError("VK Long Poll session is incomplete", transient=True)
        return {name: str(value) for name, value in values.items()}

    async def poll(self, *, server: str, key: str, ts: str, wait: int = 25) -> dict[str, Any]:
        if not server.startswith("https://"):
            raise VkApiError("VK Long Poll server is not HTTPS", transient=False)
        try:
            response = await self._client.get(
                server,
                params={"act": "a_check", "key": key, "ts": ts, "wait": wait},
                timeout=httpx.Timeout(wait + 10, connect=10),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VkApiError("VK Long Poll network error", transient=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise VkApiError(f"VK Long Poll HTTP {response.status_code}", transient=True)
        if response.is_error:
            raise VkApiError(f"VK Long Poll HTTP {response.status_code}", transient=False)
        payload = response.json()
        if not isinstance(payload, dict):
            raise VkApiError("VK Long Poll returned invalid JSON", transient=True)
        return payload

    async def _method(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"/{method}",
                params={
                    **parameters,
                    "access_token": self._token,
                    "v": self._version,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VkApiError("VK API network error", transient=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise VkApiError(f"VK API HTTP {response.status_code}", transient=True)
        if response.is_error:
            raise VkApiError(f"VK API HTTP {response.status_code}", transient=False)
        payload = response.json()
        if not isinstance(payload, dict):
            raise VkApiError("VK API returned invalid JSON", transient=True)
        if "error" in payload:
            error = payload["error"]
            code = int(error.get("error_code", 0)) if isinstance(error, dict) else 0
            transient = code in {1, 6, 9, 10, 29}
            raise VkApiError(f"VK API error {code}", transient=transient)
        return payload


class VkLongPollWorker:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        vk: VkApiClient,
        community_id: int,
        max_chat_id: int,
        wait_seconds: int = 25,
    ) -> None:
        self.sessions = sessions
        self.vk = vk
        self.community_id = community_id
        self.max_chat_id = max_chat_id
        self.wait_seconds = wait_seconds

    async def run(self) -> None:
        session: dict[str, str] | None = None
        delay = 1
        while True:
            try:
                if session is None:
                    session = await self.vk.get_long_poll_server(community_id=self.community_id)
                    log.info("vk_long_poll_session_started", community_id=self.community_id)
                payload = await self.vk.poll(
                    server=session["server"],
                    key=session["key"],
                    ts=session["ts"],
                    wait=self.wait_seconds,
                )
                failed = payload.get("failed")
                if failed == 1:
                    if payload.get("ts") is not None:
                        session["ts"] = str(payload["ts"])
                    delay = 1
                    continue
                if failed in {2, 3}:
                    session = None
                    delay = 1
                    continue
                if payload.get("ts") is not None:
                    session["ts"] = str(payload["ts"])
                for event in payload.get("updates") or []:
                    if isinstance(event, dict):
                        await self.handle_event(event)
                delay = 1
            except asyncio.CancelledError:
                raise
            except VkApiError as exc:
                log.warning(
                    "vk_long_poll_failed",
                    community_id=self.community_id,
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                if not exc.transient:
                    session = None
            except Exception as exc:
                log.exception(
                    "vk_long_poll_unexpected_error",
                    community_id=self.community_id,
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def handle_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") != "wall_post_new":
            return False
        try:
            group_id = int(event.get("group_id"))
        except (TypeError, ValueError):
            return False
        if group_id != self.community_id:
            return False
        post = event.get("object")
        if isinstance(post, dict) and isinstance(post.get("object"), dict):
            post = post["object"]
        if not isinstance(post, dict):
            return False
        try:
            post_id = int(post.get("id"))
            owner_id = int(post.get("owner_id"))
        except (TypeError, ValueError):
            return False
        if owner_id != -self.community_id:
            return False
        safe_event = dict(event)
        job_id, created = await register_vk_event(
            self.sessions,
            community_id=group_id,
            post_id=post_id,
            max_chat_id=self.max_chat_id,
            event_id=str(event.get("event_id")) if event.get("event_id") else None,
            raw_event=safe_event,
        )
        log.info(
            "vk_long_poll_event_registered",
            community_id=group_id,
            post_id=post_id,
            job_id=job_id,
            duplicate=not created,
        )
        return created


def _best_photo_url(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes")
    if not isinstance(sizes, list):
        return None
    valid = [item for item in sizes if isinstance(item, dict) and item.get("url")]
    if not valid:
        return None
    best = max(valid, key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0)))
    return str(best["url"])


def prepare_post(post: dict[str, Any]) -> PreparedPost:
    media: list[tuple[str, str, str]] = []
    unsupported: list[str] = []
    for attachment in post.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("type", "unknown"))
        value = attachment.get(kind)
        if kind == "photo" and isinstance(value, dict):
            url = _best_photo_url(value)
            if url:
                media.append(("image", url, f"vk-photo-{value.get('id', len(media))}.jpg"))
            else:
                unsupported.append("photo_without_url")
        elif kind == "doc" and isinstance(value, dict) and value.get("url"):
            extension = str(value.get("ext") or "bin").lower()
            media_type = "image" if extension == "gif" else "file"
            filename = str(value.get("title") or f"vk-document.{extension}")
            media.append((media_type, str(value["url"]), filename))
        elif kind == "link" and isinstance(value, dict) and value.get("url"):
            media.append(("share", str(value["url"]), "link"))
        else:
            unsupported.append(kind)
    created = post.get("date")
    source_created_at = (
        datetime.fromtimestamp(int(created), tz=UTC) if isinstance(created, (int, float)) else None
    )
    return PreparedPost(
        text=str(post.get("text") or ""),
        media=media,
        unsupported=unsupported,
        source_created_at=source_created_at,
    )


async def register_vk_event(
    sessions: async_sessionmaker[AsyncSession],
    *,
    community_id: int,
    post_id: int,
    max_chat_id: int,
    event_id: str | None,
    raw_event: dict[str, Any],
) -> tuple[int, bool]:
    job = SyndicationJob(
        integration_id="vk_to_max",
        source="vk",
        vk_community_id=community_id,
        vk_post_id=post_id,
        max_chat_id=max_chat_id,
        vk_event_id=event_id,
        raw_event=raw_event,
        status="pending",
    )
    async with sessions() as session:
        session.add(job)
        try:
            await session.commit()
            await session.refresh(job)
            return job.id, True
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(SyndicationJob.id).where(
                    SyndicationJob.source == "vk",
                    SyndicationJob.vk_community_id == community_id,
                    SyndicationJob.vk_post_id == post_id,
                    SyndicationJob.max_chat_id == max_chat_id,
                )
            )
            if existing is None:
                raise
            return existing, False


class SyndicationWorker:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        vk: VkApiClient,
        max_api: MaxApiClient,
        poll_seconds: float,
        max_attempts: int,
        download_limit_bytes: int,
    ) -> None:
        self.sessions = sessions
        self.vk = vk
        self.max_api = max_api
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.download_limit_bytes = download_limit_bytes
        self._download = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10), follow_redirects=True
        )

    async def close(self) -> None:
        await self.vk.close()
        await self._download.aclose()

    async def recover(self) -> None:
        async with self.sessions() as session:
            await session.execute(
                update(SyndicationJob)
                .where(SyndicationJob.status == "processing")
                .values(status="retry", next_attempt_at=utcnow(), updated_at=utcnow())
            )
            await session.commit()

    async def run(self) -> None:
        await self.recover()
        while True:
            try:
                processed = await self.process_next()
                if not processed:
                    await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("syndication_worker_error", error=type(exc).__name__)
                await asyncio.sleep(self.poll_seconds)

    async def process_next(self) -> bool:
        now = utcnow()
        async with self.sessions() as session:
            query = (
                select(SyndicationJob)
                .where(
                    SyndicationJob.status.in_(("pending", "retry")),
                    or_(
                        SyndicationJob.next_attempt_at.is_(None),
                        SyndicationJob.next_attempt_at <= now,
                    ),
                )
                .order_by(SyndicationJob.received_at, SyndicationJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = await session.scalar(query)
            if job is None:
                return False
            job.status = "processing"
            job.attempts += 1
            job.updated_at = now
            await session.commit()
            job_id = job.id
        await self._process(job_id)
        return True

    async def _process(self, job_id: int) -> None:
        async with self.sessions() as session:
            job = await session.get(SyndicationJob, job_id)
            if job is None:
                return
            try:
                log.info(
                    "syndication_processing",
                    community_id=job.vk_community_id,
                    post_id=job.vk_post_id,
                    attempt=job.attempts,
                )
                try:
                    post = await self.vk.get_wall_post(
                        community_id=job.vk_community_id, post_id=job.vk_post_id
                    )
                except VkApiError as exc:
                    if exc.transient:
                        raise
                    post = self._post_from_event(job)
                    if post is None:
                        raise
                    log.warning(
                        "vk_post_api_fallback_to_event",
                        community_id=job.vk_community_id,
                        post_id=job.vk_post_id,
                        error_type=type(exc).__name__,
                    )
                prepared = prepare_post(post)
                job.source_created_at = prepared.source_created_at
                attachments = await self._attachments(prepared)
                if not prepared.text and not attachments:
                    job.status = "skipped"
                    job.last_error = "No supported content"
                else:
                    result = await self.max_api.send_to_chat(
                        chat_id=job.max_chat_id,
                        text=prepared.text,
                        attachments=attachments,
                    )
                    message = result.get("message", result)
                    message_id = message.get("body", {}).get("mid") or message.get("mid")
                    if not message_id:
                        raise MaxApiError("MAX response does not contain message ID")
                    job.max_message_id = str(message_id)
                    job.status = "published"
                    job.published_at = utcnow()
                    job.last_error = None
                job.updated_at = utcnow()
                await session.commit()
                log.info(
                    "syndication_finished",
                    community_id=job.vk_community_id,
                    post_id=job.vk_post_id,
                    status=job.status,
                    max_message_id=job.max_message_id,
                    attachments=len(attachments),
                    unsupported=prepared.unsupported,
                )
            except (VkApiError, MaxApiError, httpx.HTTPError, PermanentSyndicationError) as exc:
                transient = getattr(exc, "transient", False)
                if isinstance(exc, httpx.HTTPError):
                    transient = True
                if transient and job.attempts < self.max_attempts:
                    job.status = "retry"
                    job.next_attempt_at = utcnow() + timedelta(seconds=min(300, 2**job.attempts))
                else:
                    job.status = "failed"
                    job.next_attempt_at = None
                job.last_error = str(exc)[:2000]
                job.updated_at = utcnow()
                await session.commit()
                log.warning(
                    "syndication_failed",
                    community_id=job.vk_community_id,
                    post_id=job.vk_post_id,
                    status=job.status,
                    attempt=job.attempts,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                if job.attempts < self.max_attempts:
                    job.status = "retry"
                    job.next_attempt_at = utcnow() + timedelta(seconds=min(300, 2**job.attempts))
                else:
                    job.status = "failed"
                    job.next_attempt_at = None
                job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                job.updated_at = utcnow()
                await session.commit()
                log.exception(
                    "syndication_unexpected_error",
                    community_id=job.vk_community_id,
                    post_id=job.vk_post_id,
                    status=job.status,
                    attempt=job.attempts,
                )

    @staticmethod
    def _post_from_event(job: SyndicationJob) -> dict[str, Any] | None:
        event_object = job.raw_event.get("object")
        if isinstance(event_object, dict) and isinstance(event_object.get("object"), dict):
            event_object = event_object["object"]
        if not isinstance(event_object, dict):
            return None
        try:
            post_id = int(event_object.get("id"))
            owner_id = int(event_object.get("owner_id"))
        except (TypeError, ValueError):
            return None
        if post_id != job.vk_post_id or owner_id != -job.vk_community_id:
            return None
        return event_object

    async def _attachments(self, post: PreparedPost) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        file_items: list[tuple[str, str, str]] = []
        for media_type, url, filename in post.media:
            if media_type == "share":
                attachments.append({"type": "share", "payload": {"url": url}})
                continue
            if media_type == "file":
                file_items.append((media_type, url, filename))
                continue
            if len(attachments) >= 12:
                break
            token = await self._download_and_upload(media_type, url, filename)
            attachments.append({"type": media_type, "payload": {"token": token}})
        # MAX permits only one file and it cannot be combined with image/video attachments.
        if file_items and not attachments:
            media_type, url, filename = file_items[0]
            token = await self._download_and_upload(media_type, url, filename)
            attachments.append({"type": media_type, "payload": {"token": token}})
        return attachments[:12]

    async def _download_and_upload(self, media_type: str, url: str, filename: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise PermanentSyndicationError("Media URL is not HTTPS")
        async with self._download.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", "0") or 0)
            if declared > self.download_limit_bytes:
                raise PermanentSyndicationError("Media file exceeds configured size limit")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.download_limit_bytes:
                    raise PermanentSyndicationError("Media file exceeds configured size limit")
            content_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0]
        endpoint = await self.max_api.create_upload(media_type=media_type)
        upload_url = endpoint.get("url")
        if not upload_url:
            raise MaxApiError("MAX upload endpoint has no URL")
        uploaded = await self.max_api.upload_media(
            upload_url=str(upload_url),
            content=bytes(data),
            filename=filename,
            content_type=str(content_type or "application/octet-stream"),
        )
        token = uploaded.get("token")
        if not token and isinstance(uploaded.get("photos"), dict):
            for value in uploaded["photos"].values():
                if isinstance(value, dict) and value.get("token"):
                    token = value["token"]
                    break
        if not token:
            raise MaxApiError("MAX upload response has no token")
        return str(token)
