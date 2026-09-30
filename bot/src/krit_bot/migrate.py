from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command

from .db import build_engine

BASELINE_REVISION = "20260928_learning_process_v1"
LEGACY_TABLES = {
    "persons",
    "admin_users",
    "person_roles",
    "person_max_identities",
    "learning_subjects",
    "learning_rooms",
    "learning_groups",
    "learning_lessons",
    "learning_lesson_participants",
    "learning_notification_jobs",
    "learning_admin_notifications",
}


async def _legacy_schema_without_alembic(database_url: str) -> bool:
    engine = build_engine(database_url)
    try:
        async with engine.connect() as connection:

            def inspect_schema(sync_connection) -> bool:
                schema = inspect(sync_connection)
                tables = set(schema.get_table_names())
                if "alembic_version" in tables or "persons" not in tables:
                    return False
                missing = LEGACY_TABLES - tables
                if missing:
                    raise RuntimeError(
                        "Неизвестная legacy-схема: отсутствуют таблицы "
                        + ", ".join(sorted(missing))
                    )
                person_columns = {item["name"] for item in schema.get_columns("persons")}
                if not {"id", "full_name", "phone", "max_user_id"} <= person_columns:
                    raise RuntimeError("Неизвестная legacy-схема таблицы persons")
                return True

            return await connection.run_sync(inspect_schema)
    finally:
        await engine.dispose()


def run() -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/krit.db")
    import asyncio

    if asyncio.run(_legacy_schema_without_alembic(database_url)):
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


if __name__ == "__main__":
    run()
