from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from .db import AdminUser


def normalize_admin_username(value: str) -> str:
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    id: int
    username: str
    full_name: str
    role: str
    must_change_password: bool
    auth_version: int
    is_protected: bool

    @classmethod
    def from_model(cls, admin: AdminUser) -> AdminPrincipal:
        return cls(
            id=admin.id,
            username=admin.username,
            full_name=admin.full_name,
            role=admin.role,
            must_change_password=admin.must_change_password,
            auth_version=admin.auth_version,
            is_protected=admin.is_protected,
        )


def issue_access_token(admin: AdminUser, secret: str) -> tuple[str, int]:
    lifetime = timedelta(hours=8)
    now = datetime.now(UTC)
    encoded = jwt.encode(
        {
            "sub": str(admin.id),
            "ver": admin.auth_version,
            "iss": "krit-bot",
            "iat": now,
            "exp": now + lifetime,
        },
        secret,
        algorithm="HS256",
    )
    return encoded, int(lifetime.total_seconds())


def decode_access_token(token: str, secret: str) -> tuple[int, int]:
    payload = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer="krit-bot",
        options={"require": ["exp", "sub", "iss", "ver"]},
    )
    return int(payload["sub"]), int(payload["ver"])
