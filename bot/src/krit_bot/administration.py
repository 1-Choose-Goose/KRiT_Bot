from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import AdminPrincipal, normalize_admin_username
from .db import ADMIN_ROLES, AdminUser, utcnow
from .learning_models import AuditEvent

password_hash = PasswordHash.recommended()


class AdminUserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    role: str
    active: bool
    must_change_password: bool
    is_protected: bool
    created_at: datetime
    updated_at: datetime


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    full_name: str = Field(min_length=3, max_length=250)
    role: str
    password: str = Field(min_length=7, max_length=256)


class AdminUserUpdate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    full_name: str = Field(min_length=3, max_length=250)
    role: str


class AdminPasswordUpdate(BaseModel):
    password: str = Field(min_length=7, max_length=256)


def _validate_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in ADMIN_ROLES:
        raise HTTPException(status_code=422, detail="Unknown administrator role")
    return normalized


def _clean_full_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) < 3:
        raise HTTPException(status_code=422, detail="Invalid administrator name")
    return cleaned


def _clean_username(value: str) -> str:
    cleaned = normalize_admin_username(value)
    if len(cleaned) < 3 or any(character.isspace() for character in cleaned):
        raise HTTPException(status_code=422, detail="Invalid administrator login")
    return cleaned


async def _audit(
    session: AsyncSession,
    principal: AdminPrincipal,
    action: str,
    target_id: int | None,
    details: dict[str, Any],
) -> None:
    session.add(
        AuditEvent(
            actor_admin_id=principal.id,
            action=action,
            entity_type="admin_user",
            entity_id=target_id,
            details=details,
        )
    )


def create_administration_router(
    sessions: async_sessionmaker[AsyncSession],
    require_admin: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/administration")

    async def require_superadmin(
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> AdminPrincipal:
        if principal.must_change_password:
            raise HTTPException(status_code=403, detail="password_change_required")
        if principal.role != "superadmin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return principal

    async def get_target(session: AsyncSession, user_id: int) -> AdminUser:
        target = await session.get(AdminUser, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Administrator not found")
        return target

    async def ensure_can_remove_superadmin(
        session: AsyncSession, target: AdminUser
    ) -> None:
        if target.role != "superadmin" or not target.active:
            return
        active_count = await session.scalar(
            select(func.count(AdminUser.id)).where(
                AdminUser.role == "superadmin", AdminUser.active.is_(True)
            )
        )
        if int(active_count or 0) <= 1:
            raise HTTPException(status_code=409, detail="Нельзя отключить последнего SuperAdmin")

    @router.get("/users", response_model=list[AdminUserView])
    async def list_users(
        _principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> list[AdminUser]:
        async with sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(AdminUser).order_by(AdminUser.full_name, AdminUser.id)
                    )
                ).all()
            )

    @router.post("/users", response_model=AdminUserView, status_code=201)
    async def create_user(
        payload: AdminUserCreate,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> AdminUser:
        username = _clean_username(payload.username)
        async with sessions() as session:
            user = AdminUser(
                username=username,
                full_name=_clean_full_name(payload.full_name),
                password_hash=password_hash.hash(payload.password),
                role=_validate_role(payload.role),
                active=True,
                must_change_password=False,
                auth_version=1,
                is_protected=False,
            )
            session.add(user)
            try:
                await session.flush()
                await _audit(
                    session,
                    principal,
                    "admin_user_created",
                    user.id,
                    {"username": user.username, "role": user.role},
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(status_code=409, detail="Логин уже существует") from exc
            await session.refresh(user)
            return user

    @router.patch("/users/{user_id}", response_model=AdminUserView)
    async def update_user(
        user_id: int,
        payload: AdminUserUpdate,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> AdminUser:
        async with sessions() as session:
            target = await get_target(session, user_id)
            role = _validate_role(payload.role)
            if target.is_protected and role != "superadmin":
                raise HTTPException(status_code=409, detail="Защищённую запись нельзя понизить")
            if target.role == "superadmin" and role != "superadmin":
                await ensure_can_remove_superadmin(session, target)
            target.username = _clean_username(payload.username)
            target.full_name = _clean_full_name(payload.full_name)
            target.role = role
            target.updated_at = utcnow()
            try:
                await _audit(
                    session,
                    principal,
                    "admin_user_updated",
                    target.id,
                    {"username": target.username, "role": target.role},
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(status_code=409, detail="Логин уже существует") from exc
            await session.refresh(target)
            return target

    @router.post("/users/{user_id}/password", response_model=AdminUserView)
    async def change_password(
        user_id: int,
        payload: AdminPasswordUpdate,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> AdminUser:
        async with sessions() as session:
            target = await get_target(session, user_id)
            target.password_hash = password_hash.hash(payload.password)
            target.must_change_password = False
            target.auth_version += 1
            target.updated_at = utcnow()
            await _audit(
                session,
                principal,
                "admin_user_password_changed",
                target.id,
                {"username": target.username},
            )
            await session.commit()
            await session.refresh(target)
            return target

    @router.post("/users/{user_id}/disable", response_model=AdminUserView)
    async def disable_user(
        user_id: int,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> AdminUser:
        async with sessions() as session:
            target = await get_target(session, user_id)
            if target.id == principal.id:
                raise HTTPException(status_code=409, detail="Нельзя отключить свою запись")
            if target.is_protected:
                raise HTTPException(status_code=409, detail="Защищённую запись нельзя отключить")
            await ensure_can_remove_superadmin(session, target)
            target.active = False
            target.auth_version += 1
            target.updated_at = utcnow()
            await _audit(
                session,
                principal,
                "admin_user_disabled",
                target.id,
                {"username": target.username},
            )
            await session.commit()
            await session.refresh(target)
            return target

    @router.post("/users/{user_id}/enable", response_model=AdminUserView)
    async def enable_user(
        user_id: int,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> AdminUser:
        async with sessions() as session:
            target = await get_target(session, user_id)
            target.active = True
            target.updated_at = utcnow()
            await _audit(
                session,
                principal,
                "admin_user_enabled",
                target.id,
                {"username": target.username},
            )
            await session.commit()
            await session.refresh(target)
            return target

    @router.delete("/users/{user_id}")
    async def delete_user(
        user_id: int,
        principal: Annotated[AdminPrincipal, Depends(require_superadmin)],
    ) -> dict[str, bool]:
        async with sessions() as session:
            target = await get_target(session, user_id)
            if target.id == principal.id:
                raise HTTPException(status_code=409, detail="Нельзя удалить свою запись")
            if target.is_protected:
                raise HTTPException(status_code=409, detail="Защищённую запись нельзя удалить")
            await ensure_can_remove_superadmin(session, target)
            target_id = target.id
            username = target.username
            await _audit(
                session,
                principal,
                "admin_user_deleted",
                target_id,
                {"username": username, "role": target.role},
            )
            await session.delete(target)
            await session.commit()
            return {"deleted": True}

    return router
