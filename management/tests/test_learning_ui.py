from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSizeF
from PySide6.QtWidgets import QApplication, QPushButton

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
    window.close()
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


def test_dense_schedule_is_paginated_and_html_escaped() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeApi())  # type: ignore[arg-type]
    assert window.learning_page.pool.waitForDone(3_000)
    app.processEvents()

    start = datetime(2026, 9, 28, 8, tzinfo=UTC)
    window.learning_page.calendar_lessons = [
        {
            "start_at": (start + timedelta(minutes=45 * index)).isoformat(),
            "subject_name_snapshot": "Программирование & робототехника <углублённый курс>",
            "teacher_name_snapshot": "Очень Длинное Имя Преподавателя Для Проверки Макета",
            "room_name_snapshot": f"Кабинет {index % 5 + 1}",
        }
        for index in range(80)
    ]
    document = window.learning_page._schedule_document()
    document.setPageSize(QSizeF(595, 842))

    assert document.pageCount() > 1
    assert "&lt;углублённый курс&gt;" in document.toHtml()
    window.close()
    app.processEvents()
