from __future__ import annotations

import argparse
import asyncio

from .config import get_settings
from .max_api import MaxApiClient


async def subscribe(url: str) -> None:
    settings = get_settings()
    if settings.max_webhook_secret is None:
        raise SystemExit("MAX_WEBHOOK_SECRET is required")
    api = MaxApiClient(
        token=settings.max_bot_token.get_secret_value(),  # type: ignore[union-attr]
        base_url=settings.max_api_base_url,
    )
    try:
        result = await api.subscribe_webhook(
            url=url,
            secret=settings.max_webhook_secret.get_secret_value(),
            update_types=["message_created", "bot_added", "bot_started", "bot_removed"],
        )
    finally:
        await api.close()
    if not result.get("success"):
        raise SystemExit(f"MAX rejected subscription: {result.get('message', 'unknown error')}")
    print("Webhook subscription created")


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the KRiT webhook in MAX")
    parser.add_argument("url", help="Public HTTPS URL, e.g. https://bot.example.ru/webhooks/max")
    args = parser.parse_args()
    asyncio.run(subscribe(args.url))


if __name__ == "__main__":
    main()
