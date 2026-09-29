from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command


def run() -> None:
    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


if __name__ == "__main__":
    run()
