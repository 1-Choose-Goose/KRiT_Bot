from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def extract_first_token(raw: str) -> str | None:
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first_line:
        return None
    return first_line.split("=", 1)[-1].strip().strip("\"'") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/krit.db"
    max_bot_token: SecretStr | None = None
    max_token_file: Path = Path("../TOKEN.txt")
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_webhook_secret: SecretStr | None = None
    jwt_secret: SecretStr | None = None
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr = SecretStr("admin")
    bot_mode: Literal["polling", "webhook"] = "polling"
    polling_timeout_seconds: int = Field(default=30, ge=1, le=90)
    vk_syndication_enabled: bool = False
    vk_api_base_url: str = "https://api.vk.com/method"
    vk_api_version: str = "5.199"
    vk_access_token: SecretStr | None = None
    vk_callback_secret: SecretStr | None = None
    vk_callback_confirmation: SecretStr | None = None
    vk_community_id: int | None = None
    max_channel_id: int | None = None
    max_required_channel_id: int | None = None
    max_required_channel_link: str | None = None
    vk_long_poll_wait_seconds: int = Field(default=25, ge=1, le=90)
    syndication_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)
    syndication_max_attempts: int = Field(default=5, ge=1, le=20)
    syndication_download_limit_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1024, le=250 * 1024 * 1024
    )
    center_timezone: str = "Asia/Yekaterinburg"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def load_token_file(self) -> Settings:
        if self.max_bot_token is None:
            candidates = [self.max_token_file, Path("TOKEN.txt")]
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                value = extract_first_token(candidate.read_text(encoding="utf-8-sig"))
                if value is None:
                    continue
                self.max_bot_token = SecretStr(value)
                break
        if self.max_bot_token is None:
            raise ValueError("MAX_BOT_TOKEN is not set and TOKEN.txt was not found")
        if self.database_url.startswith(("postgresql", "postgres")):
            password = self.bootstrap_admin_password.get_secret_value()
            if password.lower() in {"admin", "password", "replace_with_strong_password"} or len(
                password
            ) < 12:
                raise ValueError(
                    "BOOTSTRAP_ADMIN_PASSWORD must contain at least 12 characters "
                    "and must not use a default value in production"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
