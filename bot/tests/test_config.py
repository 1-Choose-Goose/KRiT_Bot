from __future__ import annotations

from krit_bot.config import Settings


def test_database_names_accept_comma_separated_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("KRIT_DATABASE_NAMES", "krit_bot, krit_messages")

    settings = Settings(_env_file=None)

    assert settings.krit_database_names == ("krit_bot", "krit_messages")


def test_database_names_accept_json_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("KRIT_DATABASE_NAMES", '["krit_bot"]')

    settings = Settings(_env_file=None)

    assert settings.krit_database_names == ("krit_bot",)
