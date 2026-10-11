# Chat Scroll to Latest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При открытии диалога и отправке ответа показывать последнее сообщение, не сбрасывая пользователя вниз при чтении истории во время фонового обновления.

**Architecture:** Перед перерисовкой фиксируется, был ли scrollbar у нижней границы. Явный выбор диалога и отправка собственного сообщения выставляют force-scroll; после `setHtml` прокрутка выполняется отложенно через `QTimer.singleShot(0, ...)`, когда Qt уже пересчитал maximum.

**Tech Stack:** Python, PySide6, pytest-qt.

**Spec:** `docs/superpowers/specs/2026-10-11-distributed-local-backups-design.md`

## Global Constraints

- Явное открытие диалога всегда показывает последнее сообщение.
- После отправки ответа показывается добавленный optimistic message.
- Фоновый refresh сохраняет позицию, если пользователь читает историю.
- Если пользователь уже был внизу, новые сообщения продолжают удерживать чат внизу.

## Review Focus

- Qt меняет scrollbar maximum только после event loop — Task 1.
- Ответ API другого, уже закрытого диалога не прокручивает текущий — Task 1.
- Пустая история имеет корректную позицию — Task 1.
- Фоновый refresh с изменившейся высотой сохраняет относительную видимую позицию — Task 1.
- Быстрое переключение двух диалогов применяет force-scroll только к актуальному person_id — Task 1.

---

### Task 1: Управляемая отложенная прокрутка

**Files:**
- Modify: `management/src/krit_management/communications_page.py`
- Modify: `management/tests/test_communications_ui.py`

**Interfaces:**
- Produces: `CommunicationsPage._is_chat_at_bottom() -> bool`.
- Produces: `CommunicationsPage._scroll_chat_to_bottom_deferred(person_id: int | None) -> None`.
- Changes: `_render_chat_messages(*, force_bottom: bool = False) -> None`.

- [ ] **Step 1: Write failing UI tests**

Добавить проверки: выбранный диалог после `qtbot.wait(0)` имеет `value == maximum`; `_reply_sent` делает то же; background refresh сохраняет значение scrollbar вдали от низа; background refresh следует вниз, если пользователь был у низа; stale person id не меняет текущую прокрутку.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project management pytest management/tests/test_communications_ui.py -q`
Expected: FAIL because scrolling currently happens synchronously before Qt recalculates maximum.

- [ ] **Step 3: Implement bottom detection and deferred scrolling**

Считать нижней позицией расстояние не более 4 px до maximum. Перед `setHtml` сохранить старые `value`, `maximum` and bottom state; после repaint использовать deferred callback, проверяющий актуальный `current_person_id`.

- [ ] **Step 4: Wire explicit and background cases**

`_dialog_selected` and `_reply_sent` request `force_bottom=True`. `_messages_loaded_for` передаёт обычный режим: follow only when already at bottom, otherwise restores reading position after document layout.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project management pytest management/tests/test_communications_ui.py -q`
Expected: PASS.

`git add management/src/krit_management/communications_page.py management/tests/test_communications_ui.py && git commit -m "fix: show latest message when opening chats"`
