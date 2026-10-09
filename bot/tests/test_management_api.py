from __future__ import annotations

import asyncio

import httpx
import pytest
from pwdlib import PasswordHash
from pydantic import SecretStr
from sqlalchemy import select

from krit_bot.config import Settings
from krit_bot.db import AdminUser, build_engine, build_session_factory
from krit_bot.learning_models import AuditEvent, PersonMaxIdentity
from krit_bot.webhook import LoginRateLimiter, create_app

password_hash = PasswordHash.recommended()


def _settings(database_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_login_limiter_is_scoped_resettable_and_bounded() -> None:
    limiter = LoginRateLimiter(
        window_seconds=60,
        per_identity_limit=3,
        per_address_limit=20,
        max_buckets=8,
    )
    for second in range(3):
        limiter.record_failure("admin", "10.0.0.1", now=float(second))

    assert limiter.retry_after("admin", "10.0.0.1", now=3.0) > 0
    assert limiter.retry_after("admin", "10.0.0.2", now=3.0) is None
    limiter.record_success("admin", "10.0.0.1")
    assert limiter.retry_after("admin", "10.0.0.1", now=3.0) is None

    shared_address = LoginRateLimiter(
        window_seconds=60,
        per_identity_limit=10,
        per_address_limit=3,
        max_buckets=20,
    )
    for index in range(3):
        shared_address.record_failure(f"wrong-{index}", "10.0.0.5", now=float(index))
    assert shared_address.retry_after("admin", "10.0.0.5", now=3.0) is not None
    shared_address.record_success("admin", "10.0.0.5")
    assert shared_address.retry_after("admin", "10.0.0.5", now=3.0) is None

    for index in range(30):
        limiter.record_failure(f"user-{index}", f"10.0.1.{index}", now=10.0)
    assert limiter.bucket_count <= 8


@pytest.mark.asyncio
async def test_login_returns_generic_401_then_clear_429_without_global_lockout(
    tmp_path,
) -> None:
    app = create_app(_settings((tmp_path / "rate-limit.db").as_posix()))
    async with app.router.lifespan_context(app):
        first_transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=first_transport, base_url="http://test"
        ) as client:
            unknown = await client.post(
                "/api/v1/auth/login",
                json={"username": "unknown", "password": "wrong"},
            )
            wrong = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
            assert unknown.status_code == wrong.status_code == 401
            assert unknown.json() == wrong.json()
            for _ in range(4):
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong"},
                )
            limited = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin"},
            )
            assert limited.status_code == 429
            assert int(limited.headers["Retry-After"]) > 0
            assert "попыт" in limited.json()["detail"].lower()

        second_transport = httpx.ASGITransport(app=app, client=("10.0.0.2", 1234))
        async with httpx.AsyncClient(
            transport=second_transport, base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin"},
                )
            ).status_code == 200


def test_initial_admin_credentials_are_environment_specific() -> None:
    sqlite = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        max_bot_token=SecretStr("test-token"),
    )
    assert sqlite.initial_admin_credentials() == ("admin", "admin", False)

    postgres = Settings(
        database_url="postgresql+asyncpg://krit@localhost/krit_bot",
        max_bot_token=SecretStr("test-token"),
    )
    assert postgres.initial_admin_credentials() == ("Choose_Goose", "123", True)


@pytest.mark.asyncio
async def test_sqlite_bootstrap_has_superadmin_profile_and_is_not_reset(tmp_path) -> None:
    database_path = (tmp_path / "bootstrap.db").as_posix()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            admin = await session.get(AdminUser, 1)
            assert admin is not None
            assert admin.username == "admin"
            assert admin.full_name == "Администратор"
            assert admin.role == "superadmin"
            assert admin.must_change_password is False
            assert admin.auth_version == 1
            assert admin.is_protected is False
            admin.full_name = "Не сбрасывать"
            await session.commit()
        await engine.dispose()

    second_app = create_app(settings)
    async with second_app.router.lifespan_context(second_app):
        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            admin = await session.get(AdminUser, 1)
            assert admin is not None
            assert admin.full_name == "Не сбрасывать"
        await engine.dispose()


@pytest.mark.asyncio
async def test_protected_bootstrap_is_added_to_existing_server_database(
    tmp_path, monkeypatch
) -> None:
    settings = _settings((tmp_path / "existing-server.db").as_posix())
    first_app = create_app(settings)
    async with first_app.router.lifespan_context(first_app):
        pass

    monkeypatch.setattr(
        Settings,
        "initial_admin_credentials",
        lambda _settings: ("Choose_Goose", "123", True),
    )
    upgraded_app = create_app(settings)
    async with upgraded_app.router.lifespan_context(upgraded_app):
        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            admins = list((await session.scalars(select(AdminUser).order_by(AdminUser.id))).all())
            assert [admin.username for admin in admins] == ["admin", "choose_goose"]
            protected = admins[1]
            assert protected.role == "superadmin"
            assert protected.active is True
            assert protected.must_change_password is True
            assert protected.is_protected is True
            assert password_hash.verify("123", protected.password_hash)
        await engine.dispose()


@pytest.mark.asyncio
async def test_forced_password_change_blocks_business_api_and_revokes_old_token(tmp_path) -> None:
    settings = _settings((tmp_path / "forced-change.db").as_posix())
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            admin = await session.get(AdminUser, 1)
            assert admin is not None
            admin.password_hash = password_hash.hash("123")
            admin.must_change_password = True
            admin.is_protected = True
            await session.commit()
        await engine.dispose()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await _login(client, " ADMIN ", "123")
            assert login["id"] == 1
            assert login["role"] == "superadmin"
            assert login["full_name"] == "Администратор"
            assert login["must_change_password"] is True
            old_headers = {"Authorization": f"Bearer {login['access_token']}"}
            blocked = await client.get("/api/v1/status", headers=old_headers)
            assert blocked.status_code == 403
            assert blocked.json()["detail"] == "password_change_required"

            changed = await client.post(
                "/api/v1/auth/change-initial-password",
                headers=old_headers,
                json={"current_password": "123", "new_password": "new-password-456"},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["must_change_password"] is False
            assert (await client.get("/api/v1/status", headers=old_headers)).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "123"},
                )
            ).status_code == 401
            current = await _login(client, "AdMiN", "new-password-456")
            assert current["must_change_password"] is False
            current_headers = {"Authorization": f"Bearer {current['access_token']}"}
            assert (await client.get("/api/v1/status", headers=current_headers)).status_code == 200
            renamed = await client.patch(
                "/api/v1/administration/users/1",
                headers=current_headers,
                json={
                    "full_name": "Администратор",
                    "username": "renamed-bootstrap",
                    "role": "superadmin",
                },
            )
            assert renamed.status_code == 409


@pytest.mark.asyncio
async def test_superadmin_manages_users_roles_protection_and_audit(tmp_path) -> None:
    settings = _settings((tmp_path / "administration.db").as_posix())
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await _login(client, "admin", "admin")
            headers = {"Authorization": f"Bearer {login['access_token']}"}

            director = await client.post(
                "/api/v1/administration/users",
                headers=headers,
                json={
                    "full_name": "Директор Клуба",
                    "username": " Director ",
                    "role": "director",
                    "password": "1234567",
                },
            )
            assert director.status_code == 201, director.text
            director_data = director.json()
            assert director_data["username"] == "director"
            assert director_data["active"] is True
            assert "password" not in director_data

            administrator = await client.post(
                "/api/v1/administration/users",
                headers=headers,
                json={
                    "full_name": "Администратор Клуба",
                    "username": "operator",
                    "role": "administrator",
                    "password": "operator-password",
                },
            )
            assert administrator.status_code == 201, administrator.text
            operator_id = administrator.json()["id"]

            duplicate = await client.post(
                "/api/v1/administration/users",
                headers=headers,
                json={
                    "full_name": "Дубликат",
                    "username": "DIRECTOR",
                    "role": "administrator",
                    "password": "another-password",
                },
            )
            assert duplicate.status_code == 409

            users = (await client.get("/api/v1/administration/users", headers=headers)).json()
            assert [item["username"] for item in users] == ["admin", "operator", "director"]

            invalid_login = await client.post(
                "/api/v1/administration/users",
                headers=headers,
                json={
                    "full_name": "Некорректный Логин",
                    "username": "bad login",
                    "role": "administrator",
                    "password": "1234567",
                },
            )
            assert invalid_login.status_code == 422

            updated = await client.patch(
                f"/api/v1/administration/users/{operator_id}",
                headers=headers,
                json={
                    "full_name": "Новый Директор",
                    "username": " NewOperator ",
                    "role": "director",
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["username"] == "newoperator"
            assert updated.json()["role"] == "director"

            changed_password = await client.post(
                f"/api/v1/administration/users/{operator_id}/password",
                headers=headers,
                json={"password": "replacement-password"},
            )
            assert changed_password.status_code == 200

            disabled = await client.post(
                f"/api/v1/administration/users/{operator_id}/disable", headers=headers
            )
            assert disabled.status_code == 200
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "newoperator", "password": "replacement-password"},
                )
            ).status_code == 401
            assert (
                await client.post(
                    f"/api/v1/administration/users/{operator_id}/enable", headers=headers
                )
            ).status_code == 200

            director_login = await _login(client, "director", "1234567")
            director_headers = {
                "Authorization": f"Bearer {director_login['access_token']}"
            }
            assert (
                await client.get("/api/v1/administration/users", headers=director_headers)
            ).status_code == 403

            assert (
                await client.post("/api/v1/administration/users/1/disable", headers=headers)
            ).status_code == 409
            assert (
                await client.delete("/api/v1/administration/users/1", headers=headers)
            ).status_code == 409
            assert (
                await client.patch(
                    "/api/v1/administration/users/1",
                    headers=headers,
                    json={
                        "full_name": "Администратор",
                        "username": "admin",
                        "role": "director",
                    },
                )
            ).status_code == 409

            assert (
                await client.delete(
                    f"/api/v1/administration/users/{operator_id}", headers=headers
                )
            ).status_code == 200

        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            events = list(
                (
                    await session.scalars(
                        select(AuditEvent).where(AuditEvent.entity_type == "admin_user")
                    )
                ).all()
            )
            assert {event.action for event in events} >= {
                "admin_user_created",
                "admin_user_updated",
                "admin_user_password_changed",
                "admin_user_disabled",
                "admin_user_enabled",
                "admin_user_deleted",
            }
            assert "replacement-password" not in repr([event.details for event in events])
        await engine.dispose()


@pytest.mark.asyncio
async def test_service_restarts_are_authorized_serialized_and_audited(tmp_path) -> None:
    settings = _settings((tmp_path / "service-restarts.db").as_posix())
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_audit_actions: list[str] = []
    server_failures = 0

    async def restart_bot() -> None:
        engine = build_engine(settings.database_url)
        sessions = build_session_factory(engine)
        async with sessions() as session:
            callback_audit_actions.extend(
                list(
                    await session.scalars(
                        select(AuditEvent.action).where(
                            AuditEvent.action == "bot_restart_requested"
                        )
                    )
                )
            )
        await engine.dispose()
        callback_started.set()
        await release_callback.wait()

    async def restart_server() -> None:
        nonlocal server_failures
        server_failures += 1
        raise RuntimeError("secret internal restart failure")

    app = create_app(
        settings,
        restart_bot=restart_bot,
        restart_server=restart_server,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await _login(client, "admin", "admin")
            headers = {"Authorization": f"Bearer {login['access_token']}"}
            created = await client.post(
                "/api/v1/administration/users",
                headers=headers,
                json={
                    "full_name": "Директор Проверки",
                    "username": "director",
                    "role": "director",
                    "password": "1234567",
                },
            )
            assert created.status_code == 201
            director_login = await _login(client, "director", "1234567")
            director_headers = {
                "Authorization": f"Bearer {director_login['access_token']}"
            }
            assert (
                await client.post(
                    "/api/v1/administration/services/bot/restart",
                    headers=director_headers,
                )
            ).status_code == 403

            first = asyncio.create_task(
                client.post(
                    "/api/v1/administration/services/bot/restart",
                    headers=headers,
                )
            )
            await callback_started.wait()
            assert callback_audit_actions == ["bot_restart_requested"]
            duplicate = await client.post(
                "/api/v1/administration/services/bot/restart",
                headers=headers,
            )
            assert duplicate.status_code == 409
            release_callback.set()
            assert (await first).status_code == 200
            assert (await client.get("/health")).status_code == 200

            failed = await client.post(
                "/api/v1/administration/services/server/restart",
                headers=headers,
            )
            assert failed.status_code == 503
            assert "secret" not in failed.text
            retry = await client.post(
                "/api/v1/administration/services/server/restart",
                headers=headers,
            )
            assert retry.status_code == 503
            assert server_failures == 2

@pytest.mark.asyncio
async def test_login_protects_management_api_and_allows_person_creation(tmp_path) -> None:
    database_path = (tmp_path / "api.db").as_posix()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        max_bot_token=SecretStr("test-token"),
        jwt_secret=SecretStr("test-jwt-secret-with-enough-entropy"),
        bot_mode="webhook",
        vk_syndication_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/status")).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong"},
                )
            ).status_code == 401

            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin"},
            )
            assert login.status_code == 200
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            assert (await client.get("/api/v1/status", headers=headers)).status_code == 200

            created = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Иванов Иван Иванович",
                    "phone": "8 (900) 123-45-67",
                    "roles": ["student"],
                    "active": True,
                },
            )
            assert created.status_code == 201
            assert created.json()["phone"] == "+79001234567"
            assert created.json()["max_user_id"] is None
            assert len((await client.get("/api/v1/people", headers=headers)).json()) == 1
            snapshot = (await client.get("/api/v1/snapshot", headers=headers)).json()
            assert snapshot["status"] == "ok"
            assert len(snapshot["people"]) == 1

            person_id = created.json()["id"]
            engine = build_engine(settings.database_url)
            sessions = build_session_factory(engine)
            async with sessions() as session:
                session.add(
                    PersonMaxIdentity(
                        person_id=person_id,
                        max_user_id=20_067_728,
                        verified_phone="+79001234567",
                    )
                )
                await session.commit()
            await engine.dispose()
            parent = await client.post(
                "/api/v1/people",
                headers=headers,
                json={
                    "full_name": "Иванова Анна Петровна",
                    "phone": "+79007654321",
                    "roles": ["parent"],
                    "active": True,
                },
            )
            assert parent.status_code == 201
            parent_id = parent.json()["id"]
            linked = await client.post(
                f"/api/v1/people/{person_id}/guardians/{parent_id}", headers=headers
            )
            assert linked.status_code == 200
            assert linked.json()["guardians"][0]["id"] == parent_id

            saved_with_unchanged_relation = await client.put(
                f"/api/v1/people/{person_id}/aggregate",
                headers=headers,
                json={
                    "person": {
                        "full_name": "Иванов Иван Иванович",
                        "phone": "+79001234567",
                        "max_auth_phone": "+79001234567",
                        "roles": ["student", "parent", "teacher"],
                        "active": True,
                    },
                    "parent_ids": [parent_id],
                    "student_ids": [],
                },
            )
            assert saved_with_unchanged_relation.status_code == 200, (
                saved_with_unchanged_relation.text
            )
            assert set(saved_with_unchanged_relation.json()["roles"]) == {
                "student",
                "parent",
                "teacher",
            }
            assert saved_with_unchanged_relation.json()["max_user_id"] == 20_067_728
            people = (await client.get("/api/v1/people", headers=headers)).json()
            parent_view = next(item for item in people if item["id"] == parent_id)
            assert parent_view["students"][0]["id"] == person_id

            all_roles = await client.put(
                f"/api/v1/people/{person_id}",
                headers=headers,
                json={
                    "full_name": "Куц Олег Олегович",
                    "phone": "+79001234567",
                    "roles": ["student", "parent", "teacher"],
                    "active": True,
                },
            )
            assert all_roles.status_code == 200, all_roles.text
            assert set(all_roles.json()["roles"]) == {"student", "parent", "teacher"}

            student_only = await client.put(
                f"/api/v1/people/{person_id}",
                headers=headers,
                json={
                    "full_name": "Куц Олег Олегович",
                    "phone": "+79001234567",
                    "roles": ["student"],
                    "active": True,
                },
            )
            assert student_only.status_code == 200, student_only.text
            assert student_only.json()["roles"] == ["student"]

            archived = await client.post(f"/api/v1/people/{person_id}/archive", headers=headers)
            assert archived.status_code == 200
            assert archived.json()["archived_at"] is not None
            assert all(
                item["id"] != person_id
                for item in (await client.get("/api/v1/people", headers=headers)).json()
            )
            assert len((await client.get("/api/v1/people-archive", headers=headers)).json()) == 1

            restored = await client.post(f"/api/v1/people/{person_id}/restore", headers=headers)
            assert restored.status_code == 200
            assert restored.json()["archived_at"] is None
            assert restored.json()["guardians"][0]["id"] == parent_id

            unlinked = await client.delete(
                f"/api/v1/people/{person_id}/guardians/{parent_id}", headers=headers
            )
            assert unlinked.status_code == 200
            assert len((await client.get("/api/v1/people", headers=headers)).json()) == 2

            await client.post(f"/api/v1/people/{person_id}/archive", headers=headers)
            deleted = await client.delete(f"/api/v1/people/{person_id}", headers=headers)
            assert deleted.status_code == 200
            assert (await client.get("/api/v1/people-archive", headers=headers)).json() == []
