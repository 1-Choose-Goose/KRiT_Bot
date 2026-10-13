from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

import krit_management.administration_page as administration_module
from krit_management.administration_page import AdministrationPage, RestoreServerDialog
from krit_management.dialogs import ChangePasswordDialog, LoginDialog
from krit_management.main import load_last_username, save_last_username
from krit_management.updates import UpdateProgress
from krit_management.window import MainWindow, UpdateProgressDialog


class FakeAdministrationApi:
    def __init__(self, role: str | None, username: str = "test") -> None:
        self.profile = {"id": 1, "username": username}
        if role is not None:
            self.profile["role"] = role
        self.status_calls = 0
        self.restarts: list[str] = []

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
            "local_backup_installations": {
                "current": 2,
                "known": 3,
                "last_success_at": "2026-10-06T17:58:00+00:00",
                "last_failure_at": None,
            },
        }

    def administration_users(self) -> list:
        return []

    def restart_service(self, service: str) -> dict:
        self.restarts.append(service)
        return {"status": "restarting"}

    def close(self) -> None:
        pass


def _labels(window: MainWindow) -> list[str]:
    return [window.main_nav.item(index).text() for index in range(window.main_nav.count())]


def test_restore_dialog_defaults_to_current_server_and_explains_same_server() -> None:
    app = QApplication.instance() or QApplication([])
    older = SimpleNamespace(
        archive=SimpleNamespace(stat=lambda: SimpleNamespace(st_size=100_000)),
        created_at=datetime(2026, 10, 10, 18, 0, tzinfo=UTC),
        info={"databases": [{"critical_counts": {"persons": 60, "lessons": 20}}]},
    )
    latest = SimpleNamespace(
        archive=SimpleNamespace(stat=lambda: SimpleNamespace(st_size=110_000)),
        created_at=datetime(2026, 10, 11, 20, 4, tzinfo=UTC),
        info={"databases": [{"critical_counts": {"persons": 68, "lessons": 30}}]},
    )
    dialog = RestoreServerDialog(
        [latest, older],
        current_server_url="https://krit.example",
        current_username="admin",
    )

    assert dialog.backup.count() == 2
    assert dialog.selected_backup() is latest
    assert "68 клиентов" in dialog.backup.currentText()
    assert "30 занятий" in dialog.backup.currentText()
    assert dialog.target_mode.currentData() == "current"
    assert dialog.server_url() == "https://krit.example"
    assert dialog.address.isEnabled() is False
    assert dialog.port.isEnabled() is False
    assert dialog.username.text() == "admin"
    assert "текущ" in dialog.instructions.text().lower()

    dialog.target_mode.setCurrentIndex(1)
    app.processEvents()
    assert dialog.target_mode.currentData() == "other"
    assert dialog.address.isEnabled() is True
    assert dialog.port.isEnabled() is True
    dialog.close()


def test_administration_control_buttons_call_expected_actions() -> None:
    app = QApplication.instance() or QApplication([])
    api = FakeAdministrationApi("superadmin")
    page = AdministrationPage(api)  # type: ignore[arg-type]
    manual_backup: list[bool] = []
    page._run_action = lambda function: function()  # type: ignore[method-assign]
    page.backup_controller.start = (  # type: ignore[method-assign]
        lambda *, manual=False: manual_backup.append(manual) or True
    )

    page.restart_bot_button.click()
    page.restart_server_button.click()
    page.backup_button.click()

    assert api.restarts == ["bot", "server"]
    assert manual_backup == [True]
    page.shutdown()
    app.processEvents()


def test_restore_to_current_server_uses_local_copy_and_current_address(
    monkeypatch, tmp_path
) -> None:
    app = QApplication.instance() or QApplication([])
    archive = tmp_path / "trusted.backup"
    archive.write_bytes(b"local backup")
    latest = SimpleNamespace(
        archive=archive,
        created_at=datetime(2026, 10, 11, 20, 4, tzinfo=UTC),
        trust="trusted",
        info={"id": "trusted", "databases": [{"name": "krit_bot"}]},
    )

    class Store:
        def entries(self):
            return [latest]

        def verify(self, entry):
            calls.append(("verify", entry.archive))
            return entry.info

    class Field:
        def __init__(self, value: str) -> None:
            self.value = value

        def text(self) -> str:
            return self.value

    dialog_arguments: dict[str, str] = {}

    class Dialog:
        def __init__(self, backups, _parent, **kwargs) -> None:
            assert backups == [latest]
            dialog_arguments.update(kwargs)
            self.username = Field("admin")
            self.password = Field("secret-password")
            self.new_password = Field("")
            self.confirmation_phrase = Field("ВОССТАНОВИТЬ")

        def exec(self):
            return administration_module.QDialog.DialogCode.Accepted

        def server_url(self) -> str:
            return "https://current.example"

        def selected_backup(self):
            return latest

    calls: list[tuple] = []

    class Target:
        def login(self, username: str, password: str) -> dict:
            calls.append(("login", username, password))
            return {"must_change_password": False}

        def create_restore(self, info: dict) -> dict:
            calls.append(("create", info))
            return {"id": "restore-1", "target_has_business_data": True}

        def upload_restore(self, restore_id: str, path, progress=None) -> None:
            calls.append(("upload", restore_id, path))
            if progress is not None:
                progress(12, 12)

        def apply_restore(self, restore_id: str, **kwargs) -> dict:
            calls.append(("apply", restore_id, kwargs["confirmation_phrase"]))
            return {"status": "accepted"}

        def restore_completion(self, restore_id: str) -> dict:
            calls.append(("completion", restore_id))
            return {"phase": "completed", "error": None}

        def close(self) -> None:
            calls.append(("close",))

    target_urls: list[str] = []
    monkeypatch.setattr(administration_module, "RestoreServerDialog", Dialog)
    monkeypatch.setattr(
        administration_module.ManagementApi,
        "for_server",
        lambda url: target_urls.append(url) or Target(),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    api = FakeAdministrationApi("superadmin", username="admin")
    api.server_url = lambda: "https://current.example"  # type: ignore[attr-defined]
    page = AdministrationPage(api)  # type: ignore[arg-type]
    page.backup_store = Store()  # type: ignore[assignment]
    restart_expected: list[bool] = []
    restart_finished: list[bool] = []
    page.server_restart_expected.connect(lambda: restart_expected.append(True))
    page.server_restart_finished.connect(lambda: restart_finished.append(True))
    page._run_progress = (  # type: ignore[method-assign]
        lambda function, progress, success, failure: success(function(progress))
    )

    page._restore_databases()

    assert dialog_arguments["current_server_url"] == "https://current.example"
    assert target_urls == ["https://current.example"]
    assert ("verify", archive) in calls
    assert ("upload", "restore-1", archive) in calls
    assert ("apply", "restore-1", "ВОССТАНОВИТЬ") in calls
    assert ("completion", "restore-1") in calls
    assert restart_expected == [True]
    assert restart_finished == [True]
    page.shutdown()
    app.processEvents()


def test_expected_restore_restart_is_shown_without_connection_error(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeAdministrationApi("superadmin"))  # type: ignore[arg-type]
    window.pool.waitForDone(5_000)
    app.processEvents()
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, message: messages.append(message),
    )

    window._expect_server_restart()
    window._refresh_failed("temporary outage", silent=False)

    assert window._server_restart_expected is True
    assert window.status_label.text() == "Восстановление данных…"
    assert messages == []

    window._loaded(FakeAdministrationApi("superadmin").snapshot())
    assert window._server_restart_expected is True
    assert window.status_label.text() == "Восстановление данных…"

    window._finish_server_restart()
    window.pool.waitForDone(5_000)
    app.processEvents()
    assert window._server_restart_expected is False
    assert window.status_label.text() == "Бот активен"
    window.close()
    app.processEvents()


def test_expired_session_is_not_reported_as_lost_connection() -> None:
    app = QApplication.instance() or QApplication([])
    api = FakeAdministrationApi("superadmin")
    api.session_expired = True
    window = MainWindow(api)  # type: ignore[arg-type]
    window.pool.waitForDone(5_000)
    app.processEvents()

    window._refresh_failed("Сеанс завершён. Войдите в программу ещё раз.", silent=True)

    assert window.status_label.text() == "Сеанс завершён"
    window.close()
    app.processEvents()


def test_update_progress_uses_fully_styled_custom_dialog() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = UpdateProgressDialog("1.0.1")

    assert dialog.objectName() == "updateProgressDialog"
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    assert dialog.minimumWidth() >= 380

    dialog.set_progress(UpdateProgress("Загрузка обновления…", 5 * 1024**2, 10 * 1024**2))
    assert dialog.progress.value() == 50
    assert "5.0 из 10.0 МБ" in dialog.details.text()

    dialog.set_installing()
    assert dialog.progress.minimum() == 0
    assert dialog.progress.maximum() == 0
    assert "установка" in dialog.stage.text().lower()
    dialog.close()
    app.processEvents()


def test_background_notification_poll_never_opens_error_dialog(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeAdministrationApi("administrator"))  # type: ignore[arg-type]
    window.pool.waitForDone(5_000)
    app.processEvents()
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, message: messages.append(message),
    )

    def fail_background(_function, _success, on_failure=None) -> None:
        (on_failure or window._show_error)("temporary outage")

    window._run = fail_background  # type: ignore[method-assign]
    window.api.communication_conversations = lambda: []  # type: ignore[attr-defined]
    window._poll_notifications()

    assert messages == []
    window.close()
    app.processEvents()


def test_restore_cancels_server_operation_when_confirmation_is_invalid(
    monkeypatch, tmp_path
) -> None:
    app = QApplication.instance() or QApplication([])
    archive = tmp_path / "trusted.backup"
    archive.write_bytes(b"local backup")
    latest = SimpleNamespace(
        archive=archive,
        created_at=datetime(2026, 10, 12, 20, 4, tzinfo=UTC),
        trust="trusted",
        info={"id": "trusted", "databases": [{"name": "krit_bot"}]},
    )

    class Store:
        def entries(self):
            return [latest]

        def verify(self, entry):
            return entry.info

    class Field:
        def __init__(self, value: str) -> None:
            self.value = value

        def text(self) -> str:
            return self.value

    class Dialog:
        def __init__(self, *_args, **_kwargs) -> None:
            self.username = Field("admin")
            self.password = Field("secret-password")
            self.new_password = Field("")
            self.confirmation_phrase = Field("НЕВЕРНО")

        def exec(self):
            return administration_module.QDialog.DialogCode.Accepted

        def server_url(self) -> str:
            return "https://current.example"

        def selected_backup(self):
            return latest

    calls: list[tuple] = []

    class Target:
        def login(self, _username: str, _password: str) -> dict:
            return {"must_change_password": False}

        def create_restore(self, _info: dict) -> dict:
            calls.append(("create",))
            return {"id": "restore-1", "target_has_business_data": True}

        def cancel_restore(self, restore_id: str) -> dict:
            calls.append(("cancel", restore_id))
            return {"cancelled": True}

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(administration_module, "RestoreServerDialog", Dialog)
    monkeypatch.setattr(
        administration_module.ManagementApi, "for_server", lambda _url: Target()
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    api = FakeAdministrationApi("superadmin", username="admin")
    api.server_url = lambda: "https://current.example"  # type: ignore[attr-defined]
    page = AdministrationPage(api)  # type: ignore[arg-type]
    page.backup_store = Store()  # type: ignore[assignment]

    def run(function, progress, _success=None, failure=None):
        try:
            function(progress)
        except Exception as exc:
            failure(str(exc))

    page._run_progress = run  # type: ignore[method-assign]

    page._restore_databases()

    assert calls == [("create",), ("cancel", "restore-1"), ("close",)]
    page.shutdown()
    app.processEvents()


def test_every_role_has_local_backup_controller() -> None:
    app = QApplication.instance() or QApplication([])
    for role in ("superadmin", "director", "administrator"):
        window = MainWindow(FakeAdministrationApi(role))  # type: ignore[arg-type]
        assert window.backup_controller is not None
        if role == "superadmin":
            assert window.administration_page is not None
            assert (
                window.administration_page.backup_controller
                is window.backup_controller
            )
        window.close()
        app.processEvents()


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

    assert page.admin_tabs.count() == 2
    assert page.admin_tabs.tabText(0) == "Управление"
    assert page.admin_tabs.tabText(1) == "Информация о сервере"
    assert page.status_details.maximumHeight() > 1_000

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
    assert rendered["Локальные резервные копии/Известные компьютеры"] == "3"
    assert "Актуальных компьютеров: 2 из 3" in page.status_labels["backups"].text()
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


def test_main_snapshot_skips_unchanged_render_and_preserves_selected_person() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeAdministrationApi("administrator"))  # type: ignore[arg-type]
    window.pool.waitForDone(5_000)
    app.processEvents()
    renders = {"people": 0, "attempts": 0, "archive": 0}
    original_people = window._render_people
    original_attempts = window._render_attempts
    original_archive = window._render_archive

    def render_people() -> None:
        renders["people"] += 1
        original_people()

    def render_attempts() -> None:
        renders["attempts"] += 1
        original_attempts()

    def render_archive() -> None:
        renders["archive"] += 1
        original_archive()

    window._render_people = render_people  # type: ignore[method-assign]
    window._render_attempts = render_attempts  # type: ignore[method-assign]
    window._render_archive = render_archive  # type: ignore[method-assign]
    first = {
        "status": "ok",
        "people": [
            {"id": 1, "full_name": "Первый Клиент", "roles": ["student"]},
            {"id": 2, "full_name": "Выбранный Клиент", "roles": ["student"]},
        ],
        "archived_people": [
            {"id": 10, "full_name": "Архив Первый", "roles": ["student"]},
            {"id": 11, "full_name": "Архив Выбранный", "roles": ["student"]},
        ],
        "access_attempts": [
            {"max_user_id": 100, "display_name": "Запрос Первый"},
            {"max_user_id": 200, "display_name": "Запрос Выбранный"},
        ],
    }
    window._last_snapshot_payload = None
    window._loaded(first)
    all_table = window.people_tables["all"]
    all_table.setCurrentCell(1, 0)
    window.archive_table.setCurrentCell(1, 0)
    window.attempts_table.setCurrentCell(1, 0)

    window._loaded(dict(first))
    assert renders == {"people": 1, "attempts": 1, "archive": 1}

    changed = dict(first)
    changed["people"] = [
        {"id": 3, "full_name": "Новый Клиент", "roles": ["student"]},
        *first["people"],
    ]
    changed["archived_people"] = [
        {"id": 12, "full_name": "Архив Новый", "roles": ["student"]},
        *first["archived_people"],
    ]
    changed["access_attempts"] = [
        {"max_user_id": 300, "display_name": "Запрос Новый"},
        *first["access_attempts"],
    ]
    window._loaded(changed)
    assert window.visible_people["all"][all_table.currentRow()]["id"] == 2
    assert changed["archived_people"][window.archive_table.currentRow()]["id"] == 11
    assert (
        changed["access_attempts"][window.attempts_table.currentRow()]["max_user_id"]
        == 200
    )
    window.close()
    app.processEvents()


def test_stale_main_snapshot_generation_is_ignored() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(FakeAdministrationApi("administrator"))  # type: ignore[arg-type]
    window.pool.waitForDone(5_000)
    app.processEvents()
    window.people = []
    window._refresh_generation = 2
    window._refresh_running = True

    window._refresh_finished(
        None,  # type: ignore[arg-type]
        {
            "status": "ok",
            "people": [{"id": 99, "full_name": "Устаревший", "roles": ["student"]}],
            "archived_people": [],
            "access_attempts": [],
        },
        generation=1,
    )

    assert window.people == []
    assert window._refresh_running is False
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


def test_login_dialog_is_blank_until_a_successful_username_was_saved(tmp_path) -> None:
    from PySide6.QtCore import QSettings

    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "login.ini"), QSettings.Format.IniFormat)
    assert load_last_username(settings) == ""
    assert LoginDialog().username.text() == ""

    save_last_username(settings, "  Choose_Goose  ")

    assert load_last_username(settings) == "Choose_Goose"
    assert LoginDialog(load_last_username(settings)).username.text() == "Choose_Goose"
    app.processEvents()
