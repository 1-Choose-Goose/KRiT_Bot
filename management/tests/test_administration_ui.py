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
    def __init__(self, role: str | None, username: str = "test") -> None:
        self.profile = {"id": 1, "username": username}
        if role is not None:
            self.profile["role"] = role
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
            "collected_at": "2026-10-06T18:00:00+00:00",
            "server": {
                "hostname": "krit-server",
                "os": "Linux",
                "kernel": "6.17.0",
                "python_version": "3.14.4",
                "krit_version": "0.4.0",
                "system_uptime_seconds": 90061,
                "process_uptime_seconds": 3661,
            },
            "resources": {
                "logical_cpus": 4,
                "load_average": [0.1, 0.2, 0.3],
                "memory": {
                    "total_bytes": 8 * 1024**3,
                    "used_bytes": 3 * 1024**3,
                    "available_bytes": 5 * 1024**3,
                    "used_percent": 37.5,
                },
                "disk": {
                    "total_bytes": 100 * 1024**3,
                    "used_bytes": 40 * 1024**3,
                    "free_bytes": 60 * 1024**3,
                    "used_percent": 40.0,
                },
            },
            "api": {"available": True, "uptime_seconds": 3661},
            "database": {
                "available": True,
                "version": "PostgreSQL 18.6",
                "revision": "20261004_administration_v7",
                "active_connections": 3,
                "databases": [{"name": "krit_bot", "size_bytes": 13_383_359}],
            },
            "bot": {
                "available": True,
                "running": True,
                "mode": "webhook",
                "last_success_at": "2026-10-06T17:59:57+00:00",
                "last_error": None,
            },
            "queues": {"pending": 0, "processing": 1, "failed": 0},
            "backups": {
                "last_backup_at": None,
                "last_result": "not_started",
                "trusted_count": 0,
                "suspicious_count": 0,
                "safety_set_pending": False,
                "free_bytes": 60 * 1024**3,
            },
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


def test_administration_renders_complete_server_information() -> None:
    app = QApplication.instance() or QApplication([])
    api = FakeAdministrationApi("superadmin")
    page = AdministrationPage(api)  # type: ignore[arg-type]

    page._render_status(api.system_status())

    rendered: dict[str, str] = {}
    for group_index in range(page.status_details.topLevelItemCount()):
        group = page.status_details.topLevelItem(group_index)
        for child_index in range(group.childCount()):
            child = group.child(child_index)
            rendered[f"{group.text(0)}/{child.text(0)}"] = child.text(1)

    assert rendered["Сервер/Версия КРиТ"] == "0.4.0"
    assert rendered["Сервер/Время работы ОС"] == "1 д 01:01:01"
    assert rendered["Ресурсы/Оперативная память"] == "3,0 ГБ из 8,0 ГБ (37,5 %)"
    assert rendered["Ресурсы/Диск"] == "40,0 ГБ из 100,0 ГБ (40,0 %)"
    assert rendered["PostgreSQL/Схема базы"] == "20261004_administration_v7"
    assert rendered["PostgreSQL/База krit_bot"] == "12,8 МБ"
    assert rendered["MAX-бот/Режим"] == "Webhook"
    assert "CPU: 4" in page.status_labels["resources"].text()

    page.shutdown()
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
    assert director_denied.text() == (
        "У вашей учётной записи нет доступа к этому разделу. "
        "Если он нужен для работы, обратитесь к руководителю."
    )

    assert _labels(administrator) == expected
    administrator.main_nav.setCurrentRow(3)
    reports_denied = administrator.pages.currentWidget().findChild(
        QLabel, "accessDeniedMessage"
    )
    assert reports_denied is not None
    assert "директора" not in reports_denied.text()
    assert "SuperAdmin" not in reports_denied.text()
    administrator.main_nav.setCurrentRow(4)
    administration_denied = administrator.pages.currentWidget().findChild(
        QLabel, "accessDeniedMessage"
    )
    assert administration_denied is not None
    assert "SuperAdmin" not in administration_denied.text()
    director.close()
    administrator.close()
    app.processEvents()


def test_server_role_overrides_legacy_admin_username() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        FakeAdministrationApi("administrator", username="admin")  # type: ignore[arg-type]
    )
    assert window.role == "administrator"
    assert window.administration_page is None
    window.main_nav.setCurrentRow(4)
    denied = window.pages.currentWidget().findChild(QLabel, "accessDeniedMessage")
    assert denied is not None
    window.close()
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
