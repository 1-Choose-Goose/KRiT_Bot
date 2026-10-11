# Administration Server Info Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вынести полную информацию о сервере в отдельную удобную вкладку, сохранив лёгкое обновление только при открытом разделе «Администрирование».

**Architecture:** `AdministrationPage` получает внутренний `QTabWidget`: «Управление» содержит краткие карточки, пользователей, службы и резервирование; «Информация о сервере» содержит полноразмерное дерево показателей. Существующий lifecycle `activate/deactivate` остаётся единственной точкой включения 15-секундного polling.

**Tech Stack:** Python, PySide6, pytest-qt.

**Spec:** `docs/superpowers/specs/2026-10-11-distributed-local-backups-design.md`

## Global Constraints

- Разделы бокового меню видны всем, но «Администрирование» доступно только SuperAdmin.
- Полная информация не ограничивается высотой 235 px.
- Polling не работает, пока раздел «Администрирование» закрыт.
- Существующие операции пользователей, служб, копий и восстановления сохраняют поведение.

## Review Focus

- Переключение внутренней вкладки не создаёт второй timer — Task 1.
- Длинные значения и группы полностью доступны с прокруткой — Task 1.
- Открытие раздела запускает один немедленный refresh, закрытие останавливает timer — Task 1.
- Ошибка status API не ломает управление пользователями — Task 1.
- Все ранее отображавшиеся группы остаются в дереве — Task 1.

---

### Task 1: Две вкладки администрирования

**Files:**
- Modify: `management/src/krit_management/administration_page.py`
- Modify: `management/tests/test_administration_ui.py`

**Interfaces:**
- Preserves: `AdministrationPage.activate()`, `deactivate()`, `shutdown()`, `refresh_status()`.
- Produces: `self.tabs: QTabWidget`, вкладки `Управление` and `Информация о сервере`.

- [ ] **Step 1: Write failing UI tests**

`test_administration_uses_separate_full_height_server_info_tab` проверяет названия вкладок, размещение `status_details` во второй вкладке, отсутствие maximum height и сохранение шести групп. `test_administration_status_polling_only_runs_while_section_visible` проверяет один timer и lifecycle.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project management pytest management/tests/test_administration_ui.py -q`
Expected: FAIL because details are embedded in the management scroll card.

- [ ] **Step 3: Refactor `AdministrationPage.__init__` into tab builders**

Добавить `_build_management_tab() -> QWidget` and `_build_server_info_tab() -> QWidget`; краткие status cards остаются на первой, `status_details` растягивается на всю вторую вкладку. Не менять публичные handlers.

- [ ] **Step 4: Preserve lifecycle and error isolation**

`activate` выполняет немедленный `refresh_status` и запускает существующий timer только один раз; `deactivate` останавливает его независимо от выбранной внутренней вкладки.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project management pytest management/tests/test_administration_ui.py -q`
Expected: PASS.

`git add management/src/krit_management/administration_page.py management/tests/test_administration_ui.py && git commit -m "feat: move server details to administration tab"`
