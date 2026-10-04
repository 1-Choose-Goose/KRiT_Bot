# Administration, Backup, and Server Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add role-based administration, safe KRiT/bot restarts, automatic local PostgreSQL backups and new-server restoration, plus a one-command Linux server installer and beginner-friendly recovery instructions.

**Architecture:** Keep the existing FastAPI process as the authenticated control plane and move administration, backup, and restore logic into focused routers/services rather than growing `webhook.py`. KRiTManagement keeps timestamped 7-daily/4-weekly trusted sets plus quarantined suspicious sets in its standard Windows data directory and uses a separately authenticated API client to restore the newest trusted set to a new or existing server. Destructive PostgreSQL replacement runs through a fixed, privileged systemd helper outside the process whose databases are being replaced.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy asyncio, Alembic, PostgreSQL 16, PySide6 6.11.2, httpx 0.28, systemd, Nginx, Bash, pg_dump/pg_restore, pytest, Ruff, Inno Setup.

**Spec:** `docs/superpowers/specs/2026-10-04-administration-access-and-restarts-design.md`

## Global Constraints

- Production bootstrap creates protected `Choose_Goose` with temporary password `123` only when `admin_users` is empty; all business APIs remain blocked until that password is changed.
- SQLite development/tests retain `admin/admin` compatibility.
- Roles are exactly `superadmin`, `director`, and `administrator`; Reports allow the first two, Administration only `superadmin`.
- Never persist or log plain-text passwords, MAX tokens, JWT secrets, PostgreSQL passwords, or restore credentials.
- Automatic restore follows `%LOCALAPPDATA%\KRiT\Backups\KRiT-latest-trusted.json`; the user never selects an arbitrary file, and retention keeps 7 daily plus 4 weekly trusted sets.
- Restore supports both a new empty server and an existing server after accidental deletion; every target gets a server-side safety dump, and an existing server requires password re-entry plus `ВОССТАНОВИТЬ`.
- Production schema changes require Alembic; application startup never silently mutates PostgreSQL schema.
- Release is blocked unless an end-to-end test proves every configured KRiT database can be backed up and restored; the current allowlist contains `krit_bot` and never enumerates unrelated PostgreSQL databases.
- Preserve existing client, learning, communications, MAX, update, and SQLite behavior unless the spec explicitly changes it.

## Review Focus

- Mixed-case or whitespace-padded logins must not bypass case-insensitive uniqueness; Task 2 tests normalized create/update/login collisions.
- A restored database can contain old JWTs and an unknown protected-account password; Task 5 tests token revocation and forced `Choose_Goose/123` change after restore.
- A dropped connection/full disk must not destroy history, and legitimate audited deletion must remain trusted while unexplained loss is quarantined without pruning; Task 4 tests `.part`, atomic-pointer, audit-watermark, and rotation behavior.
- A production server with existing data must require the stronger confirmation path and roll back to its safety dump on restore failure; Task 5 tests populated categories, missing confirmation, and the rolled-back checksum.
- Service/backup/restore buttons can be double-clicked or the process can restart mid-operation; Tasks 3–5 test locks, `409` responses, persisted operation state, and retry behavior.

---

## File Structure

- `bot/src/krit_bot/auth.py` — authenticated administrator principal, role checks, token issue/revocation, and first-password guard.
- `bot/src/krit_bot/administration.py` — user CRUD, audit writes, and service-control API.
- `bot/src/krit_bot/backups.py` — fixed-command PostgreSQL/SQLite backup creation and download metadata.
- `bot/src/krit_bot/restores.py` — restore upload validation, target-state classification, persisted operation state, and privileged-helper dispatch.
- `bot/alembic/versions/20261004_administration_v7.py` — administrator role/profile/session fields and constraints.
- `management/src/krit_management/administration_page.py` — Reports/Admin pages, user forms, service controls, backup and restore workflow.
- `management/src/krit_management/backup_store.py` — standard local paths, metadata/hash verification, and atomic replacement.
- `deploy/install-krit-server.sh` — idempotent Ubuntu/Debian installer.
- `deploy/krit-bot.service`, `deploy/krit-restore.service`, `deploy/nginx-krit.conf` — service and reverse-proxy templates.
- `deploy/krit_restore_helper.py` — allowlisted offline PostgreSQL restoration invoked only by systemd.

### Task 1: Administrator schema and first-run identity

**Files:**
- Create: `bot/alembic/versions/20261004_administration_v7.py`
- Modify: `bot/src/krit_bot/db.py`
- Modify: `bot/src/krit_bot/config.py`
- Test: `bot/tests/test_migrations.py`
- Test: `bot/tests/test_management_api.py`

**Interfaces:**
- Produces: `AdminUser.full_name`, `.role`, `.must_change_password`, `.auth_version`, `.is_protected`, `.updated_at`; role constants `ADMIN_ROLES` and `PROTECTED_ADMIN_USERNAME`.
- Consumes: existing `AdminUser`, Alembic head, `PasswordHash.recommended()`.

- [ ] **Step 1: Write failing migration/model tests** proving existing `admin` becomes active `superadmin`, all new columns are non-null, usernames are normalized/unique case-insensitively, and SQLite metadata matches PostgreSQL migration shape.
- [ ] **Step 2: Run** `pytest bot/tests/test_migrations.py bot/tests/test_management_api.py -q` and confirm failures name the missing revision/fields.
- [ ] **Step 3: Implement the migration and model fields**, advance `EXPECTED_ALEMBIC_REVISION`, and add database constraints for the three roles.
- [ ] **Step 4: Write failing bootstrap tests** for empty PostgreSQL (`Choose_Goose/123`, protected, forced change, created once) and existing/non-empty databases (no reset), while preserving SQLite `admin/admin` fixtures.
- [ ] **Step 5: Implement environment-aware bootstrap** in `config.py`/startup without putting a production password in environment logs or API responses.
- [ ] **Step 6: Run** `pytest bot/tests/test_migrations.py bot/tests/test_management_api.py -q` and expect all passing.
- [ ] **Step 7: Commit** `feat: add administrator roles and secure bootstrap`.

### Task 2: Authentication and complete user-management API

**Files:**
- Create: `bot/src/krit_bot/auth.py`
- Create: `bot/src/krit_bot/administration.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Test: `bot/tests/test_management_api.py`

**Interfaces:**
- Produces: `AdminPrincipal(id: int, username: str, role: str, must_change_password: bool, auth_version: int)`, `require_admin()`, `require_roles(*roles)`, `create_administration_router(...)`.
- Produces endpoints: `POST /api/v1/auth/login`, `POST /api/v1/auth/change-initial-password`, `GET /api/v1/auth/me`, and `/api/v1/administration/users` CRUD/action routes from the spec.
- Consumes: Task 1 administrator fields and existing `learning_audit_events` for immutable audit entries.

- [ ] **Step 1: Write failing auth tests** for token role/profile claims, disabled users, auth-version revocation, forced-password business-API denial, password-change completion, and normalized login.
- [ ] **Step 2: Run** `pytest bot/tests/test_management_api.py -q` and verify the new cases fail.
- [ ] **Step 3: Implement `auth.py`** and replace the integer dependency with `AdminPrincipal` while keeping existing routers compatible through `.id` or an adapter during the same change.
- [ ] **Step 4: Write failing user API tests** for list/create/edit/password/enable/disable/delete, all three roles, audit without secrets, protected account rules, self-action rules, and last-SuperAdmin rules.
- [ ] **Step 5: Implement `administration.py` user routes** with transactions, case-insensitive conflicts, password hashing, and stable Russian error details.
- [ ] **Step 6: Run** `pytest bot/tests/test_management_api.py bot/tests/test_learning_api.py bot/tests/test_communications.py -q` and expect all passing.
- [ ] **Step 7: Commit** `feat: add role-based administration API`.

### Task 3: Bot and KRiT process restart controls

**Files:**
- Modify: `bot/src/krit_bot/administration.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Test: `bot/tests/test_management_api.py`

**Interfaces:**
- Produces: injectable `restart_bot() -> Awaitable[None]` and `request_server_restart() -> Awaitable[None]` callbacks passed to `create_app(...)`.
- Produces endpoints: `POST /api/v1/administration/services/bot/restart` and `POST /api/v1/administration/services/server/restart`.
- Consumes: Task 2 SuperAdmin guard and audit service.

- [ ] **Step 1: Write failing tests** that director/administrator receive `403`, repeated concurrent calls receive `409`, audit commits before callback, bot restart leaves `/health` available, and callback exceptions produce safe `503` responses.
- [ ] **Step 2: Run** the focused management API tests and confirm failure.
- [ ] **Step 3: Extract bot-worker start/stop ownership inside `create_app`** and implement the two injectable callbacks without shell commands built from request data.
- [ ] **Step 4: Implement guarded service endpoints** and schedule server exit only after the HTTP response/audit can complete; production systemd performs the actual restart.
- [ ] **Step 5: Run** `pytest bot/tests/test_management_api.py bot/tests/test_max_callback_webhook.py bot/tests/test_release_safety.py -q` and expect all passing.
- [ ] **Step 6: Commit** `feat: add safe KRiT service restart controls`.

### Task 4: Complete server backup API and verified local backup store (release-critical)

**Files:**
- Create: `bot/src/krit_bot/backups.py`
- Modify: `bot/src/krit_bot/config.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Create: `management/src/krit_management/backup_store.py`
- Modify: `management/src/krit_management/api.py`
- Test: `bot/tests/test_backups.py`
- Test: `management/tests/test_backups.py`

**Interfaces:**
- Produces server `POST /api/v1/administration/backups`, `GET /api/v1/administration/backups/{id}`, and metadata `{id, size, sha256, schema_version, created_at}`.
- Produces client `BackupStore(root: Path)` with `latest_trusted()`, `install_from_stream(...)`, `rotate(daily=7, weekly=4)`, `trust(...)`, `delete_suspicious(...)`, and `discard_partial()`.
- Produces `ManagementApi.create_backup()` and `.download_backup(backup_id, destination, progress)`.

- [ ] **Step 1: Write failing backend tests** for SuperAdmin authorization, every configured KRiT database included, unrelated databases excluded, fixed `pg_dump` argv/env, SQLite backup, one-operation lock, per-database SHA-256/size/schema/critical-count/audit-watermark metadata, expiry, and sanitized failures.
- [ ] **Step 2: Implement `backups.py`** using async subprocess execution for PostgreSQL and SQLite backup API for test/development.
- [ ] **Step 3: Run** `pytest bot/tests/test_backups.py -q` and expect all passing.
- [ ] **Step 4: Write failing client-store tests** for `%LOCALAPPDATA%` default, timestamped names, successful `.part` verification/atomic pointer update, 7-daily/4-weekly oldest-first rotation, bad hash, interrupted stream, full disk, legitimate audited deletion, unexplained-loss quarantine, trust/delete actions, and preservation of every prior trusted set on failure.
- [ ] **Step 5: Implement `backup_store.py` and streaming API methods** without loading the archive into memory.
- [ ] **Step 6: Run** `pytest management/tests/test_backups.py -q` and expect all passing.
- [ ] **Step 7: Commit** `feat: add verified disaster-recovery backups`.

### Task 5: New- and existing-server automatic restore pipeline

**Files:**
- Create: `bot/src/krit_bot/restores.py`
- Create: `deploy/krit_restore_helper.py`
- Modify: `bot/src/krit_bot/config.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Modify: `management/src/krit_management/api.py`
- Test: `bot/tests/test_restores.py`
- Test: `management/tests/test_backups.py`

**Interfaces:**
- Produces restore endpoints from the spec and `RestoreState(id, phase, transferred, total, error)` persisted under `/var/lib/krit/restore`.
- Produces `ManagementApi.for_server(base_url)`, `.create_restore(metadata)`, `.upload_restore(...)`, `.apply_restore(...)`, `.restore_status(...)`.
- Consumes: Task 2 SuperAdmin authentication, Task 4 verified `BackupInfo`, and fixed paths from deployment configuration.

- [ ] **Step 1: Write failing API tests** for authentication, streamed size/hash verification, invalid archive/schema, upload expiry, backup/restore lock sharing, and no client-controlled filesystem or command values.
- [ ] **Step 2: Write failing target-state tests** for the simple bootstrap-only path, existing clients/lessons/messages/admins requiring password re-entry plus exact `ВОССТАНОВИТЬ`, and rejection without either confirmation.
- [ ] **Step 3: Implement `restores.py` upload/state/guard logic** and dispatch only a fixed operation ID to the privileged helper.
- [ ] **Step 4: Write failing helper tests** proving every configured KRiT database is safety-dumped, dropped/recreated and fully replaced from its matching archive; also test fixed pg_restore commands, maintenance marker, whole-set rollback after any restore/migration failure, protected-account reset, auth-version invalidation, and restart ordering.
- [ ] **Step 5: Implement `krit_restore_helper.py`** with allowlisted ownership/permissions and no request-derived shell evaluation.
- [ ] **Step 6: Write and implement client streaming tests/methods** for progress, retryable polling, password lifetime, health recovery, and forced re-login.
- [ ] **Step 7: Run** `pytest bot/tests/test_restores.py management/tests/test_backups.py -q` and expect all passing.
- [ ] **Step 8: Commit** `feat: restore trusted backups to KRiT servers`.

### Task 6: Reports, Administration, mandatory password UI, and scheduling

**Files:**
- Create: `management/src/krit_management/administration_page.py`
- Modify: `management/src/krit_management/api.py`
- Modify: `management/src/krit_management/dialogs.py`
- Modify: `management/src/krit_management/main.py`
- Modify: `management/src/krit_management/window.py`
- Modify: `management/src/krit_management/workers.py`
- Test: `management/tests/test_administration_ui.py`

**Interfaces:**
- Produces `ReportsPage`, `AdministrationPage`, `AdminUserDialog`, `ChangePasswordDialog`, and `RestoreServerDialog`.
- Consumes: Task 2 login profile/user routes, Task 3 controls, Task 4 `BackupStore`, Task 5 temporary server client.

- [ ] **Step 1: Write failing login/navigation tests** for mandatory first-password change and exact visibility/order: Clients, Learning, Communications, Reports, Administration.
- [ ] **Step 2: Implement profile-aware login and role-filtered navigation**, including the styled Reports placeholder.
- [ ] **Step 3: Write failing administration UI tests** for all user actions, protected/self/last-SuperAdmin feedback, confirmation dialogs, and refresh after success.
- [ ] **Step 4: Implement the user table/forms and service-control cards** using background workers and disabled busy buttons.
- [ ] **Step 5: Write failing backup/restore UI tests** for startup daily schedule, 24-hour throttle, manual backup, 7-daily/4-weekly history, suspicious-copy explanation/trust/delete actions, automatically discovered latest trusted set, address/port/login/password form with no file chooser, new/existing target indication, strong existing-server confirmation, upload progress, and forced re-login.
- [ ] **Step 6: Implement backup scheduling and restore workflow**; clear the restore password immediately after request completion/failure and never place it in settings.
- [ ] **Step 7: Run** `pytest management/tests/test_administration_ui.py management/tests/test_learning_ui.py management/tests/test_communications_ui.py -q` and expect all passing.
- [ ] **Step 8: Commit** `feat: add administration and disaster recovery UI`.

### Task 7: One-command Ubuntu/Debian server deployment

**Files:**
- Create: `deploy/install-krit-server.sh`
- Create: `deploy/krit-bot.service`
- Create: `deploy/krit-restore.service`
- Create: `deploy/nginx-krit.conf`
- Create: `deploy/build-server-package.ps1`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `RESTORE_DATABASES.txt`
- Test: `bot/tests/test_deployment_assets.py`

**Interfaces:**
- Produces `KRiTServer.tar.gz` containing the `bot` package, Alembic files, restore helper, templates, installer, and checksums.
- Installer consumes a MAX token and HTTPS hostname, generates JWT/webhook/database secrets, creates `/opt/krit-bot`, `/etc/krit-bot/krit-bot.env`, `/var/lib/krit/{backups,restore}`, PostgreSQL role/database, Nginx route, and enabled systemd units.
- Consumes: Tasks 3–5 service/recovery entry points.

- [ ] **Step 1: Write failing static deployment tests** for required archive entries, restrictive modes, `Restart=always`, localhost API binding, fixed restore helper permissions, no committed secrets, and idempotent install markers.
- [ ] **Step 2: Implement systemd/Nginx templates and package builder**; package checksums must be deterministic enough for release verification.
- [ ] **Step 3: Implement `install-krit-server.sh`** for supported Ubuntu/Debian with preflight checks, clear Russian prompts, generated secrets, PostgreSQL setup, virtualenv install, migrations, MAX webhook, health check, and rollback message on failure.
- [ ] **Step 4: Rewrite `RESTORE_DATABASES.txt` as a numbered beginner guide** covering: SSH command (`ssh root@SERVER_IP`), archive upload, one installer command, which answers to enter, successful-screen examples, KRiTManagement connection, automatic restore, verification, and manual emergency fallback. Never include the real server password/token.
- [ ] **Step 5: Update `README.md` and `.env.example`** to match first-run `Choose_Goose/123`, deployment paths, HTTPS requirement, and service commands.
- [ ] **Step 6: Run** `pytest bot/tests/test_deployment_assets.py -q` and a shell syntax check (`bash -n deploy/install-krit-server.sh`); expect all passing.
- [ ] **Step 7: Commit** `build: add one-command KRiT server installer`.

### Task 8: Full regression, packaging, push, and release

**Files:**
- Modify: `bot/pyproject.toml`
- Modify: `management/pyproject.toml`
- Modify: `management/src/krit_management/version.py`
- Modify: `management/packaging/windows-installer.iss` only if recovery documentation is not already packaged.
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces synchronized version `0.4.0`, `SetupKrit.exe`, `KRiTServer.tar.gz`, checksums, pushed commits/tag, and GitHub release.
- Consumes: every preceding task and existing build/release workflow.

- [ ] **Step 1: Add CI jobs/checks** for PostgreSQL backup/restore, deployment assets, all bot tests, all management tests, Ruff, Windows build, and both release artifacts.
- [ ] **Step 2: Run** `ruff check bot/src bot/tests management/src management/tests` and fix only relevant violations.
- [ ] **Step 3: Run** the complete bot and management suites, including configured PostgreSQL tests; record exact pass/skip counts.
- [ ] **Step 4: Build `SetupKrit.exe` and `KRiTServer.tar.gz`**, verify embedded `RESTORE_DATABASES.txt`, version metadata, icons, SHA-256, and smoke-start the Windows executable.
- [ ] **Step 5: Perform disposable end-to-end recovery rehearsals** on both an empty and a populated server: create/download the complete backup set, replace every configured KRiT database through the client API, wait for health, re-login, and compare clients, lessons, messages, administrators, schema revisions, and per-database counts/checksums.
- [ ] **Step 6: Use `superpowers:requesting-code-review`**, resolve findings, then use `superpowers:verification-before-completion` and rerun affected checks.
- [ ] **Step 7: Commit release metadata, push `main`, create and push tag `v0.4.0`, publish both artifacts/checksums and Russian release notes, and attach the resulting pull request if one is created.

## Self-Review Result

- Spec coverage: every access, bootstrap, user-management, service-control, backup, restore, UI, installer, documentation, test, and release requirement maps to Tasks 1–8.
- Step scan: each task has a failing-test, implementation, passing-test, and commit boundary; deployment documentation is tested as a release asset.
- Type consistency: `AdminPrincipal`, `BackupInfo`/metadata, restore operation IDs, and page/API method boundaries are introduced before consumers.
- Review focus: all five high-risk conditions are assigned explicit tests.
- Proportion: implementation bodies are intentionally omitted; only public interfaces, fixed values, and verification outcomes are specified.
