from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from krit_bot.migrate import run


def _config() -> Config:
    bot_root = Path(__file__).resolve().parents[1]
    config = Config(str(bot_root / "alembic.ini"))
    config.set_main_option("script_location", str(bot_root / "alembic"))
    return config


def test_alembic_builds_empty_database(tmp_path, monkeypatch) -> None:
    database = tmp_path / "empty.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(_config(), "head")

    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "learning_lesson_teacher_segments" in tables
        assert "learning_subject_teachers" in tables
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260929_learning_operations_v4",
        )
    finally:
        connection.close()


def test_alembic_adopts_known_legacy_database(tmp_path, monkeypatch) -> None:
    database = tmp_path / "production-like.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(_config(), "20260928_learning_process_v1")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO persons "
        "(id, full_name, phone, max_user_id, active, created_at, updated_at) "
        "VALUES (7, 'Существующий Клиент', '+79000000007', 700000007, 1, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO learning_admin_notifications "
        "(kind, title, message, lesson_id, created_at) VALUES "
        "('lesson_starts_soon', 'Занятие через 10 минут', 'Старое сообщение', 99, "
        "CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO learning_admin_notifications "
        "(kind, title, message, lesson_id, created_at) VALUES "
        "('lesson_starts_soon', 'Занятие через 10 минут', 'Дубль', 99, "
        "CURRENT_TIMESTAMP)"
    )
    connection.execute("DELETE FROM alembic_version")
    connection.execute("DROP TABLE alembic_version")
    connection.commit()
    connection.close()

    run()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT full_name, max_auth_phone FROM persons WHERE id = 7"
        ).fetchone() == ("Существующий Клиент", "+79000000007")
        assert connection.execute(
            "SELECT person_id, max_user_id FROM person_max_identities WHERE person_id = 7"
        ).fetchone() == (7, 700000007)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260929_learning_operations_v4",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_admin_notifications "
            "WHERE lesson_id = 99 AND kind = 'lesson_starts_soon'"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO learning_admin_notifications "
                "(dedupe_key, kind, title, message, lesson_id, created_at) VALUES "
                "('different-key', 'lesson_starts_soon', 'Дубль', 'Дубль', 99, "
                "CURRENT_TIMESTAMP)"
            )
    finally:
        connection.close()
