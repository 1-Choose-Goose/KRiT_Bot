from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from krit_management.dialogs import ChangePasswordDialog
from krit_management.window import MainWindow


class FakeAdministrationApi:
    def __init__(self, role: str) -> None:
        self.profile = {"id": 1, "username": "test", "role": role}
        self.status_calls = 0

    def snapshot(self) -> dict:
        return {
            "status": "ok",
            "people": [],
            "archived_people": [],
            "access_attempts": [],
        }

    def learning_reference_data(self) -> dict:
        return {"subjects": [], "rooms": [], "groups": [], "students": [], "teachers": []}

    def learning_today(self) -> dict:
        return {"lessons": [], "present": [], "alerts": []}

    def learning_lessons(self, _date_from: str, _date_to: str) -> list:
        return []

    def system_status(self) -> dict:
        self.status_calls += 1
        return {
            "server": {"hostname": "krit-server"},
            "database": {"available": True},
            "bot": {"running": True},
            "queues": {"pending": 0, "failed": 0},
            "backups": {"last_backup_at": None},
        }

    def administration_users(self) -> list:
        return []

    def close(self) -> None:
        pass


def _labels(window: MainWindow) -> list[str]:
    return [window.main_nav.item(index).text() for index in range(window.main_nav.count())]


def test_navigation_respects_roles_and_admin_status_runs_only_while_visible() -> None:
    app = QApplication.instance() or QApplication([])
    api = FakeAdministrationApi("superadmin")
    window = MainWindow(api)  # type: ignore[arg-type]
    assert _labels(window) == [
        "Клиенты",
        "Учебный процесс",
        "Рассылки",
        "Отчёты",
        "Администрирование",
    ]
    assert window.administration_page is not None
    assert not window.administration_page.status_timer.isActive()
    assert all(
        button.text() != "Обновить"
        for button in window.administration_page.findChildren(QPushButton)
    )

    window.main_nav.setCurrentRow(4)
    assert window.administration_page.status_timer.isActive()
    assert window.administration_page.pool.waitForDone(3_000)
    app.processEvents()
    assert api.status_calls == 1

    window.main_nav.setCurrentRow(0)
    assert not window.administration_page.status_timer.isActive()
    window.close()
    app.processEvents()


def test_reports_are_visible_to_director_but_administration_is_not() -> None:
    app = QApplication.instance() or QApplication([])
    director = MainWindow(FakeAdministrationApi("director"))  # type: ignore[arg-type]
    administrator = MainWindow(FakeAdministrationApi("administrator"))  # type: ignore[arg-type]
    assert _labels(director) == ["Клиенты", "Учебный процесс", "Рассылки", "Отчёты"]
    assert director.administration_page is None
    assert _labels(administrator) == ["Клиенты", "Учебный процесс", "Рассылки"]
    director.close()
    administrator.close()
    app.processEvents()


def test_initial_password_dialog_requires_matching_seven_character_password() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ChangePasswordDialog()
    dialog.new_password.setText("1234567")
    dialog.repeat_password.setText("different")
    dialog._accept_if_valid()
    assert dialog.result() == 0
    assert "не совпадают" in dialog.error.text()
    dialog.repeat_password.setText("1234567")
    dialog._accept_if_valid()
    assert dialog.result() == 1
    dialog.close()
    app.processEvents()
