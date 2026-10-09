# KRiT Reliability Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Исправить подтверждённые недостатки установки, резервного копирования, авторизации, фоновых очередей, MAX-интеграции, очистки сообщений и desktop refresh без изменения согласованного UI.

**Architecture:** Сохраняется текущая FastAPI/PySide6/SQLAlchemy архитектура. Надёжность добавляется точечно через существующие сервисы, фоновые lifespan-задачи, безопасные ограничения запросов и тестируемую координацию операций.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy asyncio, PostgreSQL/SQLite, PySide6, pytest, Bash, Nginx.

**Spec:** `docs/superpowers/specs/2026-10-09-reliability-audit-design.md`

## Global Constraints

- Не изменять production-сервер, production-базу, токены и пароли.
- Не менять навигацию, компоновку, пользовательские тексты и заглушку «Отчёты».
- Не добавлять шифрование резервных копий, новую архитектуру или зависимости без необходимости.
- Сохранять JWT/auth_version, at-least-once semantics и текущие рабочие бизнес-функции.
- Все исправления подтверждать тестом RED→GREEN; финальный отчёт содержит точные результаты.

## Review Focus

- Повторный запуск установщика с существующим env не должен менять секреты, пароль БД или данные.
- Одновременный scheduler/manual backup/restore должен дать только одного владельца операции и не оставить `.part`.
- Атака с меняющимися логинами или IP не должна разрастить limiter без границ и не должна блокировать корректного пользователя глобально.
- Поздний ответ MAX на старую клавиатуру и повреждённый callback не должны падать или менять чужой запрос.
- Закрытие desktop во время выполняющегося снимка не должно применить результат после shutdown.

---

### Task 1: Безопасный установщик и Nginx

**Files:**
- Modify: `deploy/install-krit-server.sh`
- Modify: `deploy/nginx-krit.conf`
- Test: `bot/tests/test_deployment_assets.py`

**Interfaces:**
- Consumes: существующие env-переменные и systemd unit names.
- Produces: preflight Python 3.13+, идемпотентное сохранение env/секретов и route-specific body limits.

- [ ] Написать тесты, фиксирующие preflight до мутаций, сохранение существующих секретов и малый общий/большой restore-only Nginx лимит.
- [ ] Запустить `pytest bot/tests/test_deployment_assets.py -q` и подтвердить падение новых тестов.
- [ ] Реализовать минимальные изменения установщика и Nginx.
- [ ] Повторить тест до PASS и закоммитить.

### Task 2: Автономное серверное резервное копирование

**Files:**
- Modify: `bot/src/krit_bot/config.py`
- Modify: `bot/src/krit_bot/backups.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Test: `bot/tests/test_backups.py`

**Interfaces:**
- Consumes: `BackupService.create()` и restore conflict checker.
- Produces: `BackupScheduler.run()`, проверка архива и rotation 7 daily + 4 weekly.

- [ ] Написать тесты расписания после рестарта, взаимного исключения, целостности и безопасной rotation.
- [ ] Подтвердить RED командой `pytest bot/tests/test_backups.py -q`.
- [ ] Реализовать scheduler поверх `BackupService`, атомарную проверку и retention без удаления newest/in-progress.
- [ ] Подключить/остановить задачу в lifespan, получить GREEN и закоммитить.

### Task 3: Ограничение попыток входа и единая серверная роль

**Files:**
- Modify: `bot/src/krit_bot/webhook.py`
- Modify: `management/src/krit_management/window.py`
- Test: `bot/tests/test_management_api.py`
- Test: `management/tests/test_administration_ui.py`

**Interfaces:**
- Consumes: нормализованный username, Request.client, `AdminPrincipal.role`.
- Produces: bounded `LoginRateLimiter`, HTTP 429 с `Retry-After`, UI-role только из server profile.

- [ ] Добавить тесты порога/сброса/изоляции limiter и отсутствия special-case username `admin`.
- [ ] Подтвердить RED целевыми pytest-тестами.
- [ ] Реализовать limiter и удалить username override роли.
- [ ] Получить GREEN и закоммитить.

### Task 4: Восстановление очередей и повтор подписки MAX

**Files:**
- Modify: `bot/src/krit_bot/learning_notifications.py`
- Modify: `bot/src/krit_bot/webhook.py`
- Test: `bot/tests/test_learning_notifications.py`
- Test: `bot/tests/test_max_callback_webhook.py`

**Interfaces:**
- Consumes: `recover_interrupted()`, `ensure_max_webhook_subscription()`.
- Produces: периодическое stale recovery и bounded background subscription retry.

- [ ] Добавить тесты, что активное processing не возвращается, stale возвращается периодически, подписка повторяется без блокировки и без параллельных дублей.
- [ ] Подтвердить RED.
- [ ] Реализовать минимальные фоновые циклы с корректной отменой.
- [ ] Получить GREEN и закоммитить.

### Task 5: Безопасные callbacks и авторизация операций

**Files:**
- Modify: `bot/src/krit_bot/handler.py`
- Modify: `bot/src/krit_bot/administration.py`
- Test: `bot/tests/test_handler.py`
- Test: `bot/tests/test_management_api.py`

**Interfaces:**
- Consumes: callback payload и `AdminPrincipal.role`.
- Produces: безопасный parser числовых id, harmless stale response, сохранение last-superadmin/protected/auth_version правил.

- [ ] Добавить тесты malformed/stale callback и матрицы защищённых административных операций.
- [ ] Подтвердить RED.
- [ ] Реализовать валидацию до преобразований/мутаций и устранить найденные обходы роли.
- [ ] Получить GREEN и закоммитить.

### Task 6: Безопасная очистка истории переписки

**Files:**
- Modify: `bot/src/krit_bot/communications.py`
- Test: `bot/tests/test_communications.py`

**Interfaces:**
- Consumes: message age/status/references и thread read state.
- Produces: cleanup только старше 30 дней и только без активных business references; корректный preview/unread rebuild.

- [ ] Добавить тесты failed/unavailable моложе 30 дней, активных ссылок, unread и preview.
- [ ] Подтвердить RED.
- [ ] Исправить predicate и пересчёт thread metadata.
- [ ] Получить GREEN и закоммитить.

### Task 7: Стабильный desktop refresh

**Files:**
- Modify: `management/src/krit_management/window.py`
- Test: `management/tests/test_learning_ui.py`

**Interfaces:**
- Consumes: `ManagementApi.snapshot()`.
- Produces: generation-aware refresh, snapshot equality gate, safe shutdown.

- [ ] Добавить тесты single-flight, stale result, unchanged snapshot, сохранения выбора/фильтра и shutdown.
- [ ] Подтвердить RED.
- [ ] Реализовать generation/equality guards без изменения UI.
- [ ] Получить GREEN и закоммитить.

### Task 8: Полная проверка и документация результата

**Files:**
- Modify only if evidence requires: `README.md`, `CHANGELOG.md`
- Test: full repository suites and PostgreSQL suites when service is available.

**Interfaces:**
- Consumes: результаты Tasks 1-7.
- Produces: проверенный branch и доказательный отчёт.

- [ ] Запустить ruff и compileall.
- [ ] Запустить полный `bot/tests` и `management/tests` с точным подсчётом.
- [ ] Запустить миграции и изолированные PostgreSQL-тесты; явно отметить недоступные внешние проверки.
- [ ] Провести whole-branch review, исправить Critical/Important через RED→GREEN и выполнить финальный полный прогон.
- [ ] Обновить документацию только по реально изменённому поведению и закоммитить.
