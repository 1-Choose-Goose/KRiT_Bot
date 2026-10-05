from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from krit_management.administration_page import AdministrationPage
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


def test_navigation_respects_roles_and_admin_status_runs_only_while_visible(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
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


def test_suspicious_backup_requires_superadmin_decision(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    archive_one = tmp_path / "first.backup"
    archive_two = tmp_path / "second.backup"
    archive_one.write_bytes(b"first")
    archive_two.write_bytes(b"second")
    entries = [
        SimpleNamespace(
            archive=archive_one,
            created_at=datetime(2026, 10, 5, 10, tzinfo=UTC),
            trust="suspicious",
            reason="unexplained_data_loss",
        ),
        SimpleNamespace(
            archive=archive_two,
            created_at=datetime(2026, 10, 5, 11, tzinfo=UTC),
            trust="suspicious",
            reason="unexplained_data_loss",
        ),
    ]

    class Store:
        def entries(self):
            return list(entries)

        def latest_trusted(self):
            return None

        def trust(self, archive_name: str):
            next(item for item in entries if item.archive.name == archive_name).trust = "trusted"

        def delete_suspicious(self, archive_name: str):
            entries[:] = [item for item in entries if item.archive.name != archive_name]

        def rotate(self, **_kwargs):
            return None

    page = AdministrationPage(FakeAdministrationApi("superadmin"))  # type: ignore[arg-type]
    page.backup_store = Store()  # type: ignore[assignment]
    page._render_backup_summary()
    page.backups_table.selectRow(1)
    assert page.trust_backup_button.isEnabled()
    assert page.delete_backup_button.isEnabled()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    page._trust_selected_backup()
    assert any(item.trust == "trusted" for item in entries)

    page.backups_table.selectRow(0)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    page._delete_selected_backup()
    assert len(entries) == 1
    page.shutdown()
    app.processEvents()


def test_restricted_sections_stay_visible_and_show_role_requirements() -> None:
    app = QApplication.instance() or QApplication([])
    director = MainWindow(FakeAdministrationApi("director"))  # type: ignore[arg-type]
    administrator = MainWindow(FakeAdministrationApi("administrator"))  # type: ignore[arg-type]
    expected = [
        "Клиенты",
        "Учебный процесс",
        "Рассылки",
        "Отчёты",
        "Администрирование",
    ]
    assert _labels(director) == expected
    assert director.administration_page is None
    director.main_nav.setCurrentRow(4)
    director_denied = director.pages.currentWidget().findChild(QLabel, "accessDeniedMessage")
    assert director_denied is not None
    assert "SuperAdmin" in director_denied.text()

    assert _labels(administrator) == expected
    administrator.main_nav.setCurrentRow(3)
    reports_denied = administrator.pages.currentWidget().findChild(
        QLabel, "accessDeniedMessage"
    )
    assert reports_denied is not None
    assert "директора или SuperAdmin" in reports_denied.text()
    administrator.main_nav.setCurrentRow(4)
    administration_denied = administrator.pages.currentWidget().findChild(
        QLabel, "accessDeniedMessage"
    )
    assert administration_denied is not None
    assert "SuperAdmin" in administration_denied.text()
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
