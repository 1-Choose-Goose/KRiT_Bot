from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from krit_bot.db import SyndicationJob, build_engine, build_session_factory, ensure_schema
from krit_bot.max_api import MaxApiError
from krit_bot.syndication import (
    PreparedPost,
    SyndicationWorker,
    VkApiError,
    VkLongPollWorker,
    prepare_post,
    register_vk_event,
)


class FakeVk:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result

    async def get_wall_post(self, **_: Any) -> dict[str, Any]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        return None


class FakeMax:
    def __init__(self, result: dict[str, Any] | Exception | None = None) -> None:
        self.result = result or {"message": {"body": {"mid": "mid.test"}}}
        self.sent: list[dict[str, Any]] = []

    async def send_to_chat(self, **payload: Any) -> dict[str, Any]:
        self.sent.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SequenceLongPollVk:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.sessions = 0
        self.seen_ts: list[str] = []

    async def get_long_poll_server(self, **_: Any) -> dict[str, str]:
        self.sessions += 1
        return {"server": "https://lp.vk.test", "key": "key", "ts": "10"}

    async def poll(self, *, ts: str, **_: Any) -> dict[str, Any]:
        self.seen_ts.append(ts)
        if not self.responses:
            raise asyncio.CancelledError
        return self.responses.pop(0)


@pytest.fixture
async def sessions(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await ensure_schema(engine)
    yield build_session_factory(engine)
    await engine.dispose()


def test_text_post() -> None:
    prepared = prepare_post({"text": "Первый абзац\n\nВторой 😊", "attachments": []})
    assert prepared.text == "Первый абзац\n\nВторой 😊"
    assert prepared.media == []


def test_one_photo_uses_largest_size() -> None:
    prepared = prepare_post(
        {"attachments": [{"type": "photo", "photo": {"id": 7, "sizes": [
            {"url": "https://vk.test/s.jpg", "width": 100, "height": 100},
            {"url": "https://vk.test/l.jpg", "width": 1000, "height": 800},
        ]}}]}
    )
    assert prepared.media == [("image", "https://vk.test/l.jpg", "vk-photo-7.jpg")]


def test_multiple_photos_keep_order() -> None:
    prepared = prepare_post({"attachments": [
        {"type": "photo", "photo": {
            "id": 1, "sizes": [{"url": "https://vk/1", "width": 1, "height": 1}]
        }},
        {"type": "photo", "photo": {
            "id": 2, "sizes": [{"url": "https://vk/2", "width": 1, "height": 1}]
        }},
    ]})
    assert [item[1] for item in prepared.media] == ["https://vk/1", "https://vk/2"]


def test_text_and_photo() -> None:
    prepared = prepare_post({"text": "Текст", "attachments": [
        {"type": "photo", "photo": {
            "sizes": [{"url": "https://vk/image", "width": 1, "height": 1}]
        }}
    ]})
    assert prepared.text == "Текст"
    assert prepared.media[0][0] == "image"


def test_document_gif_link_and_unsupported_attachment() -> None:
    prepared = prepare_post({"attachments": [
        {"type": "doc", "doc": {"url": "https://vk/a.gif", "ext": "gif", "title": "a.gif"}},
        {"type": "doc", "doc": {"url": "https://vk/a.pdf", "ext": "pdf", "title": "a.pdf"}},
        {"type": "link", "link": {"url": "https://example.test"}},
        {"type": "poll", "poll": {"id": 1}},
    ]})
    assert [item[0] for item in prepared.media] == ["image", "file", "share"]
    assert prepared.unsupported == ["poll"]


async def _add_job(sessions, post_id: int = 1) -> int:
    job_id, _ = await register_vk_event(
        sessions,
        community_id=225565387,
        post_id=post_id,
        max_chat_id=777,
        event_id=f"event-{post_id}",
        raw_event={"type": "wall_post_new"},
    )
    return job_id


@pytest.mark.asyncio
async def test_database_deduplicates_same_post(sessions) -> None:
    first = await _add_job(sessions)
    second, created = await register_vk_event(
        sessions,
        community_id=225565387,
        post_id=1,
        max_chat_id=777,
        event_id="duplicate",
        raw_event={},
    )
    assert second == first
    assert created is False


@pytest.mark.asyncio
async def test_long_poll_registers_only_expected_wall_event(sessions) -> None:
    worker = VkLongPollWorker(
        sessions=sessions,
        vk=SequenceLongPollVk([]),  # type: ignore[arg-type]
        community_id=225565387,
        max_chat_id=777,
    )
    event = {
        "type": "wall_post_new",
        "event_id": "event",
        "group_id": 225565387,
        "object": {"id": 42, "owner_id": -225565387},
    }
    assert await worker.handle_event(event) is True
    assert await worker.handle_event(event) is False
    assert await worker.handle_event({**event, "group_id": 1}) is False
    assert await worker.handle_event({**event, "object": {"id": 43, "owner_id": -1}}) is False
    async with sessions() as session:
        jobs = list((await session.scalars(select(SyndicationJob))).all())
        assert len(jobs) == 1
        assert jobs[0].vk_post_id == 42


@pytest.mark.asyncio
async def test_long_poll_failed_one_uses_new_ts(sessions) -> None:
    vk = SequenceLongPollVk([{"failed": 1, "ts": "11"}])
    worker = VkLongPollWorker(
        sessions=sessions,
        vk=vk,  # type: ignore[arg-type]
        community_id=225565387,
        max_chat_id=777,
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert vk.sessions == 1
    assert vk.seen_ts == ["10", "11"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [2, 3])
async def test_long_poll_expired_session_is_refreshed(sessions, failed: int) -> None:
    vk = SequenceLongPollVk([{"failed": failed}])
    worker = VkLongPollWorker(
        sessions=sessions,
        vk=vk,  # type: ignore[arg-type]
        community_id=225565387,
        max_chat_id=777,
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert vk.sessions == 2
    assert vk.seen_ts == ["10", "10"]


def _worker(sessions, vk, max_api, *, attempts=3) -> SyndicationWorker:
    worker = SyndicationWorker(
        sessions=sessions,
        vk=vk,
        max_api=max_api,
        poll_seconds=0.01,
        max_attempts=attempts,
        download_limit_bytes=1024,
    )

    async def no_media(_: PreparedPost) -> list[dict[str, Any]]:
        return []

    worker._attachments = no_media  # type: ignore[method-assign]
    return worker


@pytest.mark.asyncio
async def test_published_and_message_id_saved(sessions) -> None:
    await _add_job(sessions)
    max_api = FakeMax()
    worker = _worker(sessions, FakeVk({"text": "Готово", "date": 1}), max_api)
    assert await worker.process_next() is True
    async with sessions() as session:
        job = await session.scalar(select(SyndicationJob))
        assert job.status == "published"
        assert job.max_message_id == "mid.test"
        assert job.source_created_at.replace(tzinfo=UTC) == datetime.fromtimestamp(1, tz=UTC)
    assert max_api.sent[0]["text"] == "Готово"
    await worker.close()


@pytest.mark.asyncio
async def test_photos_are_sent_together_with_text(sessions) -> None:
    await _add_job(sessions)
    post = {
        "text": "Текст с фотографиями",
        "attachments": [
            {"type": "photo", "photo": {
                "id": number,
                "sizes": [{
                    "url": f"https://vk.test/{number}.jpg", "width": 100, "height": 100
                }],
            }}
            for number in (1, 2)
        ],
    }
    max_api = FakeMax()
    worker = SyndicationWorker(
        sessions=sessions,
        vk=FakeVk(post),
        max_api=max_api,
        poll_seconds=0.01,
        max_attempts=3,
        download_limit_bytes=1024,
    )

    async def fake_upload(media_type: str, url: str, filename: str) -> str:
        assert media_type == "image"
        return filename

    worker._download_and_upload = fake_upload  # type: ignore[method-assign]
    await worker.process_next()
    assert len(max_api.sent) == 1
    assert max_api.sent[0]["text"] == "Текст с фотографиями"
    assert [item["payload"]["token"] for item in max_api.sent[0]["attachments"]] == [
        "vk-photo-1.jpg",
        "vk-photo-2.jpg",
    ]
    await worker.close()


@pytest.mark.asyncio
async def test_temporary_vk_failure_schedules_retry(sessions) -> None:
    await _add_job(sessions)
    worker = _worker(sessions, FakeVk(VkApiError("temporary", transient=True)), FakeMax())
    await worker.process_next()
    async with sessions() as session:
        job = await session.scalar(select(SyndicationJob))
        assert job.status == "retry"
        assert job.next_attempt_at is not None
    await worker.close()


@pytest.mark.asyncio
async def test_temporary_max_failure_schedules_retry(sessions) -> None:
    await _add_job(sessions)
    worker = _worker(
        sessions,
        FakeVk({"text": "x"}),
        FakeMax(MaxApiError("busy", status_code=503)),
    )
    await worker.process_next()
    async with sessions() as session:
        job = await session.scalar(select(SyndicationJob))
        assert job.status == "retry"
    await worker.close()


@pytest.mark.asyncio
async def test_permanent_failure_is_final(sessions) -> None:
    await _add_job(sessions)
    worker = _worker(
        sessions,
        FakeVk(VkApiError("forbidden", transient=False)),
        FakeMax(),
    )
    await worker.process_next()
    async with sessions() as session:
        job = await session.scalar(select(SyndicationJob))
        assert job.status == "failed"
    await worker.close()


@pytest.mark.asyncio
async def test_permanent_wall_api_error_falls_back_to_long_poll_event(sessions) -> None:
    job_id = await _add_job(sessions, post_id=55)
    async with sessions() as session:
        job = await session.get(SyndicationJob, job_id)
        job.raw_event = {
            "type": "wall_post_new",
            "group_id": 225565387,
            "object": {
                "id": 55,
                "owner_id": -225565387,
                "text": "Текст из Bots Long Poll",
                "attachments": [],
            },
        }
        await session.commit()
    max_api = FakeMax()
    worker = _worker(
        sessions,
        FakeVk(VkApiError("group authorization failed", transient=False)),
        max_api,
    )
    await worker.process_next()
    async with sessions() as session:
        job = await session.get(SyndicationJob, job_id)
        assert job.status == "published"
        assert job.max_message_id == "mid.test"
    assert max_api.sent[0]["text"] == "Текст из Bots Long Poll"
    await worker.close()


@pytest.mark.asyncio
async def test_retry_becomes_failed_at_limit(sessions) -> None:
    await _add_job(sessions)
    worker = _worker(
        sessions,
        FakeVk(VkApiError("temporary", transient=True)),
        FakeMax(),
        attempts=1,
    )
    await worker.process_next()
    async with sessions() as session:
        job = await session.scalar(select(SyndicationJob))
        assert job.status == "failed"
        assert job.attempts == 1
    await worker.close()


@pytest.mark.asyncio
async def test_recover_processing_job_after_restart(sessions) -> None:
    job_id = await _add_job(sessions)
    async with sessions() as session:
        job = await session.get(SyndicationJob, job_id)
        job.status = "processing"
        await session.commit()
    worker = _worker(sessions, FakeVk({"text": "x"}), FakeMax())
    await worker.recover()
    async with sessions() as session:
        job = await session.get(SyndicationJob, job_id)
        assert job.status == "retry"
    await worker.close()
