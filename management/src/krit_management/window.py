from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QComboBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .dialogs import ROLE_LABELS, PersonDialog, person_roles
from .updates import (
    UpdateInfo,
    check_for_update,
    download_update,
    launch_updater,
    updates_supported,
)
from .workers import Worker

ASSETS_DIR = Path(__file__).with_name("assets")


def format_phone(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return str(value or "")


class MainWindow(QMainWindow):
    def __init__(self, api: ManagementApi) -> None:
        super().__init__()
        self.api = api
        self.people: list[dict[str, Any]] = []
        self.archived_people: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.people_tables: dict[str, QTableWidget] = {}
        self.visible_people: dict[str, list[dict[str, Any]]] = {}
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self._refresh_running = False
        self._background_jobs = 0
        self.setWindowTitle("КРиТ · управление")
        self.setMinimumSize(1120, 620)
        self.resize(1240, 760)
        self._build_ui()
        self.refresh()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self.refresh_timer.start()
        QTimer.singleShot(2500, self._check_updates_automatically)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._sidebar())

        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(22, 18, 22, 22)
        workspace_layout.setSpacing(14)
        workspace_layout.addWidget(self._workspace_header())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._clients_section())
        self.pages.addWidget(self._placeholder("Модуль учебного процесса"))
        self.pages.addWidget(self._placeholder("Модуль рассылок"))
        workspace_layout.addWidget(self.pages, 1)
        root_layout.addWidget(workspace, 1)
        self.setCentralWidget(root)

    def _sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(216)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(12)

        logo = QLabel()
        logo.setObjectName("sidebarLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(188, 62)
        pixmap = QPixmap(str(ASSETS_DIR / "brand_horizontal.png"))
        logo.setPixmap(
            pixmap.scaled(
                QSize(176, 52),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        layout.addWidget(logo)

        self.main_nav = QListWidget()
        self.main_nav.setObjectName("mainNavigation")
        self.main_nav.setSpacing(3)
        for label in ("Клиенты", "Учебный процесс", "Рассылки"):
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, 42))
            self.main_nav.addItem(item)
        self.main_nav.setCurrentRow(0)
        self.main_nav.currentRowChanged.connect(self._change_section)
        layout.addWidget(self.main_nav, 1)

        update_button = QPushButton("Обновления")
        update_button.setProperty("kind", "navigation")
        update_button.clicked.connect(self.check_updates)
        layout.addWidget(update_button)
        return sidebar

    def _workspace_header(self) -> QWidget:
        card = QFrame()
        card.setObjectName("workspaceHeader")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(2, 0, 2, 0)
        self.section_title = QLabel("Клиенты")
        self.section_title.setObjectName("pageTitle")
        layout.addWidget(self.section_title)
        layout.addStretch(1)
        self.status_label = QLabel("Подключение…")
        self.status_label.setObjectName("connectionStatus")
        self.status_label.setProperty("state", "loading")
        layout.addWidget(self.status_label)
        refresh_button = QPushButton("Обновить")
        refresh_button.setProperty("kind", "secondary")
        refresh_button.clicked.connect(lambda _checked=False: self.refresh(silent=False))
        layout.addWidget(refresh_button)
        return card

    def _change_section(self, index: int) -> None:
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        self.section_title.setText(("Клиенты", "Учебный процесс", "Рассылки")[index])

    def _clients_section(self) -> QWidget:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        filters = QHBoxLayout()
        self.people_search = QLineEdit()
        self.people_search.setPlaceholderText("Поиск по ФИО или телефону")
        self.people_search.setClearButtonEnabled(True)
        self.people_search.textChanged.connect(self._render_people)
        self.people_status = QComboBox()
        self.people_status.addItem("Все статусы", "all")
        self.people_status.addItem("Активные", "active")
        self.people_status.addItem("Отключённые", "inactive")
        self.people_status.addItem("Авторизованные", "authorized")
        self.people_status.addItem("Без авторизации", "unauthorized")
        self.people_status.currentIndexChanged.connect(self._render_people)
        filters.addWidget(self.people_search, 1)
        filters.addWidget(self.people_status)
        layout.addLayout(filters)
        self.client_tabs = QTabWidget()
        self.client_tabs.setObjectName("clientTabs")
        self.client_tabs.setDocumentMode(True)
        self.client_tabs.addTab(self._people_tab("all"), "Все")
        self.client_tabs.addTab(self._people_tab("student"), "Ученики")
        self.client_tabs.addTab(self._people_tab("parent"), "Родители")
        self.client_tabs.addTab(self._people_tab("teacher"), "Учителя")
        self.client_tabs.addTab(self._attempts_tab(), "Запросы авторизации")
        self.client_tabs.addTab(self._archive_tab(), "Архив")
        layout.addWidget(self.client_tabs, 1)
        return section

    def _people_tab(self, role_filter: str) -> QWidget:
        page, layout = self._page()
        add_button = QPushButton("Добавить клиента")
        add_button.setProperty("kind", "primary")
        add_button.clicked.connect(self.add_person)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(add_button)
        layout.addLayout(actions)
        table = self._table(
            ["ФИО", "Категория", "Телефон", "Действия"]
        )
        table.doubleClicked.connect(
            lambda _index, key=role_filter, widget=table: self.edit_selected_person(key, widget)
        )
        self.people_tables[role_filter] = table
        layout.addWidget(table)
        return page

    def _attempts_tab(self) -> QWidget:
        page, layout = self._page()
        self.attempts_table = self._table(
            ["Имя в MAX", "Имя пользователя", "ID в MAX", "Попыток", "Действия"]
        )
        layout.addWidget(self.attempts_table)
        return page

    def _archive_tab(self) -> QWidget:
        page, layout = self._page()
        self.archive_table = self._table(
            ["ФИО", "Категория", "Телефон", "Действия"]
        )
        layout.addWidget(self.archive_table)
        return page

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(8)
        return page, layout

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(56)
        table.horizontalHeader().setHighlightSections(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        last_column = len(headers) - 1
        header.setSectionResizeMode(last_column, QHeaderView.ResizeMode.Fixed)
        action_width = 330 if len(headers) == 4 else 250
        table.setColumnWidth(last_column, action_width)
        if len(headers) == 4:
            for column, width in ((1, 140), (2, 170)):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                table.setColumnWidth(column, width)
        return table

    @staticmethod
    def _placeholder(text: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName("placeholder")
        layout.addWidget(label)
        return page

    def _auto_refresh(self) -> None:
        if (
            not self._closing
            and QApplication.activeModalWidget() is None
            and self._background_jobs == 0
        ):
            self.refresh(silent=True)

    def refresh(self, *, silent: bool = False) -> None:
        if self._refresh_running or self._background_jobs > 0:
            return
        self._refresh_running = True
        if not silent:
            self._set_connection("Обновление…", "loading")
        worker = Worker(
            self.api.snapshot
        )
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda result, current=worker: self._refresh_finished(current, result)
        )
        worker.signals.failed.connect(
            lambda message, current=worker: self._refresh_worker_failed(
                current, message, silent
            )
        )
        self.pool.start(worker)

    def _refresh_finished(self, worker: Worker, result: object) -> None:
        self._workers.discard(worker)
        if not self._closing:
            self._loaded(result)

    def _refresh_worker_failed(self, worker: Worker, message: str, silent: bool) -> None:
        self._workers.discard(worker)
        if not self._closing:
            self._refresh_failed(message, silent)

    def _loaded(self, result: object) -> None:
        self._refresh_running = False
        status_data = result  # type: ignore[assignment]
        self.people = status_data.get("people", [])
        self.archived_people = status_data.get("archived_people", [])
        self.attempts = status_data.get("access_attempts", [])
        self._render_people()
        self._render_attempts()
        self._render_archive()
        text = "Бот активен" if status_data.get("status") == "ok" else "Сервер активен"
        self._set_connection(text, "online")

    def _refresh_failed(self, message: str, silent: bool) -> None:
        self._refresh_running = False
        self._set_connection("Нет связи", "offline")
        if not silent:
            QMessageBox.critical(self, "Ошибка подключения", message)

    def _set_connection(self, text: str, state: str) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @staticmethod
    def _set_values(table: QTableWidget, row: int, values: list[object]) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, column, item)

    def _render_people(self) -> None:
        query = self.people_search.text().casefold().strip()
        status = self.people_status.currentData()
        for role_filter, table in self.people_tables.items():
            visible = []
            for person in self.people:
                roles = person_roles(person)
                haystack = f"{person.get('full_name', '')} {person.get('phone', '')}".casefold()
                if role_filter != "all" and role_filter not in roles:
                    continue
                if query and query not in haystack:
                    continue
                if status == "active" and not person.get("active"):
                    continue
                if status == "inactive" and person.get("active"):
                    continue
                if status == "authorized" and person.get("max_user_id") is None:
                    continue
                if status == "unauthorized" and person.get("max_user_id") is not None:
                    continue
                visible.append(person)
            self.visible_people[role_filter] = visible
            table.setRowCount(len(visible))
            for row, person in enumerate(visible):
                roles = person_roles(person)
                self._set_values(table, row, [
                    person.get("full_name", ""),
                    ", ".join(ROLE_LABELS.get(role, role) for role in roles),
                    format_phone(person.get("phone")),
                ])
                table.setCellWidget(row, 3, self._actions([
                    ("Изменить", "secondary", lambda item=person: self.edit_person(item)),
                    ("В архив", "warning", lambda item=person: self.archive_person(item)),
                ]))

    def _render_attempts(self) -> None:
        self.attempts_table.setRowCount(len(self.attempts))
        for row, attempt in enumerate(self.attempts):
            self._set_values(
                self.attempts_table,
                row,
                [
                    attempt.get("display_name") or "Без имени",
                    attempt.get("username") or "—",
                    attempt.get("max_user_id", ""),
                    attempt.get("attempts", 0),
                ],
            )
            self.attempts_table.setCellWidget(
                row,
                4,
                self._actions(
                    [("Создать карточку", "secondary", lambda item=attempt: self.add_from_attempt(item))]
                ),
            )

    def _render_archive(self) -> None:
        self.archive_table.setRowCount(len(self.archived_people))
        for row, person in enumerate(self.archived_people):
            self._set_values(
                self.archive_table,
                row,
                [
                    person.get("full_name", ""),
                    ", ".join(ROLE_LABELS.get(role, role) for role in person_roles(person)),
                    format_phone(person.get("phone")),
                ],
            )
            self.archive_table.setCellWidget(
                row,
                3,
                self._actions(
                    [
                        ("Восстановить", "secondary", lambda item=person: self.restore_person(item)),
                        ("Удалить", "danger", lambda item=person: self.delete_person(item)),
                    ]
                ),
            )

    @staticmethod
    def _actions(actions: list[tuple[str, str, Any]]) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(6)
        layout.addStretch(1)
        probe = QPushButton()
        button_width = max(
            112,
            max(probe.fontMetrics().horizontalAdvance(text) for text, _, _ in actions) + 42,
        )
        probe.deleteLater()
        for text, kind, callback in actions:
            button = QPushButton(text)
            button.setProperty("kind", kind)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedSize(button_width, 34)
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, fn=callback: fn())
            layout.addWidget(button)
        layout.addStretch(1)
        return container

    def add_person(self) -> None:
        dialog = PersonDialog(parent=self, available_people=self.people, open_related=self.edit_person)
        if dialog.exec():
            related_ids, pending = dialog.relation_state()
            self._run(
                lambda: self._create_with_relations(dialog.payload(), related_ids, pending),
                lambda _: self.refresh(),
            )

    def add_from_attempt(self, attempt: dict[str, Any]) -> None:
        dialog = PersonDialog(
            {"full_name": attempt.get("display_name") or "", "active": True},
            self,
            available_people=self.people,
            open_related=self.edit_person,
        )
        if dialog.exec():
            self._run(lambda: self.api.create_person(dialog.payload()), lambda _: self.refresh())

    def edit_selected_person(self, role_filter: str, table: QTableWidget) -> None:
        row = table.currentRow()
        visible = self.visible_people.get(role_filter, [])
        if 0 <= row < len(visible):
            self.edit_person(visible[row])

    def edit_person(self, person: dict[str, Any]) -> None:
        dialog = PersonDialog(
            person,
            self,
            available_people=self.people,
            open_related=self.edit_person,
        )
        if dialog.exec():
            related_ids, pending = dialog.relation_state()
            self._run(
                lambda: self._update_with_relations(
                    person, dialog.payload(), related_ids, pending
                ),
                lambda _: self.refresh(),
            )

    def _create_with_relations(
        self, payload: dict[str, Any], related_ids: list[int], pending: list[dict[str, Any]]
    ) -> dict[str, Any]:
        person = self.api.create_person(payload)
        role = payload["roles"][0]
        for related_id in related_ids:
            student_id, guardian_id = (
                (int(person["id"]), related_id) if role == "student"
                else (related_id, int(person["id"]))
            )
            self.api.link_guardian(student_id, guardian_id)
        for related_payload in pending:
            related = self.api.create_person(related_payload)
            student_id, guardian_id = (
                (int(person["id"]), int(related["id"])) if role == "student"
                else (int(related["id"]), int(person["id"]))
            )
            self.api.link_guardian(student_id, guardian_id)
        return person

    def _update_with_relations(
        self, original: dict[str, Any], payload: dict[str, Any], related_ids: list[int],
        pending: list[dict[str, Any]],
    ) -> dict[str, Any]:
        person_id = int(original["id"])
        updated = self.api.update_person(person_id, payload)
        role = payload["roles"][0]
        original_items = original.get("guardians" if role == "student" else "students", [])
        original_ids = {int(item["id"]) for item in original_items}
        desired_ids = set(related_ids)
        for related_id in original_ids - desired_ids:
            student_id, guardian_id = (
                (person_id, related_id) if role == "student" else (related_id, person_id)
            )
            self.api.unlink_guardian(student_id, guardian_id)
        for related_id in desired_ids - original_ids:
            student_id, guardian_id = (
                (person_id, related_id) if role == "student" else (related_id, person_id)
            )
            self.api.link_guardian(student_id, guardian_id)
        for related_payload in pending:
            related = self.api.create_person(related_payload)
            student_id, guardian_id = (
                (person_id, int(related["id"])) if role == "student"
                else (int(related["id"]), person_id)
            )
            self.api.link_guardian(student_id, guardian_id)
        return updated

    def archive_person(self, person: dict[str, Any]) -> None:
        answer = QMessageBox.question(
            self,
            "Перенос в архив",
            f"Перенести «{person.get('full_name', '')}» в архив?\n\nДоступ к боту будет приостановлен.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(lambda: self.api.archive_person(int(person["id"])), lambda _: self.refresh())

    def restore_person(self, person: dict[str, Any]) -> None:
        self._run(lambda: self.api.restore_person(int(person["id"])), lambda _: self.refresh())

    def delete_person(self, person: dict[str, Any]) -> None:
        answer = QMessageBox.warning(
            self,
            "Удаление клиента",
            f"Навсегда удалить «{person.get('full_name', '')}»?\n\nЭто действие нельзя отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(lambda: self.api.delete_person(int(person["id"])), lambda _: self.refresh())

    def check_updates(self) -> None:
        self._start_update_check(interactive=True)

    def _check_updates_automatically(self) -> None:
        self._start_update_check(interactive=False)

    def _start_update_check(self, *, interactive: bool) -> None:
        self._run(
            check_for_update,
            lambda update: self._update_checked(update, interactive=interactive),
            None if interactive else lambda _: None,
        )

    def _update_checked(self, update: object, *, interactive: bool) -> None:
        if update is None:
            if interactive:
                QMessageBox.information(self, "Обновления", "Установлена актуальная версия.")
            return
        info: UpdateInfo = update  # type: ignore[assignment]
        notes = info.notes or "Описание изменений не указано."
        if not updates_supported():
            answer = QMessageBox.information(
                self,
                f"Доступна версия {info.version}",
                notes + "\n\nАвтоустановка включится в собранной Windows-версии.",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Close,
                QMessageBox.StandardButton.Open,
            )
            if answer == QMessageBox.StandardButton.Open and info.page_url:
                webbrowser.open(info.page_url)
            return
        answer = QMessageBox.question(
            self,
            f"Доступна версия {info.version}",
            notes + "\n\nСкачать и установить обновление?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(lambda: download_update(info), self._update_downloaded)

    def _update_downloaded(self, archive: object) -> None:
        launch_updater(archive)  # type: ignore[arg-type]
        self.close()

    def _run(self, function, on_success, on_failure=None) -> None:
        self._background_jobs += 1
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda result, current=worker: self._operation_finished(
                current, result, on_success
            )
        )
        worker.signals.failed.connect(
            lambda message, current=worker: self._operation_failed(
                current, message, on_failure or self._show_error
            )
        )
        self.pool.start(worker)

    def _operation_finished(self, worker: Worker, result: object, callback) -> None:
        self._workers.discard(worker)
        self._background_jobs = max(0, self._background_jobs - 1)
        if not self._closing:
            callback(result)

    def _operation_failed(self, worker: Worker, message: str, callback) -> None:
        self._workers.discard(worker)
        self._background_jobs = max(0, self._background_jobs - 1)
        if not self._closing:
            callback(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.refresh_timer.stop()
        self.pool.clear()
        self.pool.waitForDone(16000)
        self._workers.clear()
        self.api.close()
        super().closeEvent(event)
