from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def test_alembic_adopts_existing_database_and_preserves_people(tmp_path, monkeypatch) -> None:
    database = tmp_path / "production-like.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE persons (
            id INTEGER PRIMARY KEY,
            full_name VARCHAR(250) NOT NULL,
            phone VARCHAR(32) NOT NULL,
            max_user_id BIGINT,
            active BOOLEAN NOT NULL DEFAULT 1,
            archived_at DATETIME,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE admin_users (id INTEGER PRIMARY KEY);
        CREATE TABLE learning_subjects (id INTEGER PRIMARY KEY, name VARCHAR(160));
        CREATE TABLE learning_rooms (id INTEGER PRIMARY KEY, name VARCHAR(120), capacity INTEGER);
        CREATE TABLE learning_groups (
            id INTEGER PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            subject_id INTEGER,
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE learning_lessons (
            id INTEGER PRIMARY KEY,
            subject_id INTEGER,
            teacher_id INTEGER,
            room_id INTEGER,
            start_at DATETIME,
            end_at DATETIME,
            status VARCHAR(20)
        );
        CREATE TABLE learning_lesson_participants (
            id INTEGER PRIMARY KEY,
            lesson_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            person_name_snapshot VARCHAR(250) NOT NULL,
            attendance_status VARCHAR(20) NOT NULL DEFAULT 'expected',
            arrived_at DATETIME,
            left_at DATETIME,
            late_minutes INTEGER,
            note VARCHAR(500)
        );
        INSERT INTO persons (id, full_name, phone, max_user_id, active)
        VALUES (7, 'Существующий Клиент', '+79000000007', 700000007, 1);
        INSERT INTO learning_groups (id, name, active) VALUES (3, 'Существующая группа', 1);
        """
    )
    connection.commit()
    connection.close()

    bot_root = Path(__file__).resolve().parents[1]
    config = Config(str(bot_root / "alembic.ini"))
    config.set_main_option("script_location", str(bot_root / "alembic"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(config, "head")

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT full_name FROM persons WHERE id = 7"
        ).fetchone() == ("Существующий Клиент",)
        assert connection.execute(
            "SELECT default_duration_minutes FROM learning_groups WHERE id = 3"
        ).fetchone() == (60,)
        assert connection.execute(
            "SELECT person_id, max_user_id FROM person_max_identities WHERE person_id = 7"
        ).fetchone() == (7, 700000007)
        participant_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('learning_lesson_participants')"
            )
        }
        assert {
            "cancelled_at",
            "cancelled_by",
            "cancelled_by_person_id",
            "cancelled_by_admin_id",
            "cancellation_reason",
        } <= participant_columns
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260929_learning_process_v2",
        )
    finally:
        connection.close()
