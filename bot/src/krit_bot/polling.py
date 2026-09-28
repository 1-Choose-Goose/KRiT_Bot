from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import get_marker, save_marker
from .handler import EchoHandler
from .max_api import MaxApiClient, MaxApiError

log = structlog.get_logger()


async def run_polling(
    *,
    api: MaxApiClient,
    handler: EchoHandler,
    sessions: async_sessionmaker[AsyncSession],
    poll_timeout: int,
) -> None:
    delay = 1
    async with sessions() as session:
        marker = await get_marker(session)

    while True:
        try:
            payload = await api.get_updates(marker=marker, poll_timeout=poll_timeout)
            for update in payload.get("updates", []):
                if isinstance(update, dict):
                    await handler.handle(update)
            marker = payload.get("marker", marker)
            async with sessions() as session:
                await save_marker(session, marker)
                await session.commit()
            delay = 1
        except asyncio.CancelledError:
            raise
        except (MaxApiError, OSError) as exc:
            log.warning("polling_failed", error=str(exc), retry_seconds=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
