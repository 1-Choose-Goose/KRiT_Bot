from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QDateTime, QPoint, QPointF, QSizeF, Qt, QTime
from PySide6.QtGui import QPageLayout, QPdfWriter, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QInputDialog,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionSpinBox,
    QTableWidget,
)

import krit_management.learning_page as learning_page_module
from krit_management.api import ApiError, ManagementApi
from krit_management.dialogs import PersonDialog
from krit_management.learning_page import (
    FreeSlotDialog,
    LearningPage,
    LessonCardDialog,
    LessonDialog,
    ReasonDialog,
    ReferenceDialog,
    SchedulePreviewDialog,
)
from krit_management.main import build_stylesheet
from krit_management.widgets import SafeComboBox, SearchableComboBox
from krit_management.window import MainWindow


class FakeApi:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "ok",
            "people": [],
            "archived_people": [],
            "access_attempts": [],
        }

    def learning_reference_data(self) -> dict[str, list[object]]:
        return {
            "subjects": [],
            "rooms": [],
            "groups": [],
            "students": [],
            "teachers": [],
        }

    def learning_today(self) -> dict[str, list[object]]:
        return {"lessons": [], "present": [], "alerts": []}

    def learning_lessons(self, date_from: str, date_to: str) -> list[object]:
        assert date_from
        assert date_to
        return []

    def learning_lesson(self, lesson_id: int) -> dict[str, object]:
        return {"id": lesson_id, "participants": []}

    def lesson_action(
        self, lesson_id: int, action: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {"id": lesson_id, "action": action, "payload": payload or {}}

    def reconcile_lesson(
        self, lesson_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        return {"id": lesson_id, "payload": payload}

    def close(self) -> None:
        pass


def test_main_window_loads_learning_calendar_without_worker_argument_error() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    window.show()

    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    app.processEvents()

    assert window.learning_page.calendar_table.rowCount() == 0
    assert [window.client_tabs.tabText(index) for index in range(4)] == [
        "Ученики",
        "Учителя",
        "Родители",
        "Все",
    ]
    assert window.learning_page.calendar_date.displayFormat() == "dd.MM.yyyy"
    assert window.learning_page.calendar_end.displayFormat() == "dd.MM.yyyy"
    assert window.learning_page.calendar_table.horizontalScrollBar().maximum() == 0
    assert all(button.text() != "Обновить" for button in window.findChildren(QPushButton))
    assert window.refresh_timer.isActive()
    assert window.learning_page.live_timer.isActive()
    window.close()
    app.processEvents()
    assert not window.refresh_timer.isActive()
    assert not window.notification_timer.isActive()
    assert not window.learning_page.live_timer.isActive()
    assert window.learning_page._closing is True


def test_reference_edit_is_available_from_rows_without_duplicate_button() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    app.processEvents()

    reference_buttons = window.learning_page.reference_tabs.findChildren(QPushButton)
    assert all(button.text() != "Изменить" for button in reference_buttons)
    assert all(
        "Дважды щёлкните" in table.toolTip()
        for table in window.learning_page.reference_tables.values()
    )
    window.close()
    app.processEvents()


def test_spinbox_arrows_use_the_shared_vertical_control_style() -> None:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(build_stylesheet())
    dialog = ReferenceDialog("rooms", {"name": "Кабинет №1", "capacity": 12})
    dialog.show()
    app.processEvents()
    option = QStyleOptionSpinBox()
    dialog.capacity.initStyleOption(option)
    up = dialog.capacity.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox,
        option,
        QStyle.SubControl.SC_SpinBoxUp,
        dialog.capacity,
    )
    down = dialog.capacity.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox,
        option,
        QStyle.SubControl.SC_SpinBoxDown,
        dialog.capacity,
    )

    assert up.x() == down.x()
    assert up.width() == down.width()
    assert 28 <= up.width() <= 29
    assert up.top() < down.top()
    assert "%s" not in app.styleSheet()
    assert "QCheckBox::indicator:checked:disabled" in app.styleSheet()
    dialog.close()
    app.processEvents()


def test_early_leave_reason_dialog_is_large_and_validates_text() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ReasonDialog("Ученик покинул занятие", "Укажите причину:")
    ok_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert dialog.minimumWidth() >= 520
    assert dialog.minimumHeight() >= 240
    assert dialog.editor.minimumHeight() >= 110
    assert not ok_button.isEnabled()
    assert dialog.error.isHidden()

    dialog.editor.setPlainText("живот болит")
    app.processEvents()

    assert ok_button.isEnabled()
    assert dialog.reason() == "живот болит"
    dialog.deleteLater()
    app.processEvents()


def test_archive_action_buttons_are_not_clipped() -> None:
    app = QApplication.instance() or QApplication([])
    actions = MainWindow._actions(
        [("Восстановить", "secondary", lambda: None), ("Удалить", "danger", lambda: None)]
    )
    buttons = actions.findChildren(QPushButton)

    assert [button.text() for button in buttons] == ["Восстановить", "Удалить"]
    assert all(
        button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 40
        for button in buttons
    )
    actions.deleteLater()
    app.processEvents()


def test_person_action_buttons_fit_actions_column_without_duplicate_learning_action() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    window.people = [
        {
            "id": 1,
            "full_name": "Алексеев Александр",
            "phone": "+79000000001",
            "roles": ["student"],
            "active": True,
        }
    ]
    window._render_people()
    actions = window.people_tables["student"].cellWidget(0, 3)
    layout = actions.layout()
    buttons = actions.findChildren(QPushButton)
    occupied_width = (
        sum(button.width() for button in buttons)
        + layout.spacing() * (len(buttons) - 1)
        + layout.contentsMargins().left()
        + layout.contentsMargins().right()
    )

    assert occupied_width <= 330
    assert [button.text() for button in buttons] == ["В архив"]
    assert all(button.text() != "Обучение" for button in buttons)
    window.close()
    app.processEvents()


def test_notification_center_deduplicates_and_uses_non_overlapping_footer(
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    notifications = [
        {
            "id": notification_id,
            "kind": "lesson_starts_soon",
            "title": "Занятие через 10 минут",
            "message": "Русский язык\nПрибыли 1 из 3",
            "lesson_id": 77,
            "created_at": "2026-09-29T10:50:00+05:00",
            "read_at": None,
        }
        for notification_id in (1, 2)
    ]
    assert len(window._deduplicate_notifications(notifications)) == 1
    monkeypatch.setattr(QDialog, "exec", lambda _dialog: QDialog.DialogCode.Rejected)

    window._show_notification_center(notifications)
    dialog = next(
        child
        for child in window.findChildren(QDialog)
        if child.windowTitle() == "Центр уведомлений"
    )
    table = dialog.findChild(QTableWidget)
    buttons = dialog.findChildren(QPushButton)

    assert table.rowCount() == 1
    assert {button.text() for button in buttons} >= {
        "Открыть занятие",
        "Прочитать",
        "Прочитать все",
        "Закрыть",
    }
    assert all(button.text() != "Отметить прочитанным" for button in buttons)
    dialog.deleteLater()
    window.close()
    app.processEvents()


def test_today_table_shows_current_participant_count() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page._today_loaded(
        {
            "alerts": [],
            "present": [],
            "lessons": [
                {
                    "id": 1,
                    "start_at": "2026-09-29T16:00:00+05:00",
                    "end_at": "2026-09-29T17:00:00+05:00",
                    "subject_name_snapshot": "Русский язык",
                    "teacher_name_snapshot": "Рябова Галина Викторовна",
                    "room_name_snapshot": "Кабинет №1",
                    "participants": [
                        {"person_id": 1, "attendance_status": "present"},
                        {"person_id": 2, "attendance_status": "absent"},
                        {"person_id": 3, "attendance_status": "late"},
                    ],
                    "status": "planned",
                }
            ],
        }
    )

    assert page.today_lessons.horizontalHeaderItem(4).text() == "Участники"
    assert page.today_lessons.item(0, 4).text() == "3"
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_today_action_buttons_keep_only_operational_actions() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page._today_loaded(
        {
            "alerts": [],
            "present": [],
            "lessons": [
                {
                    "id": 1,
                    "start_at": "2099-09-30T10:30:00+05:00",
                    "end_at": "2099-09-30T11:30:00+05:00",
                    "subject_name_snapshot": "Информатика",
                    "teacher_name_snapshot": "Быков Валерий Андреевич",
                    "room_name_snapshot": "Кабинет №1",
                    "participants": [],
                    "status": "planned",
                }
            ],
        }
    )

    action_cell = page.today_lessons.cellWidget(0, 6)
    buttons = action_cell.findChildren(QPushButton)
    assert page.today_lessons.columnWidth(6) == 120
    assert [button.text() for button in buttons] == ["Начать"]
    assert all(button.property("density") == "compact" for button in buttons)
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_today_row_double_click_opens_one_card_for_planned_and_completed(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    app.processEvents()
    planned = {
        "id": 1,
        "start_at": "2099-09-30T10:30:00+05:00",
        "end_at": "2099-09-30T11:30:00+05:00",
        "subject_name_snapshot": "Информатика",
        "teacher_name_snapshot": "Быков Валерий Андреевич",
        "room_name_snapshot": "Кабинет №1",
        "participants": [],
        "status": "planned",
        "confirmation": {"state": "yellow", "rows": []},
    }
    completed = {**planned, "id": 2, "status": "completed"}
    page._today_loaded(
        {"alerts": [], "present": [], "lessons": [planned, completed]}
    )
    opened: list[int] = []
    monkeypatch.setattr(page, "open_lesson", lambda lesson: opened.append(lesson["id"]))

    page.today_lessons.cellDoubleClicked.emit(0, 0)
    page.today_lessons.cellDoubleClicked.emit(1, 0)

    assert opened == [1, 2]
    assert page.today_lessons.cellWidget(1, 6) is None
    page.deleteLater()
    app.processEvents()


def test_opening_planned_lesson_without_confirmations_loads_full_card(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    lesson = {
        "id": 41,
        "start_at": "2099-09-30T10:30:00+05:00",
        "end_at": "2099-09-30T11:30:00+05:00",
        "status": "planned",
    }
    page._today_loaded({"alerts": [], "present": [], "lessons": [lesson]})
    calls: list[tuple[object, tuple[object, ...], object]] = []
    monkeypatch.setattr(
        page,
        "_run",
        lambda fn, *args, done=None, **_kwargs: calls.append((fn, args, done)),
    )

    page._open_today_lesson(0)

    assert len(calls) == 1
    assert calls[0][0] == page.api.learning_lesson
    assert calls[0][1] == (41,)
    assert callable(calls[0][2])
    page.deleteLater()
    app.processEvents()


def test_planned_lesson_card_contains_edit_action() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonCardDialog(
        {
            "id": 1,
            "start_at": "2099-09-30T10:30:00+05:00",
            "end_at": "2099-09-30T11:30:00+05:00",
            "subject_name_snapshot": "Информатика",
            "teacher_name_snapshot": "Быков Валерий Андреевич",
            "room_name_snapshot": "Кабинет №1",
            "participants": [],
            "status": "planned",
        }
    )
    edit_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Изменить занятие"
    )

    edit_button.click()

    assert dialog.operation == "edit"
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()
    app.processEvents()


def test_planned_lesson_card_shows_confirmations_and_admin_override_action() -> None:
    app = QApplication.instance() or QApplication([])
    response = {
        "recipient_context": "student",
        "recipient_person_id": 7,
        "recipient_name": "Куц Олег Олегович",
        "subject_person_id": 7,
        "subject_name": "Куц Олег Олегович",
        "max_available": False,
        "answer": None,
        "reason": None,
    }
    dialog = LessonCardDialog(
        {
            "id": 1,
            "start_at": "2099-09-30T10:30:00+05:00",
            "end_at": "2099-09-30T11:30:00+05:00",
            "subject_name_snapshot": "Информатика",
            "teacher_name_snapshot": "Быков Валерий Андреевич",
            "room_name_snapshot": "Кабинет №1",
            "participants": [],
            "status": "planned",
            "confirmation": {
                "state": "yellow",
                "label": "Ожидаются подтверждения",
                "rows": [response],
            },
        }
    )
    assert dialog.confirmation_table is not None
    assert dialog.confirmation_table.item(0, 3).text() == "Недоступен"
    overall_marker = dialog.findChild(QFrame, "overallConfirmationMarker")
    assert overall_marker is not None
    assert "#f59e0b" in overall_marker.styleSheet()
    answer = dialog.confirmation_table.item(0, 4)
    assert answer.text() == ""
    assert answer.icon().pixmap(12, 12).toImage().pixelColor(6, 6).name() == "#f59e0b"
    assert answer.toolTip() == "Ожидается подтверждение"
    assert not any(
        label.text().startswith("Подтверждения:")
        for label in dialog.findChildren(QLabel)
    )
    assert any(
        button.text() == "Повторить запрос"
        for button in dialog.findChildren(QPushButton)
    )
    confirmation_panel = dialog.findChild(QFrame, "confirmationActionsPanel")
    lesson_panel = dialog.findChild(QFrame, "lessonActionsPanel")
    assert confirmation_panel is not None
    assert lesson_panel is not None
    assert {
        button.text() for button in confirmation_panel.findChildren(QPushButton)
    } == {
        "Подтвердить администратором",
        "Повторить запрос",
        "Исключить ученика",
    }
    assert {button.text() for button in lesson_panel.findChildren(QPushButton)} == {
        "Изменить занятие",
        "Перенести",
    }
    resend_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Повторить запрос"
    )
    confirm_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Подтвердить администратором"
    )
    assert not resend_button.isEnabled()
    assert not confirm_button.isEnabled()
    dialog.confirmation_table.selectRow(0)
    app.processEvents()
    assert not resend_button.isEnabled()
    assert "MAX" in resend_button.toolTip()
    assert confirm_button.isEnabled()
    confirm_button.click()
    assert dialog.operation == "confirm_by_admin"
    assert dialog.selected_confirmation() == response
    dialog.deleteLater()
    app.processEvents()


def test_planned_lesson_card_builds_selectable_waiting_rows_without_confirmation_payload() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonCardDialog(
        {
            "id": 2,
            "start_at": "2099-09-30T10:30:00+05:00",
            "end_at": "2099-09-30T11:30:00+05:00",
            "subject_name_snapshot": "Русский язык",
            "teacher_id": 5,
            "teacher_name_snapshot": "Рябова Галина Викторовна",
            "room_name_snapshot": "Кабинет №1",
            "participants": [
                {"person_id": 7, "person_name_snapshot": "Куц Олег Олегович"},
                {"person_id": 8, "person_name_snapshot": "Пупкин Иван Пупкович"},
            ],
            "status": "planned",
        }
    )

    assert dialog.confirmation_table is not None
    assert dialog.confirmation_table.rowCount() == 3
    assert dialog.confirmation_table.item(1, 3).text() == "Проверяется"
    for row in range(dialog.confirmation_table.rowCount()):
        answer = dialog.confirmation_table.item(row, 4)
        assert answer.text() == ""
        assert answer.icon().pixmap(12, 12).toImage().pixelColor(6, 6).name() == "#f59e0b"

    dialog.confirmation_table.selectRow(1)
    app.processEvents()
    resend_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Повторить запрос"
    )
    assert not resend_button.isEnabled()
    assert "MAX" in resend_button.toolTip()
    confirm_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Подтвердить администратором"
    )
    confirm_button.click()
    assert dialog.operation == "confirm_by_admin"
    assert dialog.selected_confirmation()["subject_person_id"] == 7
    dialog.deleteLater()
    app.processEvents()


def test_resend_confirmation_is_enabled_for_selected_max_recipient() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonCardDialog(
        {
            "id": 3,
            "start_at": "2099-09-30T10:30:00+05:00",
            "end_at": "2099-09-30T11:30:00+05:00",
            "subject_name_snapshot": "Информатика",
            "teacher_name_snapshot": "Учитель",
            "room_name_snapshot": "Кабинет №1",
            "participants": [],
            "status": "planned",
            "confirmation": {
                "state": "yellow",
                "rows": [
                    {
                        "recipient_context": "student",
                        "recipient_person_id": 7,
                        "recipient_name": "Куц Олег Олегович",
                        "subject_person_id": 7,
                        "subject_name": "Куц Олег Олегович",
                        "max_available": True,
                        "answer": None,
                        "reason": None,
                    }
                ],
            },
        }
    )
    resend_button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Повторить запрос"
    )
    assert not resend_button.isEnabled()
    dialog.confirmation_table.selectRow(0)
    app.processEvents()
    assert resend_button.isEnabled()
    resend_button.click()
    assert dialog.operation == "resend_confirmation"
    dialog.deleteLater()
    app.processEvents()


def test_confirmation_colors_are_visible_in_today_and_calendar_tables() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    lessons = []
    expected = {
        "red": ("#dc2626", "Есть отказ"),
        "yellow": ("#f59e0b", "Ожидаются подтверждения"),
        "green": ("#16a34a", "Все подтвердили"),
    }
    for index, (state, (_color, label)) in enumerate(expected.items(), start=1):
        lessons.append(
            {
                "id": index,
                "start_at": f"2026-10-04T{9 + index:02d}:00:00+05:00",
                "end_at": f"2026-10-04T{10 + index:02d}:00:00+05:00",
                "subject_name_snapshot": "Информатика",
                "teacher_name_snapshot": "Учитель",
                "room_name_snapshot": "Кабинет",
                "participants": [],
                "status": "planned",
                "confirmation": {"state": state, "label": label, "rows": []},
            }
        )
    page._today_loaded({"lessons": lessons, "present": [], "alerts": []})
    page._calendar_loaded(lessons)

    for row, (_state, (color, label)) in enumerate(expected.items()):
        for table in (page.today_lessons, page.calendar_table):
            item = table.item(row, 5)
            marker = item.icon().pixmap(12, 12).toImage()
            assert marker.pixelColor(6, 6).name() == color
            assert item.text() == "Запланировано"
            assert item.toolTip() == label
    page.deleteLater()
    app.processEvents()


def test_planned_lesson_without_confirmation_payload_is_shown_as_waiting() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    lesson = {
        "id": 17,
        "start_at": "2099-10-04T10:30:00+05:00",
        "end_at": "2099-10-04T11:30:00+05:00",
        "subject_name_snapshot": "Информатика",
        "teacher_name_snapshot": "Учитель",
        "room_name_snapshot": "Кабинет №1",
        "participants": [],
        "status": "planned",
    }

    page._today_loaded({"lessons": [lesson], "present": [], "alerts": []})
    page._calendar_loaded([lesson])

    for table in (page.today_lessons, page.calendar_table):
        item = table.item(0, 5)
        marker = item.icon().pixmap(12, 12).toImage()
        assert marker.pixelColor(6, 6).name() == "#f59e0b"
        assert item.text() == "Запланировано"
        assert item.toolTip() == "Ожидаются подтверждения"
    page.deleteLater()
    app.processEvents()


def test_lesson_editor_shows_participant_confirmation_and_resend_feedback() -> None:
    app = QApplication.instance() or QApplication([])
    target = {
        "recipient_context": "student",
        "recipient_person_id": 7,
        "subject_person_id": 7,
        "answer": None,
        "max_available": True,
    }
    dialog = LessonDialog(
        {
            "subjects": [],
            "teachers": [],
            "rooms": [],
            "groups": [],
            "students": [{"id": 7, "full_name": "Куц Олег Олегович"}],
        },
        {
            "participants": [{"person_id": 7}],
            "confirmation": {"state": "yellow", "rows": [target]},
            "notes": None,
        },
    )
    emitted: list[list[dict[str, object]]] = []
    dialog.resend_confirmation_requested.connect(lambda rows: emitted.append(list(rows)))
    holder = dialog.students.cellWidget(0, 2)
    assert holder is not None
    confirmation_cell = dialog.students.item(0, 2)
    assert confirmation_cell.text() == ""
    assert confirmation_cell.toolTip() == "Ожидается"
    marker = holder.findChild(QFrame, "confirmationMarker")
    assert marker is not None
    assert "#f59e0b" in marker.styleSheet()
    assert not any(label.text() == "Ожидается" for label in holder.findChildren(QLabel))
    resend = next(
        button for button in holder.findChildren(QPushButton) if button.text() == "Повторить"
    )
    resend.click()

    assert emitted == [[target]]
    assert not resend.isEnabled()
    assert resend.text() == "Отправлено"
    dialog.deleteLater()
    app.processEvents()


def test_selected_participant_without_confirmation_data_gets_yellow_marker() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonDialog(
        {
            "subjects": [],
            "teachers": [],
            "rooms": [],
            "groups": [],
            "students": [{"id": 7, "full_name": "Куц Олег Олегович"}],
        },
        {"participants": [{"person_id": 7}], "notes": None},
    )

    holder = dialog.students.cellWidget(0, 2)
    assert holder is not None
    marker = holder.findChild(QFrame, "confirmationMarker")
    assert marker is not None
    assert "#f59e0b" in marker.styleSheet()
    assert dialog.students.item(0, 2).text() == ""
    dialog.deleteLater()
    app.processEvents()


def test_student_journal_loads_full_lesson_before_opening_card(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    page._student_history_loaded(
        {
            "lessons": [
                {
                    "id": 27,
                    "start_at": "2026-09-29T16:00:00+05:00",
                    "end_at": "2026-09-29T17:00:00+05:00",
                    "subject_name_snapshot": "Русский язык",
                    "teacher_name_snapshot": "Учитель",
                    "attendance_status": "present",
                }
            ]
        }
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        page,
        "_run",
        lambda fn, *args, **kwargs: calls.append((fn, *args)),
    )
    page.student_journal.selectRow(0)
    page._open_student_journal_lesson()

    assert calls == [(page.api.learning_lesson, 27)]
    page.deleteLater()
    app.processEvents()


def test_calendar_can_remove_selected_planned_lesson(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    app.processEvents()
    # Stop queued initial refresh callbacks before loading the fixture data.
    # Their ordering differs between the Windows and Linux Qt event loops.
    page.shutdown()
    app.processEvents()
    lesson = {
        "id": 17,
        "start_at": "2026-09-30T10:30:00+05:00",
        "subject_name_snapshot": "Информатика",
        "teacher_name_snapshot": "Быков Валерий Андреевич",
        "room_name_snapshot": "Кабинет №1",
        "participants": [],
        "status": "planned",
    }
    page._calendar_loaded([lesson, {**lesson, "id": 18, "status": "cancelled"}])
    page.calendar_table.selectRow(0)
    app.processEvents()
    assert page.calendar_table.rowCount() == 1
    assert page.delete_calendar_button.isEnabled()

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(page, "_confirm_lesson_deletion", lambda _lesson: True)
    monkeypatch.setattr(
        page,
        "_run",
        lambda _fn, *args, **_kwargs: calls.append(args),
    )
    page._delete_calendar_selected()

    assert calls == [(17, "cancel", {"reason": "Удалено администратором"})]
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_overdue_calendar_lesson_reconciles_only_by_double_click(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    app.processEvents()
    lesson = {
        "id": 19,
        "start_at": "2020-09-30T10:30:00+05:00",
        "end_at": "2020-09-30T11:30:00+05:00",
        "subject_name_snapshot": "Информатика",
        "teacher_name_snapshot": "Быков Валерий Андреевич",
        "room_name_snapshot": "Кабинет №1",
        "participants": [],
        "status": "planned",
    }
    page._calendar_loaded([lesson])
    page.calendar_table.selectRow(0)
    app.processEvents()

    assert page.calendar_table.item(0, 5).text() == "Требует уточнения"
    assert all(
        button.text() != "Уточнить статус" for button in page.findChildren(QPushButton)
    )
    assert not page.delete_calendar_button.isEnabled()

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        page,
        "_choose_reconciliation",
        lambda _lesson: {
            "outcome": "held",
            "reason": "Администратор внёс отметку после занятия",
        },
    )
    monkeypatch.setattr(
        page,
        "_run",
        lambda _fn, *args, **_kwargs: calls.append(args[1]),
    )
    page._edit_calendar_selected()

    assert calls == [
        {
            "outcome": "held",
            "reason": "Администратор внёс отметку после занятия",
        }
    ]
    page.deleteLater()
    app.processEvents()


def test_completed_lesson_presence_uses_actual_times_without_reason_prompt(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    app.processEvents()

    class AcceptedLessonCard:
        operation = None

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def exec(self) -> int:
            return 1

        def attendance(self) -> list[tuple[int, str]]:
            return [(44, "present")]

        def actual_time_change(self):
            return None

    calls: list[tuple[int, int, dict[str, object]]] = []
    monkeypatch.setattr(learning_page_module, "LessonCardDialog", AcceptedLessonCard)
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Для обычного присутствия причина не запрашивается")
        ),
    )
    monkeypatch.setattr(
        page.api,
        "correct_attendance",
        lambda lesson_id, person_id, payload: calls.append(
            (lesson_id, person_id, payload)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        page,
        "_run",
        lambda function, *args, **_kwargs: function(*args),
    )
    page.open_lesson(
        {
            "id": 18,
            "status": "completed",
            "start_at": "2026-10-03T12:00:00+05:00",
            "end_at": "2026-10-03T13:00:00+05:00",
            "actual_start_at": "2026-10-03T12:00:00+05:00",
            "actual_end_at": "2026-10-03T13:00:00+05:00",
            "subject_name_snapshot": "Информатика",
            "participants": [
                {
                    "person_id": 44,
                    "person_name_snapshot": "Куц Олег Олегович",
                    "attendance_status": "expected",
                    "arrived_at": None,
                    "left_at": None,
                }
            ],
        }
    )

    assert calls == [
        (
            18,
            44,
            {
                "attendance_status": "present",
                "arrived_at": "2026-10-03T12:00:00+05:00",
                "left_at": "2026-10-03T13:00:00+05:00",
                "reason": "Посещаемость внесена задним числом",
            },
        )
    ]
    page.deleteLater()
    app.processEvents()


def test_reference_data_refreshes_group_memberships_for_immediate_preview(monkeypatch) -> None:
    api = ManagementApi("http://127.0.0.1:1")
    calls: list[str] = []

    def request(_method: str, path: str, **_kwargs):
        calls.append(path)
        if path == "/learning/reference-data":
            return {"groups": [{"id": 15, "name": "Группа №15"}]}
        if path == "/learning/groups/15/memberships":
            return [
                {
                    "id": 1,
                    "person_id": 22,
                    "start_at": "2026-09-29T10:00:00+05:00",
                    "end_at": None,
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr(api, "_request", request)
    data = api.learning_reference_data()

    assert calls == ["/learning/reference-data", "/learning/groups/15/memberships"]
    assert data["groups"][0]["memberships"][0]["person_id"] == 22
    api.close()


def test_lesson_dialog_gives_participant_list_more_space() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonDialog(
        {"subjects": [], "teachers": [], "rooms": [], "groups": [], "students": []}
    )

    assert dialog.minimumWidth() >= 800
    assert dialog.height() >= 840
    assert dialog.students.minimumHeight() >= 290
    dialog.deleteLater()
    app.processEvents()


def test_dense_schedule_is_paginated_and_html_escaped() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    app.processEvents()

    start = datetime(2026, 9, 28, 8, tzinfo=UTC)
    window.learning_page.calendar_date.setDate(QDate(2026, 9, 28))
    window.learning_page.calendar_end.setDate(QDate(2026, 11, 30))
    window.learning_page.calendar_lessons = [
        {
            "start_at": (start + timedelta(hours=20 * index)).isoformat(),
            "end_at": (start + timedelta(hours=20 * index, minutes=60)).isoformat(),
            "subject_name_snapshot": "Программирование & робототехника <углублённый курс>",
            "teacher_name_snapshot": "Очень Длинное Имя Преподавателя Для Проверки Макета",
            "room_name_snapshot": f"Кабинет {index % 5 + 1}",
            "participants": [
                {
                    "person_name_snapshot": f"Ученик {index} Александрович",
                    "attendance_status": "expected",
                }
            ],
        }
        for index in range(80)
    ]
    document = window.learning_page._schedule_document()
    document.setPageSize(QSizeF(842, 595))

    assert document.pageCount() > 1
    assert "&lt;углублённый курс&gt;" in document.toHtml()
    assert "Кабинет" not in document.toPlainText()
    window.close()
    app.processEvents()


def test_schedule_preview_is_landscape_calendar_with_students(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    app.processEvents()
    page.calendar_date.setDate(QDate(2026, 9, 28))
    page.calendar_end.setDate(QDate(2026, 10, 4))
    page.calendar_lessons = [
        {
            "start_at": "2026-09-30T10:30:00+05:00",
            "end_at": "2026-09-30T11:30:00+05:00",
            "subject_name_snapshot": "Информатика",
            "teacher_name_snapshot": "Быков Валерий Андреевич",
            "room_name_snapshot": "Кабинет №1",
            "status": "planned",
            "participants": [
                {
                    "person_name_snapshot": "Алексеев Александр Фёдорович",
                    "attendance_status": "expected",
                },
                {
                    "person_name_snapshot": "Белов Степан Алексеевич",
                    "attendance_status": "excused",
                },
            ],
        }
    ]
    document = page._schedule_document()
    plain = document.toPlainText()
    printer = QPdfWriter(str(tmp_path / "preview-layout.pdf"))
    page._prepare_schedule_printer(printer)

    assert printer.pageLayout().orientation() == QPageLayout.Orientation.Landscape
    assert "Среда" in plain
    assert "Понедельник" not in plain
    assert "Воскресенье" not in plain
    assert "Занятий нет" not in plain
    assert "Алексеев А. Ф." in plain
    assert "Белов" not in plain
    assert "Кабинет №1" not in plain

    assert SchedulePreviewDialog._normalize_zoom_percent(150) == 150
    assert SchedulePreviewDialog._normalize_zoom_percent(10) == 25
    assert SchedulePreviewDialog._normalize_zoom_percent(500) == 400
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_busy_day_uses_parallel_columns_before_adding_pages() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    app.processEvents()
    page.calendar_date.setDate(QDate(2026, 9, 30))
    page.calendar_end.setDate(QDate(2026, 9, 30))
    page.calendar_lessons = [
        {
            "start_at": f"2026-09-30T{8 + index:02d}:00:00+05:00",
            "end_at": f"2026-09-30T{9 + index:02d}:00:00+05:00",
            "subject_name_snapshot": "Информатика" if index % 2 == 0 else "Русский язык",
            "teacher_name_snapshot": "Быков Валерий Андреевич",
            "status": "planned",
            "participants": [
                {
                    "person_name_snapshot": f"Ученик {student} Александрович",
                    "attendance_status": "expected",
                }
                for student in range(4)
            ],
        }
        for index in range(13)
    ]

    document = page._schedule_document()
    document.setPageSize(QSizeF(842, 595))
    plain = document.toPlainText()

    assert document.pageCount() == 1
    assert plain.count("Среда") >= 2
    assert "часть 1 из" in plain
    assert "Занятий нет" not in plain
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_schedule_document_exports_to_nonempty_landscape_pdf(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    app.processEvents()
    output = tmp_path / "schedule.pdf"
    printer = QPdfWriter(str(output))
    page._prepare_schedule_printer(printer)

    page._schedule_document().print_(printer)
    del printer

    assert output.exists()
    assert output.stat().st_size > 500
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_searchable_combo_matches_prefix_of_surname_or_name() -> None:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(build_stylesheet())
    combo = SearchableComboBox()
    combo.addItems(
        [
            "Алексеев Александр Фёдорович",
            "Белов Александр Сергеевич",
            "Смирнова Мария Алексеевна",
        ]
    )

    combo._search("ал фё")
    assert combo._proxy.rowCount() == 1
    assert combo._proxy.index(0, 0).data() == "Алексеев Александр Фёдорович"

    combo._search("мар")
    assert combo._proxy.rowCount() == 1
    assert combo._proxy.index(0, 0).data() == "Смирнова Мария Алексеевна"
    assert combo.completer().popup().objectName() == "searchCompleterPopup"
    assert "QAbstractItemView#searchCompleterPopup" in app.styleSheet()
    combo.deleteLater()

    data_combo = SearchableComboBox()
    data_combo.addItem("Не выбран", None)
    data_combo.addItem("Быков Валерий Андреевич", 42)
    data_combo.setEditText("Быков Валерий Андреевич")
    assert data_combo.currentData() == 42
    data_combo.deleteLater()
    app.processEvents()


def test_closed_combo_does_not_change_value_with_mouse_wheel() -> None:
    app = QApplication.instance() or QApplication([])
    combo = SafeComboBox()
    combo.addItems(["Первый", "Второй", "Третий"])
    combo.setCurrentIndex(1)
    combo.show()
    app.processEvents()

    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(combo, event)

    assert combo.currentIndex() == 1
    assert not event.isAccepted()
    combo.deleteLater()
    app.processEvents()


def test_group_teacher_typed_from_search_is_saved_as_identifier() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ReferenceDialog(
        "groups",
        {"name": "Группа №15"},
        references={
            "subjects": [{"id": 1, "name": "Информатика"}],
            "teachers": [{"id": 42, "full_name": "Быков Валерий Андреевич"}],
        },
    )
    dialog.group_subject.setEditText("Информатика")
    dialog.group_teacher.setEditText("Быков Валерий Андреевич")

    payload = dialog.payload()

    assert payload["subject_id"] == 1
    assert payload["default_teacher_id"] == 42
    dialog.deleteLater()
    app.processEvents()


def test_subject_assignments_filter_teachers_in_group_and_lesson() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [
            {"id": 1, "name": "Информатика", "teacher_ids": [10]},
            {"id": 2, "name": "Математика", "teacher_ids": [11]},
        ],
        "teachers": [
            {
                "id": 10,
                "full_name": "Быков Валерий Андреевич",
                "active": False,
            },
            {"id": 11, "full_name": "Иванова Мария Сергеевна"},
        ],
        "rooms": [{"id": 20, "name": "Кабинет №1"}],
        "groups": [],
        "students": [],
    }
    lesson = LessonDialog(references)
    assert lesson.students.editTriggers() == QTableWidget.EditTrigger.NoEditTriggers
    assert lesson.teacher.findData(10) >= 0
    assert lesson.teacher.findData(11) == -1
    lesson.subject.setCurrentIndex(lesson.subject.findData(2))
    app.processEvents()
    assert lesson.teacher.findData(10) == -1
    assert lesson.teacher.findData(11) >= 0

    free_slot = FreeSlotDialog(references)
    assert free_slot.teacher.findData(10) >= 0

    group = ReferenceDialog("groups", {"name": "Группа"}, references=references)
    group.group_subject.setCurrentIndex(group.group_subject.findData(1))
    app.processEvents()
    assert group.group_teacher.findData(10) >= 0
    assert group.group_teacher.findData(11) == -1
    lesson.deleteLater()
    free_slot.deleteLater()
    group.deleteLater()
    app.processEvents()


def test_subject_table_uses_visible_color_swatch_and_reports_old_server() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page._references_loaded(
        {
            "subjects": [{"id": 1, "name": "Информатика", "color": "#c00000", "active": True}],
            "rooms": [],
            "groups": [],
            "students": [],
            "teachers": [],
        }
    )
    table = page.reference_tables["subjects"]

    assert table.cellWidget(0, 1) is not None
    assert table.item(0, 2).text() == "Требуется обновление сервера"

    try:
        ManagementApi._verify_subject_assignments(
            "subjects", {"teacher_ids": [10]}, {"id": 1, "name": "Информатика"}
        )
    except ApiError as exc:
        assert "не поддерживает" in str(exc)
    else:
        raise AssertionError("Старый сервер должен быть обнаружен")
    page.deleteLater()
    app.processEvents()


def test_subject_color_marks_lessons_in_today_and_calendar() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.shutdown()
    app.processEvents()
    page.references["subjects"] = [
        {"id": 7, "name": "Информатика", "color": "#c00000"}
    ]
    lesson = {
        "id": 18,
        "subject_id": 7,
        "start_at": "2099-10-03T12:00:00+05:00",
        "end_at": "2099-10-03T13:00:00+05:00",
        "subject_name_snapshot": "Информатика",
        "teacher_name_snapshot": "Быков Валерий Андреевич",
        "room_name_snapshot": "Кабинет №1",
        "participants": [],
        "status": "planned",
    }

    page._calendar_loaded([lesson])
    page._today_loaded({"lessons": [lesson], "present": [], "alerts": []})

    for item in (page.calendar_table.item(0, 1), page.today_lessons.item(0, 1)):
        assert not item.icon().isNull()
        assert item.icon().pixmap(12, 12).toImage().pixelColor(6, 6).name() == "#c00000"
        assert item.toolTip() == "Цвет предмета «Информатика»"
    page.deleteLater()
    app.processEvents()


def test_subject_dialog_uses_palette_button_and_saves_teacher_ids() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ReferenceDialog(
        "subjects",
        {
            "name": "Информатика",
            "color": "#2563eb",
            "teacher_ids": [10],
        },
        references={
            "teachers": [
                {"id": 10, "full_name": "Быков Валерий Андреевич"},
                {"id": 11, "full_name": "Иванова Мария Сергеевна"},
            ]
        },
    )

    payload = dialog.payload()

    assert dialog.color_button.text() == "Выбрать цвет"
    assert payload["color"] == "#2563eb"
    assert payload["teacher_ids"] == [10]
    dialog.deleteLater()
    app.processEvents()


def test_departure_uses_selected_present_row_instead_of_arrival_field(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    app.processEvents()
    page = window.learning_page
    page.presence_person.addItem("Алексеев Александр Фёдорович", 55)
    page._today_loaded(
        {
            "lessons": [],
            "alerts": [],
            "present": [
                {
                    "person_id": 77,
                    "person_name": "Пупкин Иван Пупкович",
                    "arrived_at": datetime.now(UTC).isoformat(),
                }
            ],
        }
    )
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        page.api,
        "presence_action",
        lambda person_id, action: calls.append((person_id, action)) or {},
        raising=False,
    )
    monkeypatch.setattr(
        page,
        "_run",
        lambda function, *args, **_kwargs: function(*args),
    )
    page.present_table.selectRow(0)

    page.presence("departure")

    assert calls == [(77, "departure")]
    window.close()
    app.processEvents()


def test_stale_presence_is_visible_and_uses_time_correction_flow(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    app.processEvents()
    page = window.learning_page
    arrived_at = "2026-10-03T15:27:00+00:00"
    page._today_loaded(
        {
            "lessons": [],
            "alerts": [],
            "present": [],
            "stale_presence": [
                {
                    "person_id": 77,
                    "person_name": "Пупкин Иван Пупкович",
                    "arrived_at": arrived_at,
                }
            ],
        }
    )

    assert page.present_table.rowCount() == 1
    assert page.present_table.item(0, 0).data(Qt.ItemDataRole.UserRole + 1) is True
    assert "03.10" in page.present_table.item(0, 1).text()
    assert "не закрыто" in page.present_table.item(0, 1).text()
    corrections: list[tuple[int, str]] = []
    monkeypatch.setattr(
        page,
        "_correct_stale_departure",
        lambda person_id, arrived: corrections.append((person_id, arrived)),
    )
    page.present_table.selectRow(0)

    page.presence("departure")

    assert corrections == [(77, arrived_at)]
    window.close()
    app.processEvents()


def test_double_clicking_present_person_immediately_marks_departure(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    assert window.pool.waitForDone(3_000)
    app.processEvents()
    page = window.learning_page
    page.presence_person.addItem("Пупкин Иван Пупкович", 77)
    page._today_loaded(
        {
            "lessons": [],
            "alerts": [],
            "present": [
                {
                    "person_id": 77,
                    "person_name": "Пупкин Иван Пупкович",
                    "arrived_at": datetime.now(UTC).isoformat(),
                }
            ],
        }
    )
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        page.api,
        "presence_action",
        lambda person_id, action: calls.append((person_id, action)) or {},
        raising=False,
    )
    monkeypatch.setattr(
        page,
        "_run",
        lambda function, *args, **_kwargs: function(*args),
    )

    page._select_present_person(0)

    assert page.presence_person.currentData() == 77
    assert calls == [(77, "departure")]
    window.close()
    app.processEvents()


def test_person_history_is_loaded_only_when_learning_tab_opens() -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def load(person_id: int, _roles: list[str], callback) -> None:
        calls.append(person_id)
        callback({"student": {"lessons": [], "presence": []}})

    dialog = PersonDialog(
        {
            "id": 7,
            "full_name": "Алексеев Александр Фёдорович",
            "phone": "+70010000017",
            "roles": ["student"],
        },
        load_learning_history=load,
    )
    assert calls == []
    dialog.sections.setCurrentIndex(2)
    app.processEvents()
    assert calls == [7]
    dialog.deleteLater()


def test_group_defaults_fill_new_lesson_without_changing_override_support() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [{"id": 1, "name": "Математика"}],
        "teachers": [{"id": 2, "full_name": "Воронцов Борис Александрович"}],
        "rooms": [{"id": 3, "name": "Кабинет 2"}],
        "groups": [
            {
                "id": 4,
                "name": "Группа А",
                "subject_id": 1,
                "default_teacher_id": 2,
                "default_duration_minutes": 90,
            }
        ],
        "students": [],
    }
    dialog = LessonDialog(references)
    dialog.start.setDateTime(QDateTime(QDate(2026, 10, 1), QTime(10, 0)))
    dialog.group.setCurrentIndex(dialog.group.findData(4))
    app.processEvents()

    assert dialog.subject.currentData() == 1
    assert dialog.teacher.currentData() == 2
    assert dialog.duration.value() == 90
    assert dialog.end.dateTime() == dialog.start.dateTime().addSecs(90 * 60)
    dialog.deleteLater()


def test_lesson_dialog_keeps_selected_students_at_top() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [],
        "teachers": [],
        "rooms": [],
        "groups": [],
        "students": [
            {"id": 1, "full_name": "Алексеев Александр"},
            {"id": 2, "full_name": "Белов Борис"},
            {"id": 3, "full_name": "Волкова Алиса"},
        ],
    }
    dialog = LessonDialog(
        references,
        {
            "participants": [{"person_id": 3}],
            "notes": None,
        },
    )

    assert dialog.students.item(0, 1).text() == "Волкова Алиса"
    first_unselected = dialog.students.cellWidget(1, 0).findChild(QCheckBox)
    first_unselected.setChecked(True)
    app.processEvents()

    assert [dialog.students.item(row, 1).text() for row in range(2)] == [
        "Алексеев Александр",
        "Волкова Алиса",
    ]
    assert dialog.students.item(0, 0).text() == ""
    dialog.deleteLater()
    app.processEvents()


def test_group_selection_loads_members_and_keeps_extra_student_available() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [{"id": 1, "name": "Математика"}],
        "teachers": [{"id": 2, "full_name": "Иванов Иван Иванович"}],
        "rooms": [{"id": 3, "name": "Кабинет 2"}],
        "groups": [
            {
                "id": 4,
                "name": "Группа А",
                "subject_id": 1,
                "default_teacher_id": 2,
                "default_duration_minutes": 60,
                "memberships": [
                    {
                        "person_id": 10,
                        "start_at": "2020-01-01T00:00:00",
                        "end_at": None,
                    },
                    {
                        "person_id": 11,
                        "start_at": "2020-01-01T00:00:00+05:00",
                        "end_at": None,
                    },
                ],
            }
        ],
        "students": [
            {"id": 10, "full_name": "Алексеев Александр Фёдорович"},
            {"id": 11, "full_name": "Андреева Милана Олеговна"},
            {"id": 12, "full_name": "Белов Степан Алексеевич"},
        ],
    }
    dialog = LessonDialog(references)
    dialog.group.setCurrentIndex(dialog.group.findData(4))
    app.processEvents()

    group_checks = [
        dialog.students.cellWidget(row, 0).findChild(QCheckBox) for row in range(2)
    ]
    extra = dialog.students.cellWidget(2, 0).findChild(QCheckBox)
    assert all(check.isChecked() and check.isEnabled() for check in group_checks)
    assert dialog.students.horizontalHeaderItem(2).text() == "Подтверждение"
    assert [dialog.students.item(row, 2).text() for row in range(2)] == ["", ""]
    for row in range(2):
        marker = dialog.students.cellWidget(row, 2).findChild(
            QFrame, "confirmationMarker"
        )
        assert marker is not None
        assert "#f59e0b" in marker.styleSheet()
    assert extra.isEnabled() and not extra.isChecked()
    extra.setChecked(True)
    assert dialog.payload()["participant_ids"] == [10, 11, 12]
    assert dialog.participant_count.text() == "Из группы: 2 · исключено: 0 · доп.: 1"
    group_checks[0].setChecked(False)
    payload = dialog.payload()
    assert payload["participant_ids"] == [11, 12]
    assert payload["excluded_participant_ids"] == [10]
    assert dialog.participant_count.text() == "Из группы: 1 · исключено: 1 · доп.: 1"
    dialog.deleteLater()
    app.processEvents()


def test_repeat_count_is_shown_only_for_repeating_lessons() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = LessonDialog(
        {"subjects": [], "teachers": [], "rooms": [], "groups": [], "students": []}
    )

    assert dialog.occurrences.isHidden()
    dialog.repeat.setChecked(True)
    app.processEvents()
    assert not dialog.occurrences.isHidden()
    dialog.deleteLater()
    app.processEvents()


def test_lesson_payload_contains_timezone_and_calculated_end() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [{"id": 1, "name": "Информатика"}],
        "teachers": [{"id": 2, "full_name": "Воронцов Борис Александрович"}],
        "rooms": [{"id": 3, "name": "Кабинет №1"}],
        "groups": [],
        "students": [],
    }
    dialog = LessonDialog(references)
    dialog.end.setDateTime(dialog.start.dateTime().addSecs(75 * 60))
    payload = dialog.payload()
    start = datetime.fromisoformat(payload["start_at"])
    end = datetime.fromisoformat(payload["end_at"])

    assert start.utcoffset() is not None
    assert end.utcoffset() is not None
    assert end - start == timedelta(minutes=75)
    dialog.deleteLater()
    app.processEvents()


def test_lesson_participant_search_keeps_hidden_selections() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [],
        "teachers": [],
        "rooms": [],
        "groups": [],
        "students": [
            {"id": 1, "full_name": "Алексеев Александр Фёдорович"},
            {"id": 2, "full_name": "Белов Степан Алексеевич"},
            {"id": 3, "full_name": "Смирнова Мария Олеговна"},
        ],
    }
    dialog = LessonDialog(references)
    first = dialog.students.cellWidget(0, 0).findChild(QCheckBox)
    first.setChecked(True)
    dialog.student_search.setText("мар")
    app.processEvents()

    assert dialog.students.isRowHidden(0)
    assert dialog.students.isRowHidden(1)
    assert not dialog.students.isRowHidden(2)
    assert first.isChecked()
    assert dialog.participant_count.text() == "Выбрано: 1"
    dialog.deleteLater()
    app.processEvents()


def test_free_slot_student_search_keeps_selected_students() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = FreeSlotDialog(
        {
            "teachers": [],
            "rooms": [],
            "students": [
                {"id": 1, "full_name": "Алексеев Александр Фёдорович"},
                {"id": 2, "full_name": "Смирнова Мария Олеговна"},
            ],
        }
    )
    first = dialog.students.cellWidget(0, 0)
    first.setChecked(True)
    dialog.student_search.setText("мар")
    app.processEvents()

    assert dialog.students.isRowHidden(0)
    assert not dialog.students.isRowHidden(1)
    assert first.isChecked()
    assert dialog.student_count.text() == "Выбрано: 1"
    requested: list[dict] = []
    dialog.search_requested.connect(requested.append)
    dialog._request()
    assert requested
    assert not dialog.search_button.isEnabled()
    assert dialog.search_status.text() == "Идёт поиск свободного времени…"
    dialog.set_slots([])
    assert dialog.search_button.isEnabled()
    assert dialog.search_status.text() == "Подходящих вариантов не найдено."
    dialog.set_slots(
        [
            {
                "start_at": "2026-10-04T10:00:00+05:00",
                "end_at": "2026-10-04T11:00:00+05:00",
                "room_id": 3,
                "room_name": "Кабинет №1",
            }
        ]
    )
    assert dialog.search_status.text() == "Найдено вариантов: 1. Выберите подходящий."
    assert not dialog.create_button.isEnabled()
    dialog.results.selectRow(0)
    assert dialog.create_button.isEnabled()
    dialog.deleteLater()
    app.processEvents()


def test_lesson_error_preserves_form_and_schedules_same_dialog_again() -> None:
    app = QApplication.instance() or QApplication([])
    references = {
        "subjects": [{"id": 1, "name": "Информатика"}],
        "teachers": [{"id": 2, "full_name": "Воронцов Борис Александрович"}],
        "rooms": [{"id": 3, "name": "Кабинет №1"}],
        "groups": [],
        "students": [{"id": 4, "full_name": "Пупкин Иван Пупкович"}],
    }
    dialog = LessonDialog(references)
    dialog.notes.setPlainText("Сохранённая заметка")
    participant = dialog.students.cellWidget(0, 0).findChild(QCheckBox)
    participant.setChecked(True)
    retried: list[LessonDialog] = []

    LearningPage._restore_lesson_dialog(
        dialog,
        "Ошибка соединения",
        lambda: retried.append(dialog),
    )
    app.processEvents()

    assert retried == [dialog]
    assert not dialog.error_label.isHidden()
    assert dialog.notes.toPlainText() == "Сохранённая заметка"
    assert participant.isChecked()
    dialog.deleteLater()
    app.processEvents()


def test_custom_calendar_range_shows_date_and_excludes_excused_from_count() -> None:
    app = QApplication.instance() or QApplication([])
    page = LearningPage(FakeApi())  # type: ignore[arg-type]
    assert page.pool.waitForDone(3_000)
    page.calendar_date.setDate(QDate(2026, 10, 5))
    page.calendar_end.setDate(QDate(2026, 10, 8))
    page._calendar_loaded(
        [
            {
                "start_at": "2026-10-06T14:00:00+05:00",
                "subject_name_snapshot": "Математика",
                "teacher_name_snapshot": "Иванов И.И.",
                "room_name_snapshot": "Кабинет 2",
                "status": "planned",
                "active_participant_count": 6,
                "excused_participant_count": 1,
                "participants": [{}] * 7,
            }
        ]
    )

    assert page.calendar_table.item(0, 0).text().startswith("06.10 ")
    assert page.calendar_table.item(0, 4).text() == "6 участников · отменено: 1"
    page.shutdown()
    page.deleteLater()
    app.processEvents()


def test_multirole_person_history_loads_student_and_teacher_together() -> None:
    app = QApplication.instance() or QApplication([])
    loaded_roles: list[str] = []

    def load(_person_id: int, roles: list[str], callback) -> None:
        loaded_roles.extend(roles)
        callback({"student": {"lessons": [], "presence": []}, "teacher": {"lessons": []}})

    dialog = PersonDialog(
        {
            "id": 12,
            "full_name": "Жуков Георгий Романович",
            "phone": "+79000000012",
            "roles": ["student", "teacher"],
        },
        load_learning_history=load,
    )
    dialog.sections.setCurrentIndex(2)
    app.processEvents()

    assert set(loaded_roles) == {"student", "teacher"}
    dialog.deleteLater()
    app.processEvents()


def test_person_uses_one_phone_for_contact_and_max_authorization() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = PersonDialog(
        {
            "full_name": "Сидорова Мария Ивановна",
            "phone": "+79991112233",
            "max_auth_phone": "+79990000001",
            "roles": ["parent"],
        }
    )

    payload = dialog.payload()

    assert payload["phone"] == "+79991112233"
    assert payload["max_auth_phone"] == "+79991112233"
    assert all(
        label.text() != "Личный телефон для входа в MAX"
        for label in dialog.findChildren(QLabel)
    )
    dialog.deleteLater()
    app.processEvents()


def test_main_client_search_uses_name_prefixes_and_phone_digit_substring() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.pool.waitForDone(3_000)
    window.people = [
        {
            "id": 1,
            "full_name": "Алексеев Александр Фёдорович",
            "phone": "+79001234567",
            "roles": ["student"],
            "active": True,
            "max_user_id": None,
        },
        {
            "id": 2,
            "full_name": "Белов Степан Иванович",
            "phone": "+79007654321",
            "roles": ["student"],
            "active": True,
            "max_user_id": None,
        },
    ]
    window.people_search.setText("ал фё")
    window._render_people()
    assert [item["id"] for item in window.visible_people["student"]] == [1]
    window.people_search.setText("7654")
    window._render_people()
    assert [item["id"] for item in window.visible_people["student"]] == [2]
    window.close()
    app.processEvents()
