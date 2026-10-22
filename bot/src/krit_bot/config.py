from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/krit.db"
    max_bot_token: SecretStr
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_webhook_secret: SecretStr | None = None
    max_webhook_url: str | None = None
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
    krit_database_names: Annotated[tuple[str, ...], NoDecode] = ("krit_bot",)
    restore_root: Path = Path("./data/restore")
    online_public_url: str = "http://localhost:8000/online.html"
    online_cookie_name: str = "krit_online_session"
    online_cookie_secure: bool = True
    online_session_hours: int = Field(default=12, ge=1, le=168)
    online_otp_pepper: SecretStr | None = None
    online_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    online_upload_root: Path = Path("./data/online-assets")
    online_upload_limit_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1024, le=250 * 1024 * 1024
    )
    online_room_storage_limit_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1024 * 1024, le=2 * 1024 * 1024 * 1024
    )
    online_total_storage_limit_bytes: int = Field(
        default=300 * 1024 * 1024, ge=1024 * 1024, le=10 * 1024 * 1024 * 1024
    )
    online_min_free_disk_bytes: int = Field(
        default=1024 * 1024 * 1024, ge=100 * 1024 * 1024
    )
    online_snapshot_every_operations: int = Field(default=200, ge=50, le=2000)
    online_operation_retention: int = Field(default=5000, ge=1000, le=100_000)
    online_cleanup_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    online_abandoned_room_hours: int = Field(default=24, ge=1, le=720)

    @field_validator("krit_database_names", mode="before")
    @classmethod
    def parse_database_names(cls, value: object) -> object:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                value = json.loads(value)
                return tuple(str(item).strip() for item in value if str(item).strip())
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("online_allowed_origins", mode="before")
    @classmethod
    def parse_online_allowed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())
        return value

    def initial_admin_credentials(self) -> tuple[str, str, bool]:
        if self.database_url.startswith(("postgresql", "postgres")):
            return ("Choose_Goose", "123", True)
        return (
            self.bootstrap_admin_username.strip().lower(),
            self.bootstrap_admin_password.get_secret_value(),
            False,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
