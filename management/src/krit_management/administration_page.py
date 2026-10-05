from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .backup_store import BackupStore
from .dialogs import ChangePasswordDialog
from .widgets import configure_form_layout
from .workers import Worker


class ReportsPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("controlPanel")
        card_layout = QVBoxLayout(card)
        title = QLabel("Отчёты")
        title.setObjectName("sectionTitle")
        card_layout.addWidget(title)
        description = QLabel(
            "Раздел находится в разработке. Здесь появятся сводные отчёты клуба."
        )
        description.setWordWrap(True)
        card_layout.addWidget(description)
        card_layout.addStretch(1)
        layout.addWidget(card)


class AdminUserDialog(QDialog):
    def __init__(self, user: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.user = user or {}
        self.setWindowTitle("Пользователь программы")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        configure_form_layout(form)
        self.full_name = QLineEdit(str(self.user.get("full_name", "")))
        self.username = QLineEdit(str(self.user.get("username", "")))
        self.role = QComboBox()
        self.role.addItem("SuperAdmin", "superadmin")
        self.role.addItem("Директор", "director")
        self.role.addItem("Администратор", "administrator")
        current_role = self.role.findData(self.user.get("role", "administrator"))
        self.role.setCurrentIndex(max(0, current_role))
        form.addRow("ФИО", self.full_name)
        form.addRow("Логин", self.username)
        form.addRow("Права", self.role)
        self.password = QLineEdit()
        self.password_repeat = QLineEdit()
        for field in (self.password, self.password_repeat):
            field.setEchoMode(QLineEdit.EchoMode.Password)
        if not self.user:
            form.addRow("Пароль", self.password)
            form.addRow("Повторите пароль", self.password_repeat)
        layout.addLayout(form)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #a72c3c;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if len(self.full_name.text().strip()) < 3 or len(self.username.text().strip()) < 3:
            self.error.setText("Заполните ФИО и логин.")
            return
        if not self.user:
            if len(self.password.text()) < 7:
                self.error.setText("Пароль должен содержать не менее 7 символов.")
                return
            if self.password.text() != self.password_repeat.text():
                self.error.setText("Введённые пароли не совпадают.")
                return
        self.accept()

    def payload(self) -> dict[str, Any]:
        result = {
            "full_name": self.full_name.text().strip(),
            "username": self.username.text().strip(),
            "role": self.role.currentData(),
        }
        if not self.user:
            result["password"] = self.password.text()
        return result


class RestoreServerDialog(QDialog):
    def __init__(self, backup_description: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Восстановление баз КРиТ")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "Будет использована последняя доверенная копия:\n"
            f"{backup_description}\n\n"
            "Данные целевого сервера могут быть полностью заменены."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        form = QFormLayout()
        configure_form_layout(form)
        self.address = QLineEdit("https://")
        self.port = QLineEdit()
        self.port.setPlaceholderText("443")
        self.username = QLineEdit("Choose_Goose")
        self.password = QLineEdit()
        self.repeat_password = QLineEdit()
        self.new_password = QLineEdit()
        self.new_password_repeat = QLineEdit()
        self.confirmation_phrase = QLineEdit()
        self.confirmation_phrase.setPlaceholderText("ВОССТАНОВИТЬ — для действующего сервера")
        for field in (
            self.password,
            self.repeat_password,
            self.new_password,
            self.new_password_repeat,
        ):
            field.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Адрес сервера", self.address)
        form.addRow("Порт", self.port)
        form.addRow("Логин SuperAdmin", self.username)
        form.addRow("Пароль", self.password)
        form.addRow("Повторите пароль", self.repeat_password)
        form.addRow("Новый пароль*", self.new_password)
        form.addRow("Повтор нового пароля*", self.new_password_repeat)
        form.addRow("Контрольная фраза", self.confirmation_phrase)
        layout.addLayout(form)
        note = QLabel("* Заполняется только при первом входе с временным паролем 123.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #a72c3c;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Начать восстановление")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        address = self.address.text().strip().rstrip("/")
        if not address.startswith("https://"):
            self.error.setText("Укажите защищённый адрес, начинающийся с https://")
            return
        if not self.username.text().strip() or not self.password.text():
            self.error.setText("Укажите логин и пароль SuperAdmin.")
            return
        if self.password.text() != self.repeat_password.text():
            self.error.setText("Повтор пароля не совпадает.")
            return
        if self.new_password.text() and (
            len(self.new_password.text()) < 7
            or self.new_password.text() != self.new_password_repeat.text()
        ):
            self.error.setText("Проверьте новый пароль и его повтор.")
            return
        self.accept()

    def server_url(self) -> str:
        address = self.address.text().strip().rstrip("/")
        port = self.port.text().strip()
        return f"{address}:{port}" if port and port != "443" else address


class AdministrationPage(QWidget):
    def __init__(self, api: ManagementApi, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.backup_store = BackupStore.default()
        self._backup_running = False
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._status_running = False
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(15_000)
        self.status_timer.timeout.connect(self.refresh_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        status_card = QFrame()
        status_card.setObjectName("controlPanel")
        status_layout = QVBoxLayout(status_card)
        status_title = QLabel("Состояние сервера")
        status_title.setObjectName("sectionTitle")
        status_layout.addWidget(status_title)
        status_row = QHBoxLayout()
        self.status_labels: dict[str, QLabel] = {}
        for key, title in (
            ("server", "Сервер"),
            ("database", "PostgreSQL"),
            ("bot", "MAX-бот"),
            ("queues", "Очереди"),
            ("backups", "Резервные копии"),
        ):
            label = QLabel(f"{title}\nЗагрузка…")
            label.setObjectName("statusCard")
            label.setMinimumWidth(130)
            label.setWordWrap(True)
            self.status_labels[key] = label
            status_row.addWidget(label, 1)
        status_layout.addLayout(status_row)
        layout.addWidget(status_card)

        users_card = QFrame()
        users_card.setObjectName("controlPanel")
        users_layout = QVBoxLayout(users_card)
        users_header = QHBoxLayout()
        users_header.addWidget(QLabel("Пользователи программы"))
        users_header.addStretch(1)
        self.add_user_button = QPushButton("Добавить пользователя")
        users_header.addWidget(self.add_user_button)
        users_layout.addLayout(users_header)
        self.users_table = QTableWidget(0, 4)
        self.users_table.setHorizontalHeaderLabels(["ФИО", "Логин", "Права", "Доступ"])
        self.users_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.users_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.users_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.users_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        users_layout.addWidget(self.users_table)
        actions = QHBoxLayout()
        for text, name in (
            ("Изменить", "edit_user_button"),
            ("Сменить пароль", "password_button"),
            ("Отключить доступ", "access_button"),
            ("Полностью удалить", "delete_user_button"),
        ):
            button = QPushButton(text)
            if "удалить" in text.lower():
                button.setProperty("kind", "danger")
            elif "отключить" in text.lower():
                button.setProperty("kind", "warning")
            setattr(self, name, button)
            actions.addWidget(button)
        actions.addStretch(1)
        users_layout.addLayout(actions)
        layout.addWidget(users_card, 1)

        controls = QHBoxLayout()
        self.restart_bot_button = QPushButton("Перезапустить MAX-бот")
        self.restart_server_button = QPushButton("Перезапустить КРиТ")
        self.backup_button = QPushButton("Создать резервную копию")
        self.restore_button = QPushButton("Восстановить базы")
        self.restore_button.setProperty("kind", "warning")
        for button in (
            self.restart_bot_button,
            self.restart_server_button,
            self.backup_button,
            self.restore_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.backup_summary = QLabel("Локальные резервные копии: проверка…")
        self.backup_summary.setWordWrap(True)
        layout.addWidget(self.backup_summary)
        safety_actions = QHBoxLayout()
        self.safety_label = QLabel("Страховочная копия сервера: не ожидает решения")
        self.rollback_safety_button = QPushButton("Вернуть прежние базы")
        self.delete_safety_button = QPushButton("Удалить страховочную копию")
        self.delete_safety_button.setProperty("kind", "danger")
        self.rollback_safety_button.setEnabled(False)
        self.delete_safety_button.setEnabled(False)
        safety_actions.addWidget(self.safety_label)
        safety_actions.addStretch(1)
        safety_actions.addWidget(self.rollback_safety_button)
        safety_actions.addWidget(self.delete_safety_button)
        layout.addLayout(safety_actions)

        self.restart_bot_button.clicked.connect(
            lambda: self._run_action(lambda: self.api.restart_service("bot"))
        )
        self.restart_server_button.clicked.connect(
            lambda: self._run_action(lambda: self.api.restart_service("server"))
        )
        self.backup_button.clicked.connect(self._create_backup)
        self.restore_button.clicked.connect(self._restore_databases)
        self.add_user_button.clicked.connect(self._add_user)
        self.edit_user_button.clicked.connect(self._edit_user)
        self.password_button.clicked.connect(self._change_password)
        self.access_button.clicked.connect(self._toggle_access)
        self.delete_user_button.clicked.connect(self._delete_user)
        self.users_table.itemSelectionChanged.connect(self._selection_changed)
        self.rollback_safety_button.clicked.connect(self._rollback_safety)
        self.delete_safety_button.clicked.connect(self._delete_safety)
        self._selection_changed()
        self._render_backup_summary()

    def activate(self) -> None:
        if not self.status_timer.isActive():
            self.status_timer.start()
        self.refresh_status()
        self.refresh_users()

    def deactivate(self) -> None:
        self.status_timer.stop()

    def shutdown(self) -> None:
        self.deactivate()
        self.pool.clear()
        self.pool.waitForDone(16_000)
        self._workers.clear()

    def refresh_status(self) -> None:
        if self._status_running:
            return
        self._status_running = True
        worker = Worker(self.api.system_status)
        self._workers.add(worker)

        def finished(payload: object) -> None:
            self._status_running = False
            self._workers.discard(worker)
            self._render_status(payload if isinstance(payload, dict) else {})

        def failed(_message: str) -> None:
            self._status_running = False
            self._workers.discard(worker)
            for label in self.status_labels.values():
                label.setText("Недоступно")

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def _render_status(self, payload: dict[str, Any]) -> None:
        server = payload.get("server") or {}
        database = payload.get("database") or {}
        bot = payload.get("bot") or {}
        queues = payload.get("queues") or {}
        backups = payload.get("backups") or {}
        self.status_labels["server"].setText(
            f"Сервер\n{server.get('hostname', 'Недоступно')}"
        )
        self.status_labels["database"].setText(
            "PostgreSQL\nРаботает" if database.get("available") else "PostgreSQL\nНедоступно"
        )
        self.status_labels["bot"].setText(
            "MAX-бот\nРаботает" if bot.get("running") else "MAX-бот\nОстановлен"
        )
        self.status_labels["queues"].setText(
            f"Очереди\nОжидают: {queues.get('pending', 0)} · Ошибки: {queues.get('failed', 0)}"
        )
        self.status_labels["backups"].setText(
            f"Резервные копии\nПоследняя: {backups.get('last_backup_at') or 'ещё нет'}"
        )
        safety_pending = bool(backups.get("safety_set_pending"))
        self.safety_label.setText(
            "Страховочная копия сервера: ожидает решения"
            if safety_pending
            else "Страховочная копия сервера: не ожидает решения"
        )
        self.rollback_safety_button.setEnabled(safety_pending)
        self.delete_safety_button.setEnabled(safety_pending)

    def refresh_users(self) -> None:
        self._run(self.api.administration_users, self._render_users)

    def _render_users(self, users: object) -> None:
        values = users if isinstance(users, list) else []
        self.users_table.setRowCount(len(values))
        role_labels = {
            "superadmin": "SuperAdmin",
            "director": "Директор",
            "administrator": "Администратор",
        }
        for row, user in enumerate(values):
            columns = (
                user.get("full_name", ""),
                user.get("username", ""),
                role_labels.get(user.get("role"), user.get("role", "")),
                "Включён" if user.get("active") else "Отключён",
            )
            for column, value in enumerate(columns):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, user)
                self.users_table.setItem(row, column, item)
        self._selection_changed()

    def _selected_user(self) -> dict[str, Any] | None:
        row = self.users_table.currentRow()
        item = self.users_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _selection_changed(self) -> None:
        user = self._selected_user()
        enabled = user is not None
        for button in (
            self.edit_user_button,
            self.password_button,
            self.access_button,
            self.delete_user_button,
        ):
            button.setEnabled(enabled)
        if user is not None:
            self.access_button.setText(
                "Отключить доступ" if user.get("active") else "Включить доступ"
            )

    def _add_user(self) -> None:
        dialog = AdminUserDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run(
                lambda: self.api.create_administrator(dialog.payload()),
                lambda _result: self.refresh_users(),
            )

    def _edit_user(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dialog = AdminUserDialog(user, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run(
                lambda: self.api.update_administrator(int(user["id"]), dialog.payload()),
                lambda _result: self.refresh_users(),
            )

    def _change_password(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dialog = ChangePasswordDialog(self)
        dialog.setWindowTitle(f"Новый пароль · {user.get('full_name', '')}")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run(
                lambda: self.api.change_administrator_password(
                    int(user["id"]), dialog.new_password.text()
                )
            )

    def _toggle_access(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        enabled = not bool(user.get("active"))
        self._run(
            lambda: self.api.set_administrator_access(int(user["id"]), enabled=enabled),
            lambda _result: self.refresh_users(),
        )

    def _delete_user(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        answer = QMessageBox.question(
            self,
            "Полное удаление пользователя",
            "Удалить учётную запись без возможности входа?\n\n"
            f"ФИО: {user.get('full_name', '')}\nЛогин: {user.get('username', '')}",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(
                lambda: self.api.delete_administrator(int(user["id"])),
                lambda _result: self.refresh_users(),
            )

    def _run(self, function, success=None, failure=None) -> None:
        worker = Worker(function)
        self._workers.add(worker)

        def done(result: object) -> None:
            self._workers.discard(worker)
            if success is not None:
                success(result)

        worker.signals.finished.connect(done)
        def failed(message: str) -> None:
            self._workers.discard(worker)
            if failure is not None:
                failure(message)
            else:
                QMessageBox.warning(self, "Операция не выполнена", message)

        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def _run_action(self, function) -> None:
        self._run(function, lambda _result: self.refresh_status())

    def _create_backup(self) -> None:
        self._start_backup()

    def run_daily_backup(self) -> None:
        latest = self.backup_store.latest_trusted()
        if latest is not None and datetime.now(UTC) - latest.created_at < timedelta(hours=24):
            self._render_backup_summary()
            return
        self._start_backup()

    def _start_backup(self) -> None:
        if self._backup_running:
            return
        self._backup_running = True
        self.backup_button.setEnabled(False)

        def create_and_store():
            self.backup_store.root.mkdir(parents=True, exist_ok=True)
            self.backup_store.discard_partial()
            info = self.api.create_backup()
            temporary = self.backup_store.root / f"download-{info['id']}.part"
            try:
                self.api.download_backup(str(info["id"]), temporary)

                def chunks():
                    with temporary.open("rb") as stream:
                        while block := stream.read(1024 * 1024):
                            yield block

                entry = self.backup_store.install_from_stream(info, chunks())
                self.backup_store.rotate(daily=7, weekly=4)
                return entry
            finally:
                temporary.unlink(missing_ok=True)

        def complete(_result: object) -> None:
            self._backup_running = False
            self.backup_button.setEnabled(True)
            self._render_backup_summary()
            self.refresh_status()

        def failed(message: str) -> None:
            self._backup_running = False
            self.backup_button.setEnabled(True)
            self._render_backup_summary()
            QMessageBox.warning(self, "Резервная копия не создана", message)

        self._run(create_and_store, complete, failed)

    def _render_backup_summary(self) -> None:
        entries = self.backup_store.entries()
        trusted = [item for item in entries if item.trust == "trusted"]
        suspicious = [item for item in entries if item.trust == "suspicious"]
        latest = max(trusted, key=lambda item: item.created_at, default=None)
        latest_text = (
            latest.created_at.astimezone().strftime("%d.%m.%Y %H:%M")
            if latest is not None
            else "ещё нет"
        )
        self.backup_summary.setText(
            f"Локальные копии: доверенных {len(trusted)}, подозрительных "
            f"{len(suspicious)} · последняя: {latest_text}"
        )

    def _restore_databases(self) -> None:
        latest = self.backup_store.latest_trusted()
        if latest is None:
            QMessageBox.warning(
                self,
                "Нет резервной копии",
                "Сначала создайте и успешно сохраните доверенную резервную копию.",
            )
            return
        description = (
            f"{latest.created_at.astimezone().strftime('%d.%m.%Y %H:%M')} · "
            f"{latest.archive.stat().st_size / (1024 * 1024):.1f} МБ"
        )
        dialog = RestoreServerDialog(description, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        credentials = {
            "url": dialog.server_url(),
            "username": dialog.username.text().strip(),
            "password": dialog.password.text(),
            "new_password": dialog.new_password.text(),
            "confirmation": dialog.confirmation_phrase.text().strip(),
        }
        self.restore_button.setEnabled(False)

        def restore() -> dict[str, Any]:
            target = ManagementApi.for_server(str(credentials["url"]))
            try:
                profile = target.login(
                    str(credentials["username"]), str(credentials["password"])
                )
                if profile.get("must_change_password"):
                    if not credentials["new_password"]:
                        raise RuntimeError(
                            "Для нового сервера укажите новый постоянный пароль."
                        )
                    target.change_initial_password(
                        str(credentials["password"]),
                        str(credentials["new_password"]),
                    )
                    credentials["password"] = credentials["new_password"]
                operation = target.create_restore(latest.info)
                if operation.get("target_has_business_data") and (
                    credentials["confirmation"] != "ВОССТАНОВИТЬ"
                ):
                    raise RuntimeError(
                        "Для действующего сервера введите контрольную фразу ВОССТАНОВИТЬ."
                    )
                target.upload_restore(str(operation["id"]), latest.archive)
                return target.apply_restore(
                    str(operation["id"]),
                    password=str(credentials["password"]),
                    confirmation_phrase=str(credentials["confirmation"]),
                )
            finally:
                credentials["password"] = ""
                credentials["new_password"] = ""
                target.close()

        def complete(_result: object) -> None:
            self.restore_button.setEnabled(True)
            QMessageBox.information(
                self,
                "Восстановление запущено",
                "Сервер принял комплект. После перезапуска войдите в программу заново.",
            )

        def failed(message: str) -> None:
            self.restore_button.setEnabled(True)
            QMessageBox.warning(self, "Восстановление не выполнено", message)

        self._run(restore, complete, failed)

    def _rollback_safety(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Вернуть прежние базы",
            "Текущие базы будут заменены страховочной копией. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(
                self.api.rollback_restore_safety,
                lambda _result: self.refresh_status(),
            )

    def _delete_safety(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Удалить страховочную копию",
            "После удаления вернуть прежнее состояние будет невозможно. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(
                self.api.delete_restore_safety,
                lambda _result: self.refresh_status(),
            )
