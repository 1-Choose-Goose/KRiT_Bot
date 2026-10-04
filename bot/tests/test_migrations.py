from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from krit_bot import learning_models as _learning_models  # noqa: F401
from krit_bot.db import Base
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
            "20261004_administration_v7",
        )
        admin_columns = {
            item[1]: item for item in connection.execute("PRAGMA table_info(admin_users)")
        }
        assert {
            "full_name",
            "role",
            "must_change_password",
            "auth_version",
            "is_protected",
            "updated_at",
        } <= admin_columns.keys()
        assert admin_columns["role"][3] == 1
        assert admin_columns["auth_version"][3] == 1
        inspector = sa.inspect(sa.create_engine(f"sqlite:///{database.as_posix()}"))
        for table_name in Base.metadata.tables:
            metadata_table = Base.metadata.tables[table_name]
            expected_checks = {
                constraint.name
                for constraint in metadata_table.constraints
                if isinstance(constraint, sa.CheckConstraint) and constraint.name
            }
            actual_checks = {
                item["name"]
                for item in inspector.get_check_constraints(table_name)
                if item.get("name")
            }
            assert expected_checks <= actual_checks, table_name
            expected_indexes = {index.name for index in metadata_table.indexes if index.name}
            actual_indexes = {
                item["name"]
                for item in inspector.get_indexes(table_name)
                if item.get("name")
            }
            assert expected_indexes <= actual_indexes, table_name
            expected_uniques = {
                tuple(column.name for column in constraint.columns)
                for constraint in metadata_table.constraints
                if isinstance(constraint, sa.UniqueConstraint)
            }
            actual_uniques = {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table_name)
            }
            assert expected_uniques <= actual_uniques, table_name
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
            "20261004_administration_v7",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_admin_notifications "
            "WHERE lesson_id = 99 AND kind = 'lesson_starts_soon'"
        ).fetchone() == (1,)
        connection.execute(
            "INSERT INTO learning_admin_notifications "
            "(dedupe_key, kind, title, message, lesson_id, created_at) VALUES "
            "('different-key', 'lesson_starts_soon', 'Дубль', 'Дубль', 99, "
            "CURRENT_TIMESTAMP)"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_admin_notifications "
            "WHERE lesson_id = 99 AND kind = 'lesson_starts_soon'"
        ).fetchone() == (2,)
    finally:
        connection.close()


def test_administration_migration_preserves_existing_admin_as_superadmin(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "existing-admin.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(_config(), "20260930_communications_v6")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO admin_users (id, username, password_hash, active, created_at) "
        "VALUES (7, 'ExistingAdmin', 'hash', 1, CURRENT_TIMESTAMP)"
    )
    connection.commit()
    connection.close()

    command.upgrade(_config(), "head")

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT username, full_name, role, must_change_password, auth_version, "
            "is_protected FROM admin_users WHERE id = 7"
        ).fetchone() == ("existingadmin", "ExistingAdmin", "superadmin", 0, 1, 0)
    finally:
        connection.close()


def test_upgrade_from_v4_preserves_learning_history(tmp_path, monkeypatch) -> None:
    database = tmp_path / "v4-history.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(_config(), "20260929_learning_operations_v4")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO learning_lesson_series "
        "(id, subject_id, teacher_id, room_id, starts_at, duration_minutes, "
        "interval_weeks, occurrences, active, created_at) VALUES "
        "(41, 1, 1, 1, '2026-09-30 10:00:00', 60, 1, 1, 1, CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO learning_lessons "
        "(id, series_id, subject_id, teacher_id, room_id, start_at, end_at, status, "
        "teacher_name_snapshot, room_name_snapshot, subject_name_snapshot, created_at, updated_at) "
        "VALUES (42, 41, 1, 1, 1, '2026-09-30 10:00:00', '2026-09-30 11:00:00', "
        "'completed', 'Учитель', 'Кабинет', 'Предмет', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO learning_club_presence_sessions "
        "(id, person_id, source, arrived_at) "
        "VALUES (43, 1, 'management', '2026-09-30 09:00:00')"
    )
    connection.execute(
        "INSERT INTO learning_admin_notifications "
        "(id, dedupe_key, kind, title, message, lesson_id, created_at) VALUES "
        "(44, 'history:44', 'history', 'История', 'Сохранить', 42, CURRENT_TIMESTAMP)"
    )
    connection.commit()
    connection.close()

    command.upgrade(_config(), "head")

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT status, series_occurrence_index, series_exception "
            "FROM learning_lessons WHERE id = 42"
        ).fetchone() == ("completed", 0, 0)
        assert connection.execute(
            "SELECT arrived_at, left_at FROM learning_club_presence_sessions WHERE id = 43"
        ).fetchone() == ("2026-09-30 09:00:00", None)
        assert connection.execute(
            "SELECT message FROM learning_admin_notifications WHERE id = 44"
        ).fetchone() == ("Сохранить",)
    finally:
        connection.close()
