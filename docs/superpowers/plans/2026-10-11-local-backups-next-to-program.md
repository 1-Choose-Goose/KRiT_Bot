# Local Backups Next to Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создавать проверенные резервные копии PostgreSQL только в `Backups` рядом с KRiTManagement на каждом компьютере с активной учётной записью программы, не сохраняя плановые архивы на сервере.

**Architecture:** Сервер отдаёт разрешённые дампы PostgreSQL потоками с обязательным завершающим кадром размера и SHA-256; клиент собирает совместимый `.backup` локально, атомарно устанавливает его и только затем выполняет ротацию. Один `BackupController` главного окна обслуживает все три роли и передаётся странице администрирования для ручного запуска и отображения.

**Tech Stack:** Python 3.12, FastAPI/StreamingResponse, SQLAlchemy/Alembic, httpx, PySide6, Inno Setup, pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-distributed-local-backups-design.md`

## Global Constraints

- Рабочая PostgreSQL-база остаётся на сервере; плановые `.backup` и дампы на сервере не создаются.
- Единственный локальный каталог — `<каталог программы>\Backups`; скрытого перехода в `%LOCALAPPDATA%` нет.
- Автокопирование запускается после входа для SuperAdmin, директора и администратора не чаще одного раза за 24 часа на установку.
- Ротация запускается только после успешной проверки новой копии и сохраняет 7 ежедневных и 4 еженедельных доверенных комплекта.
- Подозрительные комплекты автоматически не удаляются и не становятся последней доверенной копией.
- Восстановление и решения по подозрительным комплектам доступны только SuperAdmin.
- Не публиковать GitHub-релиз и не отправлять изменения в GitHub до отдельного указания пользователя.

## Review Focus

- Каталог программы недоступен для записи: показать точный путь и не создавать копию в другом месте — Task 1.
- Соединение оборвалось до завершающего кадра: удалить `.part`, не менять trusted pointer и не запускать ротацию — Tasks 2–3.
- Два компьютера одновременно запрашивают экспорт: ограничить параллелизм без постоянных серверных файлов — Task 2.
- Клиент удалил данные законно: audit watermark отличает это от необъяснимого падения счётчиков — Task 3.
- Обновление или переустановка программы: сохранить каталог `Backups` и его содержимое — Task 1.

---

### Task 1: Каталог `Backups` рядом с программой

**Files:**
- Modify: `management/src/krit_management/backup_store.py`
- Modify: `management/packaging/windows-installer.iss`
- Modify: `management/tests/test_backups.py`

**Interfaces:**
- Produces: `application_directory() -> Path`, `BackupStore.default() -> BackupStore` с корнем `<application_directory>/Backups`.

- [ ] **Step 1: Write the failing path and installer tests**

Добавить `test_default_store_uses_directory_next_to_frozen_executable`, `test_source_default_store_uses_management_directory`, `test_default_store_never_falls_back_to_localappdata` и проверку текста installer script: `[Dirs]` создаёт `{app}\Backups`, а uninstall/update не удаляет пользовательские файлы каталога.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project management pytest management/tests/test_backups.py -q`
Expected: FAIL на старом пути `%LOCALAPPDATA%\KRiT\Backups`.

- [ ] **Step 3: Implement `application_directory() -> Path` and installer directory permissions**

В frozen-режиме использовать `Path(sys.executable).resolve().parent`, в исходном проекте — каталог `management`, содержащий `main.py`. `BackupStore.default()` возвращает только `application_directory() / "Backups"`. В Inno Setup создать `{app}\Backups` как сохраняемый каталог с правом записи пользователя; не добавлять fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project management pytest management/tests/test_backups.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git add management/src/krit_management/backup_store.py management/packaging/windows-installer.iss management/tests/test_backups.py && git commit -m "fix: store backups next to management app"`

### Task 2: Потоковый экспорт без серверного архива

**Files:**
- Create: `bot/src/krit_bot/backup_exports.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Modify: `bot/tests/test_backups.py`
- Create: `bot/tests/test_backup_exports.py`

**Interfaces:**
- Produces: `BackupExportService.begin(principal_id: int, installation_id: str) -> Awaitable[dict[str, Any]]`.
- Produces: `BackupExportService.stream_database(export_id: str, database_name: str) -> AsyncIterator[bytes]` using protocol `KRITDB1` with length-prefixed chunks and a final JSON frame containing `complete`, `size`, and `sha256`.
- Produces: `create_backup_export_router(require_admin, service) -> APIRouter` at `/api/v1/backups/exports`.

- [ ] **Step 1: Write failing service and API tests**

Проверить все три роли, запрет inactive/must-change-password, allowlist базы, обязательный финальный кадр, обрыв pg_dump, лимит одновременных экспортов и отсутствие новых файлов в `/var/lib/krit/backups`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project bot pytest bot/tests/test_backup_exports.py -q`
Expected: FAIL because streaming export API is absent.

- [ ] **Step 3: Implement the framed streaming service and router**

`pg_dump --format=custom` пишет только в stdout. Каждый data frame содержит 4-byte big-endian length и bytes; нулевая длина завершает данные, затем идёт length-prefixed JSON footer. Ошибка процесса завершает поток без footer, поэтому клиент не может принять обрезанный дамп.

- [ ] **Step 4: Disable production scheduler and persistent backup router**

В `webhook.create_app` не создавать/не запускать `BackupScheduler` и не подключать маршрут, который сначала создаёт серверный `.backup`; подключить новый export router. Не затрагивать временные safety-файлы восстановления.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project bot pytest bot/tests/test_backup_exports.py bot/tests/test_backups.py bot/tests/test_restores.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

`git add bot/src/krit_bot/backup_exports.py bot/src/krit_bot/webhook.py bot/tests/test_backup_exports.py bot/tests/test_backups.py && git commit -m "feat: stream database exports without server archives"`

### Task 3: Клиентская сборка, проверка и ротация

**Files:**
- Modify: `management/src/krit_management/api.py`
- Modify: `management/src/krit_management/backup_store.py`
- Modify: `management/tests/test_backups.py`

**Interfaces:**
- Produces: `ManagementApi.begin_backup_export(installation_id: str) -> dict[str, Any]`.
- Produces: `ManagementApi.download_backup_database(export_id: str, database_name: str, destination: Path, progress=None) -> dict[str, Any]`.
- Produces: `BackupStore.install_export(manifest: dict[str, Any], database_files: Mapping[str, Path]) -> BackupEntry`.

- [ ] **Step 1: Write failing protocol, atomicity, and rotation tests**

Проверить валидный footer, отсутствие footer, несовпадающий SHA-256, сетевой обрыв, отсутствие места, атомарную установку, отсутствие ротации при ошибке, 7 daily + 4 weekly, сохранение suspicious и отсутствие записи вне `<program>/Backups`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project management pytest management/tests/test_backups.py -q`
Expected: FAIL because export protocol and local assembly are absent.

- [ ] **Step 3: Implement API stream parser**

`download_backup_database` принимает только поток с magic `KRITDB1` и полным footer, вычисляет размер/SHA-256 независимо и удаляет destination при любом несоответствии.

- [ ] **Step 4: Implement local archive assembly**

`install_export` создаёт zip-compatible `.backup.part`, записывает дампы и `manifest.json`, повторно вызывает `_verify_archive`, атомарно переименовывает и запускает `rotate(daily=7, weekly=4)` только для trusted результата.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project management pytest management/tests/test_backups.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

`git add management/src/krit_management/api.py management/src/krit_management/backup_store.py management/tests/test_backups.py && git commit -m "feat: assemble and rotate verified local backups"`

### Task 4: Автокопирование для каждой роли и ручной запуск SuperAdmin

**Files:**
- Create: `management/src/krit_management/backup_controller.py`
- Modify: `management/src/krit_management/window.py`
- Modify: `management/src/krit_management/administration_page.py`
- Modify: `management/tests/test_administration_ui.py`
- Modify: `management/tests/test_backups.py`

**Interfaces:**
- Consumes: Task 3 export and store interfaces.
- Produces: `BackupController.start_if_due() -> None`, `BackupController.start(manual: bool = False) -> None`, signals `completed`, `failed`, `state_changed`.

- [ ] **Step 1: Write failing controller and role tests**

Проверить единичный запуск после входа для каждой роли, 24-hour guard, отсутствие параллельного запуска, ненавязчивую ошибку пути, ручной запуск SuperAdmin и использование одного controller/store в MainWindow и AdministrationPage.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project management pytest management/tests/test_backups.py management/tests/test_administration_ui.py -q`
Expected: FAIL because backup execution still belongs to AdministrationPage.

- [ ] **Step 3: Implement `BackupController` and wire it in `MainWindow`**

MainWindow создаёт controller для любой активной программной роли и вызывает `start_if_due` через 5 секунд. AdministrationPage получает тот же controller и не создаёт отдельную задачу.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project management pytest management/tests/test_backups.py management/tests/test_administration_ui.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git add management/src/krit_management/backup_controller.py management/src/krit_management/window.py management/src/krit_management/administration_page.py management/tests/test_backups.py management/tests/test_administration_ui.py && git commit -m "feat: back up databases on every management installation"`

### Task 5: Учёт установок и безопасная миграция

**Files:**
- Create: `bot/alembic/versions/20261011_backup_exports_v8.py`
- Modify: `bot/src/krit_bot/backup_exports.py`
- Modify: `bot/src/krit_bot/administration.py`
- Modify: `management/src/krit_management/api.py`
- Modify: `management/src/krit_management/backup_controller.py`
- Modify: `bot/tests/test_backup_exports.py`
- Modify: `bot/tests/test_administration.py`

**Interfaces:**
- Produces: `ManagementApi.complete_backup_export(export_id: str, installation_id: str, size: int, sha256: str) -> dict[str, Any]`.
- Extends: system status with `local_backup_installations.current`, `last_success_at`, and `last_failure_at`; stores metadata only.

- [ ] **Step 1: Write failing receipt and status tests**

Проверить idempotent completion, one row per installation/export, no archive content in PostgreSQL, rejected forged export id, current-installation calculation and failed-attempt audit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project bot pytest bot/tests/test_backup_exports.py bot/tests/test_administration.py -q`
Expected: FAIL because receipt storage is absent.

- [ ] **Step 3: Add migration, completion endpoint, and status aggregation**

Хранить только идентификаторы, timestamps, size, SHA-256 and result; never archive bytes, passwords, tokens or database rows.

- [ ] **Step 4: Report verified completion from the client**

Controller вызывает completion endpoint только после `install_export` и успешной локальной проверки.

- [ ] **Step 5: Run focused and full suites**

Run: `uv run --project bot pytest bot/tests/test_backup_exports.py bot/tests/test_administration.py -q`
Expected: PASS.

Run: `uv run --project management pytest management/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

`git add bot/alembic/versions/20261011_backup_exports_v8.py bot/src/krit_bot/backup_exports.py bot/src/krit_bot/administration.py management/src/krit_management/api.py management/src/krit_management/backup_controller.py bot/tests/test_backup_exports.py bot/tests/test_administration.py && git commit -m "feat: audit distributed local backup coverage"`

### Task 6: Финальная проверка без публикации

**Files:**
- Modify: `RESTORE_DATABASES.txt`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update recovery documentation**

Указать единственный путь `<каталог KRiTManagement.exe>\Backups`, правила выбора trusted/suspicious, перенос копии с другого компьютера и временные safety-файлы восстановления.

- [ ] **Step 2: Run all relevant checks**

Run: `uv run --project bot pytest bot/tests -q`

Run: `uv run --project management pytest management/tests -q`

Run: `uv run --project bot ruff check bot/src bot/tests`

Run: `uv run --project management ruff check management/src management/tests`

Expected: all PASS.

- [ ] **Step 3: Verify installer semantics without publishing**

Собрать только локально по отдельному подтверждению пользователя; проверить, что upgrade сохраняет заранее заполненный `{app}\Backups`. Серверные архивы и старый `%LOCALAPPDATA%\KRiT\Backups` уже удалены с разрешения пользователя; проверить, что они не создаются повторно. Не выполнять push или GitHub release.

- [ ] **Step 4: Commit documentation only**

`git add RESTORE_DATABASES.txt CHANGELOG.md && git commit -m "docs: document distributed local backup recovery"`
