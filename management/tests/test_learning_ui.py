from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSizeF
from PySide6.QtWidgets import QApplication, QPushButton

from krit_management.dialogs import PersonDialog
from krit_management.learning_page import LessonDialog
from krit_management.widgets import SearchableComboBox
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


def test_searchable_combo_matches_prefix_of_surname_or_name() -> None:
    app = QApplication.instance() or QApplication([])
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
    combo.deleteLater()
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
    dialog.group.setCurrentIndex(dialog.group.findData(4))
    app.processEvents()

    assert dialog.subject.currentData() == 1
    assert dialog.teacher.currentData() == 2
    assert dialog.start.dateTime().secsTo(dialog.end.dateTime()) == 90 * 60
    dialog.deleteLater()
