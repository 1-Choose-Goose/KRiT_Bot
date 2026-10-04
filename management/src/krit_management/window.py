from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .communications_page import CommunicationsPage
from .dialogs import ROLE_LABELS, PersonDialog, person_roles
from .learning_page import LearningPage
from .timeutils import parse_center
from .updates import (
    UpdateInfo,
    UpdateProgress,
    check_for_update,
    download_update,
    launch_updater,
    updates_supported,
)
from .widgets import SafeComboBox, matches_word_prefix
from .workers import ProgressWorker, Worker

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
        self.people_empty_labels: dict[str, QLabel] = {}
        self.visible_people: dict[str, list[dict[str, Any]]] = {}
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self._refresh_running = False
        self._background_jobs = 0
        self._update_progress: QProgressDialog | None = None
        self._seen_notification_ids: set[int] = set()
        self._notifications: list[dict[str, Any]] = []
        self._last_toast_lesson_id: int | None = None
        self._last_toast_person_id: int | None = None
        self._conversation_unread_counts: dict[int, int] | None = None
        self.setWindowTitle("КРиТ · управление")
        self.setMinimumSize(1120, 620)
        self.resize(1240, 760)
        self._build_ui()
        self.learning_page.notifications_changed.connect(self._notifications_changed)
        self.learning_page.person_requested.connect(self._open_person_by_id)
        self.communications_page.unread_changed.connect(self._set_message_unread_total)
        self.tray = QSystemTrayIcon(QIcon(str(ASSETS_DIR / "app_icon.ico")), self)
        self.tray.setToolTip("КРиТ · управление")
        self.tray.messageClicked.connect(self._open_last_toast)
        self.tray.show()
        self.refresh()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self.refresh_timer.start()
        self.notification_timer = QTimer(self)
        self.notification_timer.setInterval(10_000)
        self.notification_timer.timeout.connect(self._poll_notifications)
        self.notification_timer.start()
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
        self.learning_page = LearningPage(self.api)
        self.pages.addWidget(self.learning_page)
        self.communications_page = CommunicationsPage(self.api)
        self.pages.addWidget(self.communications_page)
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
        self.notifications_button = QPushButton("Уведомления")
        self.notifications_button.setProperty("kind", "secondary")
        self.notifications_button.clicked.connect(self.open_notification_center)
        layout.addWidget(self.notifications_button)
        return card

    def _notifications_changed(self, notifications: list[dict[str, Any]]) -> None:
        self._notifications = self._deduplicate_notifications(notifications)
        unread = [item for item in self._notifications if not item.get("read_at")]
        self.notifications_button.setText(
            f"Уведомления ({len(unread)})" if unread else "Уведомления"
        )
        new_items = [
            item for item in unread if int(item.get("id", 0)) not in self._seen_notification_ids
        ]
        for item in reversed(new_items[:3]):
            self._last_toast_lesson_id = (
                int(item["lesson_id"]) if item.get("lesson_id") is not None else None
            )
            self._last_toast_person_id = None
            self.tray.showMessage(
                str(item.get("title", "Уведомление КРиТ")),
                str(item.get("message", "")),
                QSystemTrayIcon.MessageIcon.Information,
                7000,
            )
        self._seen_notification_ids.update(int(item.get("id", 0)) for item in unread)

    def _open_last_toast(self) -> None:
        if self._last_toast_person_id is not None:
            self.main_nav.setCurrentRow(2)
            self.communications_page.current_person_id = self._last_toast_person_id
            self.communications_page.tabs.setCurrentIndex(1)
            self.communications_page.load_dialogs()
            return
        if self._last_toast_lesson_id is not None:
            self._open_lesson_from_person(self._last_toast_lesson_id)

    def _set_message_unread_total(self, total: int) -> None:
        self.main_nav.item(2).setText(f"Рассылки ({total})" if total else "Рассылки")

    def _conversations_changed(self, result: object) -> None:
        rows = list(result) if isinstance(result, list) else []
        counts = {
            int(row["person_id"]): int(row.get("admin_unread_count") or 0)
            for row in rows
        }
        self._set_message_unread_total(sum(counts.values()))
        previous = self._conversation_unread_counts
        self._conversation_unread_counts = counts
        if previous is None:
            return
        for row in rows:
            person_id = int(row["person_id"])
            unread = counts[person_id]
            if unread <= previous.get(person_id, 0):
                continue
            self._last_toast_lesson_id = None
            self._last_toast_person_id = person_id
            self.tray.showMessage(
                f"Новое сообщение: {row.get('full_name', 'Клиент')}",
                str(row.get("last_message_preview") or "Новое сообщение"),
                QSystemTrayIcon.MessageIcon.Information,
                7000,
            )
            break

    def open_notification_center(self) -> None:
        self._run(
            lambda: self.api.admin_notifications(unread_only=False),
            self._show_notification_center,
        )

    def _poll_notifications(self) -> None:
        if self._closing or QApplication.activeModalWidget() is not None:
            return
        self._run(
            lambda: self.api.admin_notifications(unread_only=False),
            lambda result: self._notifications_changed(result if isinstance(result, list) else []),
        )
        self._run(
            self.api.communication_conversations,
            self._conversations_changed,
            lambda _message: None,
        )

    def _show_notification_center(self, result: object) -> None:
        notifications = self._deduplicate_notifications(result if isinstance(result, list) else [])
        dialog = QDialog(self)
        dialog.setWindowTitle("Центр уведомлений")
        dialog.setMinimumSize(760, 460)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Состояние", "Дата", "Событие", "Сообщение"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 105)
        table.setColumnWidth(1, 145)
        table.setColumnWidth(2, 190)
        table.setRowCount(len(notifications))
        for row, item in enumerate(notifications):
            created = parse_center(item["created_at"])
            values = [
                "Прочитано" if item.get("read_at") else "Новое",
                f"{created:%d.%m.%Y %H:%M}",
                item.get("title", ""),
                item.get("message", ""),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, item)
                table.setItem(row, column, cell)
        layout.addWidget(table)
        resolution_panel = QWidget()
        resolution_layout = QHBoxLayout(resolution_panel)
        resolution_layout.setContentsMargins(0, 0, 0, 0)
        resolution_layout.addWidget(QLabel("В занятии не осталось участвующих учеников"))
        resolution_layout.addStretch(1)
        finish_early_button = QPushButton("Завершить досрочно")
        finish_early_button.setProperty("kind", "danger")
        keep_active_button = QPushButton("Оставить активным")
        keep_active_button.setProperty("kind", "secondary")
        resolution_layout.addWidget(finish_early_button)
        resolution_layout.addWidget(keep_active_button)
        resolution_panel.setVisible(False)
        layout.addWidget(resolution_panel)

        footer = QHBoxLayout()
        open_button = QPushButton("Открыть занятие")
        open_button.setProperty("kind", "secondary")
        footer.addWidget(open_button)
        footer.addStretch(1)
        read_button = QPushButton("Прочитать")
        read_button.setProperty("kind", "secondary")
        read_all_button = QPushButton("Прочитать все")
        read_all_button.setProperty("kind", "secondary")
        close_button = QPushButton("Закрыть")
        close_button.setProperty("kind", "secondary")
        footer.addWidget(read_button)
        footer.addWidget(read_all_button)
        footer.addWidget(close_button)
        close_button.clicked.connect(dialog.reject)
        layout.addLayout(footer)

        def selected() -> dict[str, Any] | None:
            row = table.currentRow()
            return table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None

        def selection_changed() -> None:
            item = selected()
            show_resolution = bool(item and item.get("kind") == "lesson_no_active_students")
            resolution_panel.setVisible(show_resolution)

        def open_selected() -> None:
            item = selected()
            if item and item.get("lesson_id"):
                dialog.accept()
                self._run(
                    lambda: self.api.read_admin_notification(int(item["id"])),
                    lambda _result: self._open_lesson_from_person(int(item["lesson_id"])),
                )

        def read_selected() -> None:
            item = selected()
            if item:
                row = table.currentRow()
                self._run(
                    lambda: self.api.read_admin_notification(int(item["id"])),
                    lambda _result: table.item(row, 0).setText("Прочитано"),
                )

        def finish_selected_early() -> None:
            item = selected()
            if item and item.get("kind") == "lesson_no_active_students" and item.get("lesson_id"):
                dialog.accept()
                self.learning_page._finish_lesson_early(int(item["lesson_id"]))

        def keep_selected_active() -> None:
            item = selected()
            if item and item.get("kind") == "lesson_no_active_students":
                read_selected()

        open_button.clicked.connect(open_selected)
        finish_early_button.clicked.connect(finish_selected_early)
        keep_active_button.clicked.connect(keep_selected_active)
        read_button.clicked.connect(read_selected)
        read_all_button.clicked.connect(
            lambda _checked=False: self._run(
                self.api.read_all_admin_notifications,
                lambda _result: [
                    table.item(row, 0).setText("Прочитано") for row in range(table.rowCount())
                ],
            )
        )
        table.itemSelectionChanged.connect(selection_changed)
        table.doubleClicked.connect(lambda _index: open_selected())
        dialog.exec()
        self.learning_page.refresh_today()

    @staticmethod
    def _deduplicate_notifications(
        notifications: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[object, ...]] = set()
        for item in notifications:
            key = (
                ("lesson", item.get("lesson_id"), item.get("kind"))
                if item.get("lesson_id") is not None
                else ("id", item.get("id"))
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _change_section(self, index: int) -> None:
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        self.section_title.setText(("Клиенты", "Учебный процесс", "Рассылки")[index])
        if index == 1:
            self.learning_page.refresh()
        elif index == 2:
            self.communications_page.refresh()

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
        self.people_status = SafeComboBox()
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
        self.client_tabs.addTab(self._people_tab("student"), "Ученики")
        self.client_tabs.addTab(self._people_tab("teacher"), "Учителя")
        self.client_tabs.addTab(self._people_tab("parent"), "Родители")
        self.client_tabs.addTab(self._people_tab("all"), "Все")
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
        table = self._table(["ФИО", "Роли", "Телефон", "Действия"])
        table.doubleClicked.connect(
            lambda _index, key=role_filter, widget=table: self.edit_selected_person(key, widget)
        )
        self.people_tables[role_filter] = table
        layout.addWidget(table, 1)
        empty = self._empty_state("В этом разделе пока нет клиентов")
        self.people_empty_labels[role_filter] = empty
        layout.addWidget(empty, 1)
        return page

    def _attempts_tab(self) -> QWidget:
        page, layout = self._page()
        self.attempts_table = self._table(
            ["Имя в MAX", "Имя пользователя", "ID в MAX", "Попыток", "Действия"]
        )
        layout.addWidget(self.attempts_table, 1)
        self.attempts_empty = self._empty_state("Новых запросов авторизации нет")
        layout.addWidget(self.attempts_empty, 1)
        return page

    def _archive_tab(self) -> QWidget:
        page, layout = self._page()
        self.archive_table = self._table(["ФИО", "Роли", "Телефон", "Действия"])
        layout.addWidget(self.archive_table, 1)
        self.archive_empty = self._empty_state("Архив пуст")
        layout.addWidget(self.archive_empty, 1)
        return page

    @staticmethod
    def _empty_state(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("emptyState")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

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
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
        worker = Worker(self.api.snapshot)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda result, current=worker: self._refresh_finished(current, result)
        )
        worker.signals.failed.connect(
            lambda message, current=worker: self._refresh_worker_failed(current, message, silent)
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
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, column, item)

    def _render_people(self) -> None:
        query = self.people_search.text().casefold().strip()
        query_digits = "".join(character for character in query if character.isdigit())
        status = self.people_status.currentData()
        for role_filter, table in self.people_tables.items():
            visible = []
            for person in self.people:
                roles = person_roles(person)
                if role_filter != "all" and role_filter not in roles:
                    continue
                name_matches = matches_word_prefix(query, str(person.get("full_name", "")))
                phone_digits = "".join(
                    character for character in str(person.get("phone", "")) if character.isdigit()
                )
                if (
                    query
                    and not name_matches
                    and not (query_digits and query_digits in phone_digits)
                ):
                    continue
                if status == "active" and not person.get("bot_access_enabled", True):
                    continue
                if status == "inactive" and person.get("bot_access_enabled", True):
                    continue
                if status == "authorized" and person.get("max_user_id") is None:
                    continue
                if status == "unauthorized" and person.get("max_user_id") is not None:
                    continue
                visible.append(person)
            self.visible_people[role_filter] = visible
            table.setVisible(bool(visible))
            self.people_empty_labels[role_filter].setVisible(not visible)
            table.setRowCount(len(visible))
            for row, person in enumerate(visible):
                roles = person_roles(person)
                self._set_values(
                    table,
                    row,
                    [
                        person.get("full_name", ""),
                        ", ".join(ROLE_LABELS.get(role, role) for role in roles),
                        format_phone(person.get("phone")),
                    ],
                )
                table.setCellWidget(
                    row,
                    3,
                    self._actions(
                        [
                            (
                                "В архив",
                                "warning",
                                lambda item=person: self.archive_person(item),
                            ),
                        ]
                    ),
                )

    def open_person_notifications(self, person: dict[str, Any]) -> None:
        self._change_section(2)
        self.communications_page.tabs.setCurrentIndex(3)
        self.communications_page.pending_settings_person_id = int(person["id"])
        self.communications_page.refresh()

    def _render_attempts(self) -> None:
        self.attempts_table.setVisible(bool(self.attempts))
        self.attempts_empty.setVisible(not self.attempts)
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
                    [
                        (
                            "Создать карточку",
                            "secondary",
                            lambda item=attempt: self.add_from_attempt(item),
                        )
                    ]
                ),
            )

    def _render_archive(self) -> None:
        self.archive_table.setVisible(bool(self.archived_people))
        self.archive_empty.setVisible(not self.archived_people)
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
                        (
                            "Восстановить",
                            "secondary",
                            lambda item=person: self.restore_person(item),
                        ),
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
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for text, kind, callback in actions:
            button = QPushButton(text)
            button.setProperty("kind", kind)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button_width = max(92, button.fontMetrics().horizontalAdvance(text) + 48)
            button.setFixedSize(button_width, 34)
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, fn=callback: fn())
            layout.addWidget(button)
        return container

    def open_person_journal(self, person: dict[str, Any]) -> None:
        dialog = PersonDialog(
            person,
            self,
            available_people=self.people,
            open_related=self.edit_person,
            load_learning_history=self._load_person_history,
            open_lesson=self._open_lesson_from_person,
            open_notifications=self.open_person_notifications,
        )
        history_index = next(
            (
                index
                for index in range(dialog.sections.count())
                if dialog.sections.tabText(index) == "Учебный процесс"
            ),
            -1,
        )
        if history_index >= 0:
            dialog.sections.setCurrentIndex(history_index)
        dialog.exec()

    def add_person(self) -> None:
        dialog = PersonDialog(
            parent=self, available_people=self.people, open_related=self.edit_person
        )
        if dialog.exec():
            relations = dialog.relation_state()
            self._run(
                lambda: self._create_with_relations(dialog.payload(), relations),
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
            load_learning_history=self._load_person_history,
            open_lesson=self._open_lesson_from_person,
            open_notifications=self.open_person_notifications,
        )
        if dialog.exec():
            relations = dialog.relation_state()
            self._run(
                lambda: self._update_with_relations(person, dialog.payload(), relations),
                lambda _: self.refresh(),
            )

    def _open_person_by_id(self, person_id: int) -> None:
        person = next((item for item in self.people if int(item["id"]) == person_id), None)
        if person is not None:
            self.edit_person(person)

    def _load_person_history(
        self,
        person_id: int,
        roles: list[str],
        callback,
    ) -> None:
        def load() -> dict[str, Any]:
            result: dict[str, Any] = {}
            if "student" in roles:
                result["student"] = self.api.student_history(person_id)
            if "teacher" in roles:
                result["teacher"] = self.api.teacher_history(person_id)
            return result

        self._run(load, callback)

    def _open_lesson_from_person(self, lesson_id: int) -> None:
        self._run(
            lambda: self.api.learning_lesson(lesson_id),
            lambda lesson: (
                self.learning_page.open_lesson(lesson) if isinstance(lesson, dict) else None
            ),
        )

    def _create_with_relations(
        self,
        payload: dict[str, Any],
        relations: dict[str, tuple[list[int], list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        return self.api.create_person_aggregate(
            {
                "person": payload,
                "parent_ids": relations.get("parent", ([], []))[0],
                "student_ids": relations.get("student", ([], []))[0],
                "new_parents": relations.get("parent", ([], []))[1],
                "new_students": relations.get("student", ([], []))[1],
            }
        )

    def _update_with_relations(
        self,
        original: dict[str, Any],
        payload: dict[str, Any],
        relations: dict[str, tuple[list[int], list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        person_id = int(original["id"])
        return self.api.update_person_aggregate(
            person_id,
            {
                "person": payload,
                "parent_ids": relations.get("parent", ([], []))[0],
                "student_ids": relations.get("student", ([], []))[0],
                "new_parents": relations.get("parent", ([], []))[1],
                "new_students": relations.get("student", ([], []))[1],
            },
        )

    def archive_person(self, person: dict[str, Any]) -> None:
        answer = QMessageBox.question(
            self,
            "Перенос в архив",
            f"Перенести «{person.get('full_name', '')}» в архив?\n\n"
            "Доступ к боту будет приостановлен.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:

            def failed(message: str) -> None:
                if "can_resolve_student_dependencies" not in message:
                    self._show_error(message)
                    return
                confirm = QMessageBox.warning(
                    self,
                    "Будущие занятия",
                    "У ученика есть будущие занятия или активные группы. "
                    "Исключить его из будущих занятий и архивировать?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if confirm == QMessageBox.StandardButton.Yes:
                    self._run(
                        lambda: self.api.archive_person(
                            int(person["id"]),
                            resolve_future_student_dependencies=True,
                        ),
                        lambda _: self.refresh(),
                    )

            self._run(
                lambda: self.api.archive_person(int(person["id"])),
                lambda _: self.refresh(),
                failed,
            )

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
            self._start_update_download(info)

    def _start_update_download(self, info: UpdateInfo) -> None:
        dialog = QProgressDialog("Подключение к серверу обновлений…", "", 0, 100, self)
        dialog.setWindowTitle(f"Обновление КРиТ до версии {info.version}")
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setValue(0)
        dialog.show()
        self._update_progress = dialog

        self._background_jobs += 1
        worker = ProgressWorker(lambda report: download_update(info, report))
        self._workers.add(worker)
        worker.signals.progress.connect(self._update_download_progress)
        worker.signals.finished.connect(
            lambda result, current=worker: self._operation_finished(
                current, result, self._update_downloaded
            )
        )
        worker.signals.failed.connect(
            lambda message, current=worker: self._operation_failed(
                current, message, self._update_download_failed
            )
        )
        self.pool.start(worker)

    def _update_download_progress(self, value: object) -> None:
        if self._update_progress is None or not isinstance(value, UpdateProgress):
            return
        if value.total > 0:
            downloaded_mb = value.downloaded / (1024 * 1024)
            total_mb = value.total / (1024 * 1024)
            self._update_progress.setRange(0, 100)
            self._update_progress.setValue(value.percent)
            self._update_progress.setLabelText(
                f"{value.stage}\n{downloaded_mb:.1f} из {total_mb:.1f} МБ"
            )
        else:
            self._update_progress.setRange(0, 0)
            self._update_progress.setLabelText(value.stage)

    def _close_update_progress(self) -> None:
        if self._update_progress is not None:
            self._update_progress.close()
            self._update_progress.deleteLater()
            self._update_progress = None

    def _update_download_failed(self, message: str) -> None:
        self._close_update_progress()
        self._show_error(message)

    def _update_downloaded(self, archive: object) -> None:
        if self._update_progress is not None:
            self._update_progress.setRange(0, 0)
            self._update_progress.setLabelText(
                "Загрузка завершена. Запускается установка обновления…"
            )
        try:
            launch_updater(archive)  # type: ignore[arg-type]
        except Exception as exc:
            self._close_update_progress()
            self._show_error(str(exc))
            return
        self.close()

    def _run(self, function, on_success, on_failure=None) -> None:
        self._background_jobs += 1
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda result, current=worker: self._operation_finished(current, result, on_success)
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
        self.notification_timer.stop()
        self.learning_page.shutdown()
        self.communications_page.shutdown()
        self.pool.clear()
        self.pool.waitForDone(16000)
        self._workers.clear()
        self.api.close()
        super().closeEvent(event)
