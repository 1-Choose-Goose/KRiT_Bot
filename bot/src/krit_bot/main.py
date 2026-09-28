import uvicorn

from .config import get_settings
from .logging import configure_logging


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "krit_bot.asgi:app",
        host="127.0.0.1",
        port=8080,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
