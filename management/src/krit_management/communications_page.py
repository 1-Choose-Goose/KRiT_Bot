from __future__ import annotations

import html
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QDate, QSize, Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .timeutils import now_center, parse_center
from .widgets import SafeComboBox, SearchableComboBox
from .workers import Worker

EVENT_LABELS = {
    "schedule_published": "Расписание опубликовано",
    "schedule_changed": "Расписание изменено",
    "lesson_reminder": "Напоминание о занятиях",
    "lesson_confirmation_request": "Подтверждение посещения",
    "lesson_cancelled": "Занятие отменено",
    "lesson_started": "Занятие началось",
    "lesson_participant_started": "Ученик приступил к занятию",
    "lesson_finished": "Занятие завершено",
    "student_arrived_club": "Ученик пришёл в клуб",
    "student_left_club": "Ученик ушёл из клуба",
    "participant_added": "Ученик добавлен",
    "participant_removed": "Ученик исключён",
    "teacher_replaced": "Преподаватель заменён",
    "custom_message": "Пользовательское сообщение",
    "custom_yes_no_request": "Опрос Да / Нет",
    "registration_message": "Регистрация в MAX",
    "subscription_required": "Требуется подписка на канал",
}
CONTEXT_LABELS = {
    "*": "Все",
    "student": "Ученик",
    "guardian": "Родитель",
    "teacher": "Преподаватель",
}
ROLE_LABELS = {
    "student": "Ученик",
    "teacher": "Преподаватель",
    "parent": "Родитель",
    "guardian": "Родитель",
}
PRIORITY_LABELS = {"low": "Низкий", "normal": "Обычный", "high": "Высокий"}
EVENT_GROUPS = {
    "Сообщения": {
        "custom_message",
        "custom_yes_no_request",
        "registration_message",
        "subscription_required",
    },
    "Расписание": {
        "schedule_published",
        "schedule_changed",
        "lesson_cancelled",
        "teacher_replaced",
        "participant_added",
        "participant_removed",
    },
    "Занятия и посещение": {
        "lesson_reminder",
        "lesson_confirmation_request",
        "lesson_started",
        "lesson_participant_started",
        "lesson_finished",
        "student_arrived_club",
        "student_left_club",
    },
}
EVENT_DESCRIPTIONS = {
    "custom_message": "Ручные сообщения, которые администратор отправляет из программы.",
    "custom_yes_no_request": "Опрос с вариантами ответа «Да» и «Нет».",
    "schedule_published": "Уведомление о публикации расписания на выбранный период.",
    "schedule_changed": "Уведомление об изменениях уже опубликованного расписания.",
    "lesson_reminder": "Напоминание о предстоящем занятии.",
    "lesson_confirmation_request": "Запрос подтверждения посещения занятия.",
    "lesson_cancelled": "Сообщение об отмене занятия.",
    "teacher_replaced": "Сообщение о замене преподавателя.",
    "participant_added": "Сообщение о добавлении ученика в занятие.",
    "participant_removed": "Сообщение об исключении ученика из занятия.",
    "lesson_started": "Сообщение о начале занятия.",
    "lesson_participant_started": "Сообщение о том, что ученик приступил к занятию.",
    "lesson_finished": "Сообщение о завершении занятия.",
    "student_arrived_club": "Сообщение о приходе ученика в клуб.",
    "student_left_club": "Сообщение об уходе ученика из клуба.",
    "registration_message": "Сообщение при регистрации пользователя в MAX.",
    "subscription_required": "Просьба подписаться на обязательный канал.",
}
CAMPAIGN_TYPE_LABELS = {
    "manual_message": "Сообщение",
    "custom_poll": "Опрос Да / Нет",
    "schedule_publication": "Публикация расписания",
    "schedule_change": "Изменения расписания",
}
CAMPAIGN_STATUS_LABELS = {
    "draft": "Черновик",
    "scheduled": "В очереди",
    "sending": "Отправляется",
    "completed": "Завершена",
    "partial": "Выполнена частично",
    "failed": "Ошибка",
}
JOB_STATUS_LABELS = {
    "pending": "ожидает",
    "processing": "отправляется",
    "sent": "доставлено",
    "retry": "повтор",
    "failed": "ошибка",
    "cancelled": "отменено",
}
DELIVERY_STATUS_LABELS = {
    "received": "получено",
    "pending": "ожидает отправки",
    "sending": "отправляется",
    "sent": "доставлено",
    "failed": "ошибка доставки",
    "unavailable": "MAX недоступен",
}
ANSWER_LABELS = {"yes": "Да", "no": "Нет", "partial": "По занятиям"}
MESSAGE_TYPE_LABELS = {
    "command": "Команда боту",
    "interaction_callback": "Взаимодействие с ботом",
    "interaction_reason": "Ответ на запрос бота",
}
PERSON_SEARCH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_LABELS = {
    "pending": "Ожидается ответ",
    "confirmed": "Посещение подтверждено",
    "declined": "Посещение отклонено",
    "no_response": "Ответ не получен",
    "needs_reconfirmation": "Нужно подтвердить повторно",
    "conflict": "Ответы расходятся",
}


def _button(text: str, callback: Callable[[], None], *, primary: bool = False) -> QPushButton:
    result = QPushButton(text)
    result.setProperty("kind", "primary" if primary else "secondary")
    result.clicked.connect(callback)
    return result


def _table(headers: list[str]) -> QTableWidget:
    result = QTableWidget(0, len(headers))
    result.setHorizontalHeaderLabels(headers)
    result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    result.verticalHeader().setVisible(False)
    result.horizontalHeader().setStretchLastSection(True)
    return result


def _dialog_preview(full_name: str, value: object) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    lines = [line for line in lines if line.casefold() != full_name.strip().casefold()]
    if not lines:
        return "Нет сообщений"
    first = lines[0]
    lowered = first.casefold()
    if lowered.startswith("ваши занятия") or lowered.startswith("занятия "):
        return "Расписание занятий"
    return " ".join(lines)[:72]


def _dialog_time(value: object) -> str:
    if not value:
        return ""
    try:
        moment = parse_center(value)
    except (TypeError, ValueError):
        return ""
    return moment.strftime("%H:%M" if moment.date() == now_center().date() else "%d.%m")


def _answer_people(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Не назначен"
    return "\n".join(
        f"{item.get('name', '—')} — {ANSWER_LABELS.get(str(item.get('answer')), 'нет ответа')}"
        for item in items
    )


class PollDetailsDialog(QDialog):
    def __init__(self, details: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Результаты опроса · КРиТ")
        self.resize(980, 680)
        layout = QVBoxLayout(self)
        title = QLabel(str(details.get("title", "Опрос")))
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        counts = details.get("counts") or {}
        summary = QLabel(
            f"Получателей: {counts.get('recipients', 0)} · "
            f"Да: {counts.get('yes', 0)} · Нет: {counts.get('no', 0)} · "
            f"Без ответа: {counts.get('no_response', 0)}"
        )
        summary.setObjectName("supportingText")
        layout.addWidget(summary)
        layout.addWidget(QLabel("Ответы получателей"))
        self.recipients = _table(
            ["ФИО", "Роли", "Доставка", "Ответ", "Время ответа"]
        )
        recipient_header = self.recipients.horizontalHeader()
        recipient_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            recipient_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        recipients = list(details.get("recipients") or [])
        self.recipients.setRowCount(len(recipients))
        for row, item in enumerate(recipients):
            answer = ANSWER_LABELS.get(str(item.get("answer")), "—")
            raw_delivery = str(item.get("delivery_status", ""))
            if item.get("answer"):
                delivery = "Ответ получен"
            elif raw_delivery == "sent":
                delivery = "Доставлено, ответа нет"
            else:
                delivery = JOB_STATUS_LABELS.get(raw_delivery, "Недоступно")
            answered_at = "—"
            if item.get("answered_at"):
                try:
                    answered_at = parse_center(item["answered_at"]).strftime("%d.%m.%Y %H:%M")
                except (TypeError, ValueError):
                    answered_at = str(item["answered_at"])
            values = [
                item.get("full_name", ""),
                ", ".join(
                    ROLE_LABELS.get(str(role), "Другая роль")
                    for role in (item.get("roles") or [])
                ),
                delivery,
                answer,
                answered_at,
            ]
            for column, value in enumerate(values):
                self.recipients.setItem(row, column, QTableWidgetItem(str(value)))
        layout.addWidget(self.recipients, 2)
        layout.addWidget(QLabel("Согласование ответов ученика, родителя и преподавателя"))
        self.agreements = _table(
            [
                "Ученик",
                "Ответ ученика",
                "Ответы родителей",
                "Ответы преподавателей",
                "Итог",
            ]
        )
        agreement_header = self.agreements.horizontalHeader()
        agreement_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        agreement_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        agreement_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        agreements = list(details.get("agreements") or [])
        self.agreements.setRowCount(len(agreements))
        agreement_labels = {
            "agreed": "Согласовано",
            "conflict": "Ответы расходятся",
            "waiting": "Ожидается ответ",
        }
        for row, item in enumerate(agreements):
            values = [
                item.get("student_name", ""),
                ANSWER_LABELS.get(str(item.get("student_answer")), "—"),
                _answer_people(list(item.get("guardians") or [])),
                _answer_people(list(item.get("teachers") or [])),
                agreement_labels.get(str(item.get("result")), "—"),
            ]
            for column, value in enumerate(values):
                self.agreements.setItem(row, column, QTableWidgetItem(str(value)))
            self.agreements.resizeRowToContents(row)
        self.agreements.setMaximumHeight(210)
        layout.addWidget(self.agreements, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CommunicationsPage(QWidget):
    def __init__(self, api: ManagementApi, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self.people: list[dict[str, Any]] = []
        self.selected_recipient_ids: set[int] = set()
        self.chat_messages: list[dict[str, Any]] = []
        self.pending_replies: dict[int, list[dict[str, Any]]] = {}
        self.rules: list[dict[str, Any]] = []
        self.confirmation_rows: list[dict[str, Any]] = []
        self.person_settings: dict[str, Any] = {}
        self.person_rule_rows: list[dict[str, Any]] = []
        self.current_person_id: int | None = None
        self.pending_settings_person_id: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("clientTabs")
        self.tabs.addTab(self._send_tab(), "Отправить")
        self.tabs.addTab(self._dialogs_tab(), "Диалоги")
        self.tabs.addTab(self._confirmations_tab(), "Подтверждения")
        self.tabs.addTab(self._settings_tab(), "Настройки")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        layout.addWidget(self.tabs)
        self.timer = QTimer(self)
        self.timer.setInterval(10_000)
        self.timer.timeout.connect(self._auto_refresh)
        self.timer.start()

    def _send_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.recipient_search = QLineEdit()
        self.recipient_search.setPlaceholderText("Поиск получателя по ФИО или телефону")
        self.recipient_search.textChanged.connect(self._render_recipients)
        top.addWidget(self.recipient_search, 1)
        top.addWidget(_button("Снять выбор", self._clear_recipient_selection))
        self.selected_count = QLabel("Выбрано: 0")
        top.addWidget(self.selected_count)
        layout.addLayout(top)
        self.recipients = _table(["Выбрать", "ФИО", "Роли", "Телефон", "MAX"])
        recipient_header = self.recipients.horizontalHeader()
        recipient_header.setStretchLastSection(False)
        recipient_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        recipient_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        recipient_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        recipient_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        recipient_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.recipients.setColumnWidth(2, 190)
        self.recipients.setColumnWidth(3, 165)
        self.recipients.setColumnWidth(4, 145)
        self.recipients.itemChanged.connect(self._recipient_item_changed)
        self.recipients.cellDoubleClicked.connect(self._recipient_cell_clicked)
        layout.addWidget(self.recipients, 2)
        self.message_text = QTextEdit()
        self.message_text.setPlaceholderText("Введите сообщение (до 4000 символов)")
        self.message_text.setMaximumHeight(120)
        layout.addWidget(self.message_text)
        actions = QHBoxLayout()
        self.urgent = QCheckBox("Срочное сообщение")
        actions.addWidget(self.urgent)
        actions.addStretch(1)
        actions.addWidget(_button("Опрос Да / Нет", self.send_poll))
        actions.addWidget(_button("Отправить", self.send_message, primary=True))
        layout.addLayout(actions)

        schedule = QHBoxLayout()
        schedule_hint = QLabel(
            "Публикация расписания и история изменений находятся в отдельном окне."
        )
        schedule_hint.setWordWrap(True)
        schedule.addWidget(schedule_hint, 1)
        self.open_schedule_button = _button(
            "Публикация расписания…", self.open_schedule_publication
        )
        schedule.addWidget(self.open_schedule_button)
        layout.addLayout(schedule)
        self.schedule_dialog = self._build_schedule_publication_dialog()
        return page

    def _build_schedule_publication_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("Публикация расписания — КРиТ")
        dialog.setModal(False)
        dialog.setMinimumSize(900, 480)
        dialog.resize(1120, 600)
        layout = QVBoxLayout(dialog)

        heading = QLabel("Публикация расписания")
        heading.setObjectName("schedulePublicationHeading")
        layout.addWidget(heading)
        description = QLabel(
            "Выберите период, сначала проверьте изменения, затем опубликуйте их. "
            "Ниже отображается история публикаций и ошибок доставки."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        schedule = QHBoxLayout()
        schedule.addWidget(QLabel("Период"))
        self.publish_from = QDateEdit(QDate.currentDate())
        self.publish_from.setCalendarPopup(True)
        self.publish_from.setDisplayFormat("dd.MM.yyyy")
        self.publish_from.setMinimumWidth(140)
        self.publish_to = QDateEdit(QDate.currentDate().addDays(7))
        self.publish_to.setCalendarPopup(True)
        self.publish_to.setDisplayFormat("dd.MM.yyyy")
        self.publish_to.setMinimumWidth(140)
        schedule.addWidget(self.publish_from)
        schedule.addWidget(QLabel("—"))
        schedule.addWidget(self.publish_to)
        schedule.addWidget(_button("Проверить изменения", self.preview_schedule))
        schedule.addWidget(_button("Опубликовать", self.publish_schedule, primary=True))
        schedule.addStretch(1)
        layout.addLayout(schedule)

        history_label = QLabel("История публикаций")
        history_label.setObjectName("schedulePublicationHistoryHeading")
        layout.addWidget(history_label)
        self.campaigns = _table(
            ["Дата", "Тип", "Сообщение", "Статус", "Результат", "Действия"]
        )
        campaign_header = self.campaigns.horizontalHeader()
        campaign_header.setStretchLastSection(False)
        campaign_header.setMinimumSectionSize(100)
        for column, width in ((0, 155), (1, 190), (3, 175), (5, 170)):
            campaign_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.campaigns.setColumnWidth(column, width)
        campaign_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        campaign_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.campaigns.setWordWrap(True)
        self.campaigns.setAlternatingRowColors(True)
        self.campaigns.verticalHeader().setDefaultSectionSize(44)
        self.campaigns.cellDoubleClicked.connect(self._campaign_double_clicked)
        layout.addWidget(self.campaigns, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(dialog.hide)
        layout.addWidget(buttons)
        return dialog

    def open_schedule_publication(self) -> None:
        self.schedule_dialog.show()
        self.schedule_dialog.raise_()
        self.schedule_dialog.activateWindow()
        self._run(self.api.communication_campaigns, self._campaigns_loaded)

    def _dialogs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.dialog_search = QLineEdit()
        self.dialog_search.setPlaceholderText("Поиск диалога")
        self.dialog_search.returnPressed.connect(self.load_dialogs)
        layout.addWidget(self.dialog_search)
        splitter = QSplitter()
        self.dialogs = QListWidget()
        self.dialogs.currentItemChanged.connect(self._dialog_selected)
        splitter.addWidget(self.dialogs)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.chat_title = QLabel("Выберите диалог")
        self.chat_title.setObjectName("sectionTitle")
        right_layout.addWidget(self.chat_title)
        self.chat_history = QTextBrowser()
        right_layout.addWidget(self.chat_history, 1)
        reply = QHBoxLayout()
        self.reply_text = QLineEdit()
        self.reply_text.setPlaceholderText("Ответить клиенту")
        self.reply_text.returnPressed.connect(self.send_reply)
        reply.addWidget(self.reply_text, 1)
        self.reply_button = _button("Отправить", self.send_reply, primary=True)
        reply.addWidget(self.reply_button)
        right_layout.addLayout(reply)
        splitter.addWidget(right)
        splitter.setSizes([320, 700])
        layout.addWidget(splitter)
        return page

    def _confirmations_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Период"))
        self.confirm_from = QDateEdit(QDate.currentDate())
        self.confirm_from.setCalendarPopup(True)
        self.confirm_from.setDisplayFormat("dd.MM.yyyy")
        self.confirm_from.setMinimumWidth(140)
        self.confirm_to = QDateEdit(QDate.currentDate().addDays(7))
        self.confirm_to.setCalendarPopup(True)
        self.confirm_to.setDisplayFormat("dd.MM.yyyy")
        self.confirm_to.setMinimumWidth(140)
        filters.addWidget(self.confirm_from)
        filters.addWidget(self.confirm_to)
        filters.addWidget(_button("Показать", self.load_confirmations))
        self.attention_only = QCheckBox("Требуют уточнения")
        self.attention_only.toggled.connect(self._render_confirmations)
        filters.addWidget(self.attention_only)
        filters.addStretch(1)
        layout.addLayout(filters)
        self.confirmations = _table(
            [
                "Ученик",
                "Занятие",
                "Отправлено",
                "Ответ ученика",
                "Ответ родителя",
                "Итог",
                "Причина",
            ]
        )
        self.confirmations.setAlternatingRowColors(True)
        self.confirmations.setWordWrap(True)
        self.confirmations.verticalHeader().setDefaultSectionSize(42)
        confirmations_header = self.confirmations.horizontalHeader()
        confirmations_header.setStretchLastSection(False)
        confirmations_header.setMinimumSectionSize(90)
        for column in (0, 1, 5, 6):
            confirmations_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        for column, width in ((2, 105), (3, 125), (4, 135)):
            confirmations_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.confirmations.setColumnWidth(column, width)
        layout.addWidget(self.confirmations)
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        title = QLabel("Настройки уведомлений")
        title.setObjectName("settingsTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)
        help_label = QLabel(
            "Выберите событие и настройте уведомления для родителей, учеников и "
            "преподавателей. Персональные исключения изменяют правила только для "
            "конкретного человека."
        )
        help_label.setObjectName("supportingText")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        mode_bar = QFrame()
        mode_bar.setObjectName("settingsModeBar")
        mode_bar.setStyleSheet(
            "QFrame#settingsModeBar { background: #eef3fb; border: 1px solid #d7e0ec; "
            "border-radius: 8px; }"
            "QFrame#settingsModeBar QPushButton { background: transparent; color: #52627a; "
            "border: 0; border-radius: 6px; padding: 7px 16px; }"
            "QFrame#settingsModeBar QPushButton:checked { background: white; color: #164db3; "
            "font-weight: 700; }"
        )
        mode_layout = QHBoxLayout(mode_bar)
        mode_layout.setContentsMargins(3, 3, 3, 3)
        mode_layout.setSpacing(3)
        self.settings_mode_general = QPushButton("Общие правила")
        self.settings_mode_person = QPushButton("Персональные исключения")
        for button in (self.settings_mode_general, self.settings_mode_person):
            button.setCheckable(True)
            mode_layout.addWidget(button)
        mode_layout.addStretch(1)
        self.settings_mode_general.setChecked(True)
        layout.addWidget(mode_bar)

        self.settings_mode_pages = QStackedWidget()
        self.settings_mode_general.clicked.connect(
            lambda: self._set_settings_mode(0)
        )
        self.settings_mode_person.clicked.connect(lambda: self._set_settings_mode(1))
        layout.addWidget(self.settings_mode_pages, 1)

        general_page = QWidget()
        general_layout = QVBoxLayout(general_page)
        general_layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        event_panel = QFrame()
        event_panel.setObjectName("settingsEventPanel")
        event_panel.setStyleSheet(
            "QFrame#settingsEventPanel { background: #f8faff; border: 1px solid #e1e7f0; "
            "border-radius: 8px; }"
        )
        event_layout = QVBoxLayout(event_panel)
        event_layout.setContentsMargins(10, 10, 10, 10)
        event_heading = QLabel("События")
        event_heading.setStyleSheet("font-weight: 700;")
        event_layout.addWidget(event_heading)
        self.settings_event_search = QLineEdit()
        self.settings_event_search.setPlaceholderText("Найти событие")
        self.settings_event_search.setClearButtonEnabled(True)
        self.settings_event_search.textChanged.connect(self._render_settings_events)
        event_layout.addWidget(self.settings_event_search)
        self.settings_events = QListWidget()
        self.settings_events.setAlternatingRowColors(False)
        self.settings_events.currentItemChanged.connect(self._settings_event_selected)
        event_layout.addWidget(self.settings_events, 1)
        splitter.addWidget(event_panel)

        editor = QFrame()
        editor.setObjectName("settingsEditor")
        editor.setStyleSheet(
            "QFrame#settingsEditor { background: white; border: 1px solid #e1e7f0; "
            "border-radius: 8px; }"
        )
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(16, 14, 16, 14)
        editor_layout.setSpacing(10)
        self.settings_editor_title = QLabel("Выберите событие")
        self.settings_editor_title.setStyleSheet("font-size: 17px; font-weight: 700;")
        editor_layout.addWidget(self.settings_editor_title)
        self.settings_editor_description = QLabel(
            "Слева выберите событие, для которого нужно изменить правила."
        )
        self.settings_editor_description.setObjectName("supportingText")
        self.settings_editor_description.setWordWrap(True)
        editor_layout.addWidget(self.settings_editor_description)

        roles_heading = QLabel("Кому и когда отправлять")
        roles_heading.setStyleSheet("font-weight: 700;")
        editor_layout.addWidget(roles_heading)
        roles_help = QLabel(
            "«Сразу» задаётся пустым полем. Тихие часы применяются только к несрочным сообщениям."
        )
        roles_help.setObjectName("supportingText")
        roles_help.setWordWrap(True)
        editor_layout.addWidget(roles_help)

        role_grid = QGridLayout()
        role_grid.setHorizontalSpacing(8)
        role_grid.setVerticalSpacing(8)
        for column, text in enumerate(
            (
                "Получатель",
                "Включено",
                "За сколько, мин",
                "Приоритет",
                "Тихие часы с",
                "до",
                "Повтор",
            )
        ):
            heading = QLabel(text)
            heading.setObjectName("supportingText")
            role_grid.addWidget(heading, 0, column)
            if text == "Повтор":
                self.settings_repeat_heading = heading
        self.settings_role_controls: dict[str, dict[str, Any]] = {}
        for row, context in enumerate(("guardian", "student", "teacher"), start=1):
            role_name = QLabel(CONTEXT_LABELS[context])
            role_name.setStyleSheet("font-weight: 600;")
            enabled = QCheckBox("Получать")
            offset = QLineEdit()
            offset.setPlaceholderText("Сразу")
            offset.setMaximumWidth(115)
            priority = SafeComboBox()
            for priority_value in ("low", "normal", "high"):
                priority.addItem(PRIORITY_LABELS[priority_value], priority_value)
            quiet_start = QLineEdit()
            quiet_start.setPlaceholderText("22:00")
            quiet_start.setMaximumWidth(90)
            quiet_end = QLineEdit()
            quiet_end.setPlaceholderText("08:00")
            quiet_end.setMaximumWidth(90)
            follow_up = SafeComboBox()
            follow_up.addItem("Без повтора", "none")
            follow_up.addItem("Один повтор", "once")
            follow_offset = QLineEdit()
            follow_offset.setPlaceholderText("180 мин")
            follow_offset.setMaximumWidth(90)
            repeat = QWidget()
            repeat_layout = QHBoxLayout(repeat)
            repeat_layout.setContentsMargins(0, 0, 0, 0)
            repeat_layout.setSpacing(5)
            repeat_layout.addWidget(follow_up)
            repeat_layout.addWidget(follow_offset)
            role_grid.addWidget(role_name, row, 0)
            role_grid.addWidget(enabled, row, 1)
            role_grid.addWidget(offset, row, 2)
            role_grid.addWidget(priority, row, 3)
            role_grid.addWidget(quiet_start, row, 4)
            role_grid.addWidget(quiet_end, row, 5)
            role_grid.addWidget(repeat, row, 6)
            self.settings_role_controls[context] = {
                "name": role_name,
                "enabled": enabled,
                "offset": offset,
                "priority": priority,
                "quiet_start": quiet_start,
                "quiet_end": quiet_end,
                "follow_up": follow_up,
                "follow_offset": follow_offset,
                "repeat": repeat,
            }
        editor_layout.addLayout(role_grid)
        editor_layout.addStretch(1)
        editor_actions = QHBoxLayout()
        editor_actions.addStretch(1)
        editor_actions.addWidget(
            _button("Сохранить общие правила", self.save_settings, primary=True)
        )
        editor_layout.addLayout(editor_actions)
        splitter.addWidget(editor)
        splitter.setSizes([300, 760])
        general_layout.addWidget(splitter)
        self.settings_mode_pages.addWidget(general_page)

        individual_page = QWidget()
        individual_layout = QVBoxLayout(individual_page)
        individual_layout.setContentsMargins(0, 0, 0, 0)
        individual_help = QLabel(
            "Выберите человека. Здесь можно включить или отключить отдельные события, "
            "не меняя общие правила для остальных получателей."
        )
        individual_help.setObjectName("supportingText")
        individual_help.setWordWrap(True)
        individual_layout.addWidget(individual_help)
        individual = QHBoxLayout()
        individual.addWidget(QLabel("Человек"))
        self.settings_person = SearchableComboBox(
            placeholder="Введите фамилию, имя или телефон…"
        )
        self.settings_person.setSearchRole(PERSON_SEARCH_ROLE)
        self.settings_person.currentIndexChanged.connect(self.load_person_settings)
        individual.addWidget(self.settings_person, 2)
        individual.addWidget(QLabel("Контекст"))
        self.settings_target = SafeComboBox()
        self.settings_target.currentIndexChanged.connect(self._render_person_settings)
        individual.addWidget(self.settings_target, 1)
        individual_layout.addLayout(individual)

        self.person_empty_state = QFrame()
        self.person_empty_state.setObjectName("personEmptyState")
        self.person_empty_state.setStyleSheet(
            "QFrame#personEmptyState { background: #f8faff; border: 1px dashed #cdd8e8; "
            "border-radius: 8px; }"
        )
        empty_layout = QVBoxLayout(self.person_empty_state)
        empty_layout.addStretch(1)
        empty_title = QLabel("Выберите человека")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        empty_layout.addWidget(empty_title)
        empty_text = QLabel(
            "После выбора появятся только доступные для него события и контексты."
        )
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text.setObjectName("supportingText")
        empty_layout.addWidget(empty_text)
        empty_layout.addStretch(1)
        individual_layout.addWidget(self.person_empty_state, 1)

        self.settings = QTableWidget(0, 9)
        self.settings.setHorizontalHeaderLabels(
            [
                "Событие",
                "Получатель",
                "За сколько, мин",
                "Включено",
                "Приоритет",
                "Тихие часы с",
                "до",
                "Если нет ответа",
                "Повтор за, мин",
            ]
        )
        self.settings.verticalHeader().setVisible(False)
        self.settings.verticalHeader().setDefaultSectionSize(40)
        settings_header = self.settings.horizontalHeader()
        settings_header.setStretchLastSection(False)
        for column in range(self.settings.columnCount()):
            settings_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        for column, width in enumerate((250, 125, 130, 95, 135, 115, 85, 160, 130)):
            self.settings.setColumnWidth(column, width)
        self.settings.hide()
        self.person_overrides = QTableWidget(0, 4)
        self.person_overrides.setHorizontalHeaderLabels(
            ["Событие", "Контекст", "За сколько, мин", "Режим"]
        )
        self.person_overrides.verticalHeader().setVisible(False)
        self.person_overrides.verticalHeader().setDefaultSectionSize(40)
        person_header = self.person_overrides.horizontalHeader()
        person_header.setStretchLastSection(False)
        person_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        person_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        person_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        person_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.person_overrides.setColumnWidth(1, 145)
        self.person_overrides.setColumnWidth(2, 140)
        self.person_overrides.setColumnWidth(3, 250)
        individual_layout.addWidget(self.person_overrides, 1)
        person_actions = QHBoxLayout()
        person_actions.addStretch(1)
        self.save_person_settings_button = _button(
            "Сохранить индивидуальные настройки", self.save_person_settings, primary=True
        )
        person_actions.addWidget(self.save_person_settings_button)
        individual_layout.addLayout(person_actions)
        self.settings_mode_pages.addWidget(individual_page)
        self._set_settings_mode(0)
        self._render_person_settings()
        return page

    def _set_settings_mode(self, index: int) -> None:
        self.settings_mode_general.setChecked(index == 0)
        self.settings_mode_person.setChecked(index == 1)
        self.settings_mode_pages.setCurrentIndex(index)

    def _render_settings_events(self) -> None:
        if not hasattr(self, "settings_events"):
            return
        self._store_current_settings_editor()
        selected_code = getattr(self, "_current_settings_event", None)
        query = self.settings_event_search.text().strip().casefold()
        available = {str(rule.get("event_code")) for rule in self.rules}
        self.settings_events.blockSignals(True)
        self.settings_events.clear()
        selected_item: QListWidgetItem | None = None
        first_item: QListWidgetItem | None = None
        grouped_codes = set().union(*EVENT_GROUPS.values())
        groups = list(EVENT_GROUPS.items())
        other_codes = available - grouped_codes
        if other_codes:
            groups.append(("Другие события", other_codes))
        for group_name, group_codes in groups:
            codes = sorted(
                (
                    code
                    for code in available & group_codes
                    if not query or query in EVENT_LABELS.get(code, code).casefold()
                ),
                key=lambda code: EVENT_LABELS.get(code, code),
            )
            if not codes:
                continue
            group_item = QListWidgetItem(group_name)
            group_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.settings_events.addItem(group_item)
            for code in codes:
                item = QListWidgetItem(EVENT_LABELS.get(code, "Другое событие"))
                item.setData(Qt.ItemDataRole.UserRole, code)
                self.settings_events.addItem(item)
                first_item = first_item or item
                if code == selected_code:
                    selected_item = item
        self.settings_events.blockSignals(False)
        target = selected_item or first_item
        if target is not None:
            self.settings_events.setCurrentItem(target)
        else:
            self._current_settings_event = None
            self.settings_editor_title.setText("События не найдены")
            self.settings_editor_description.setText(
                "Измените поисковый запрос или обновите список правил."
            )

    def _settings_event_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        event_code = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if not event_code:
            return
        self._store_current_settings_editor()
        self._current_settings_event = str(event_code)
        self._populate_settings_editor(str(event_code))

    def _store_current_settings_editor(self) -> None:
        event_code = getattr(self, "_current_settings_event", None)
        if not event_code or not hasattr(self, "settings_role_controls"):
            return
        for context, controls in self.settings_role_controls.items():
            rule_row = next(
                (
                    (row, rule)
                    for row, rule in enumerate(self.rules)
                    if rule.get("event_code") == event_code
                    and rule.get("recipient_context") == context
                ),
                None,
            )
            if rule_row is None:
                continue
            row, rule = rule_row
            offset_text = controls["offset"].text().strip()
            try:
                offset = int(offset_text) if offset_text else -1
            except ValueError:
                offset = int(rule.get("offset_minutes", -1))
            rule["offset_minutes"] = offset
            rule["enabled"] = controls["enabled"].isChecked()
            rule["priority"] = controls["priority"].currentData() or "normal"
            rule["quiet_start"] = controls["quiet_start"].text().strip() or None
            rule["quiet_end"] = controls["quiet_end"].text().strip() or None
            configuration = dict(rule.get("configuration") or {})
            if event_code == "lesson_confirmation_request":
                configuration["follow_up"] = controls["follow_up"].currentData() or "once"
                follow_text = controls["follow_offset"].text().strip()
                try:
                    configuration["follow_up_offset_minutes"] = max(
                        61, int(follow_text) if follow_text else 180
                    )
                except ValueError:
                    configuration["follow_up_offset_minutes"] = 180
            rule["configuration"] = configuration
            self.settings.item(row, 2).setText("Сразу" if offset < 0 else str(offset))
            self.settings.item(row, 3).setCheckState(
                Qt.CheckState.Checked if rule["enabled"] else Qt.CheckState.Unchecked
            )
            priority = self.settings.cellWidget(row, 4)
            if isinstance(priority, QComboBox):
                priority.setCurrentIndex(max(0, priority.findData(rule["priority"])))
            self.settings.item(row, 5).setText(str(rule["quiet_start"] or ""))
            self.settings.item(row, 6).setText(str(rule["quiet_end"] or ""))
            if event_code == "lesson_confirmation_request":
                follow_up = self.settings.cellWidget(row, 7)
                if isinstance(follow_up, QComboBox):
                    follow_up.setCurrentIndex(
                        max(0, follow_up.findData(configuration["follow_up"]))
                    )
                self.settings.item(row, 8).setText(
                    str(configuration["follow_up_offset_minutes"])
                )

    def _populate_settings_editor(self, event_code: str) -> None:
        self.settings_editor_title.setText(EVENT_LABELS.get(event_code, "Другое событие"))
        self.settings_editor_description.setText(
            EVENT_DESCRIPTIONS.get(
                event_code,
                "Настройте получателей, время отправки и приоритет этого уведомления.",
            )
        )
        is_confirmation = event_code == "lesson_confirmation_request"
        self.settings_repeat_heading.setVisible(is_confirmation)
        for context, controls in self.settings_role_controls.items():
            rule = next(
                (
                    item
                    for item in self.rules
                    if item.get("event_code") == event_code
                    and item.get("recipient_context") == context
                ),
                None,
            )
            available = rule is not None
            for name in (
                "enabled",
                "offset",
                "priority",
                "quiet_start",
                "quiet_end",
                "follow_up",
                "follow_offset",
            ):
                controls[name].setEnabled(available)
            controls["repeat"].setVisible(is_confirmation)
            if rule is None:
                controls["enabled"].setChecked(False)
                controls["offset"].clear()
                controls["quiet_start"].clear()
                controls["quiet_end"].clear()
                continue
            offset = int(rule.get("offset_minutes", -1))
            controls["enabled"].setChecked(bool(rule.get("enabled")))
            controls["offset"].setText("" if offset < 0 else str(offset))
            controls["priority"].setCurrentIndex(
                max(0, controls["priority"].findData(str(rule.get("priority", "normal"))))
            )
            controls["quiet_start"].setText(str(rule.get("quiet_start") or ""))
            controls["quiet_end"].setText(str(rule.get("quiet_end") or ""))
            configuration = rule.get("configuration") or {}
            controls["follow_up"].setCurrentIndex(
                max(0, controls["follow_up"].findData(configuration.get("follow_up", "once")))
            )
            controls["follow_offset"].setText(
                str(configuration.get("follow_up_offset_minutes", 180))
            )

    def _run(
        self,
        fn: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
    ) -> None:
        if self._closing:
            return
        worker = Worker(fn)
        self._workers.add(worker)

        def finished(result: object) -> None:
            self._workers.discard(worker)
            if not self._closing and done:
                done(result)

        def failed(message: str) -> None:
            self._workers.discard(worker)
            if not self._closing:
                QMessageBox.critical(self, "Ошибка", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def _auto_refresh(self) -> None:
        if self.isVisible() and not self._workers:
            self.refresh()

    def refresh(self) -> None:
        if self.tabs.currentIndex() == 0:
            self._run(self.api.people, self._people_loaded)
            if self.schedule_dialog.isVisible():
                self._run(self.api.communication_campaigns, self._campaigns_loaded)
        elif self.tabs.currentIndex() == 1:
            self.load_dialogs()
        elif self.tabs.currentIndex() == 2:
            self.load_confirmations()
        else:
            self._run(self.api.communication_global_settings, self._settings_loaded)
            self._run(self.api.people, self._settings_people_loaded)

    def _people_loaded(self, value: object) -> None:
        self.people = list(value) if isinstance(value, list) else []
        self._render_recipients()

    def _render_recipients(self) -> None:
        query = self.recipient_search.text().strip().casefold()
        rows = [
            person
            for person in self.people
            if not query
            or query in str(person.get("full_name", "")).casefold()
            or query in str(person.get("phone", "")).casefold()
        ]
        self.recipients.blockSignals(True)
        self.recipients.setRowCount(len(rows))
        for row, person in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(
                Qt.CheckState.Checked
                if int(person["id"]) in self.selected_recipient_ids
                else Qt.CheckState.Unchecked
            )
            check.setData(Qt.ItemDataRole.UserRole, int(person["id"]))
            values = [
                check,
                QTableWidgetItem(str(person.get("full_name", ""))),
                QTableWidgetItem(
                    ", ".join(
                        ROLE_LABELS.get(str(role), "Другая роль")
                        for role in (person.get("roles") or [])
                    )
                ),
                QTableWidgetItem(str(person.get("phone", ""))),
                QTableWidgetItem("Доступен" if person.get("max_user_id") else "Не подключён"),
            ]
            for column, item in enumerate(values):
                self.recipients.setItem(row, column, item)
        self.recipients.blockSignals(False)
        self._update_selected_count()

    def _recipient_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        person_id = item.data(Qt.ItemDataRole.UserRole)
        if person_id is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_recipient_ids.add(int(person_id))
        else:
            self.selected_recipient_ids.discard(int(person_id))
        self._update_selected_count()

    def _recipient_cell_clicked(self, row: int, column: int) -> None:
        if column == 0:
            return
        item = self.recipients.item(row, 0)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def _clear_recipient_selection(self) -> None:
        self.selected_recipient_ids.clear()
        self._render_recipients()

    def _selected_ids(self) -> list[int]:
        return sorted(self.selected_recipient_ids)

    def _update_selected_count(self) -> None:
        self.selected_count.setText(f"Выбрано: {len(self._selected_ids())}")

    def _message_payload(self) -> tuple[list[int], str] | None:
        ids, text = self._selected_ids(), self.message_text.toPlainText().strip()
        if not ids:
            QMessageBox.information(self, "Рассылка", "Выберите хотя бы одного получателя.")
            return None
        if not text:
            QMessageBox.information(self, "Рассылка", "Введите текст сообщения.")
            return None
        return ids, text

    def preview_message(self) -> None:
        values = self._message_payload()
        if values:
            self._run(
                lambda: self.api.communication_send(*values, preview=True),
                lambda result: QMessageBox.information(
                    self,
                    "Предпросмотр",
                    f"Получателей: {result.get('recipients', 0)}\n"
                    f"Доступны в MAX: {result.get('available', 0)}\n"
                    f"Недоступны: {len(result.get('unavailable', []))}",
                ),
            )

    def send_message(self) -> None:
        values = self._message_payload()
        if values:
            self._run(
                lambda: self.api.communication_send(*values, urgent=self.urgent.isChecked()),
                self._sent,
            )

    def send_poll(self) -> None:
        values = self._message_payload()
        if values:
            self._run(lambda: self.api.communication_poll(*values), self._sent)

    def _sent(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        self.message_text.clear()
        QMessageBox.information(
            self,
            "Рассылка создана",
            f"Поставлено в очередь: {data.get('available', data.get('queued', 0))}",
        )
        self.refresh()

    def _campaigns_loaded(self, value: object) -> None:
        rows = list(value) if isinstance(value, list) else []
        self.campaigns.clearContents()
        self.campaigns.setRowCount(len(rows))
        for row, item in enumerate(rows):
            counts = item.get("counts") or {}
            poll = item.get("poll") or {}
            ordered_statuses = ["sent", "pending", "processing", "retry", "failed", "cancelled"]
            ordered_statuses.extend(
                str(key) for key in counts if str(key) not in ordered_statuses
            )
            outcome = ", ".join(
                f"{JOB_STATUS_LABELS.get(key, 'неизвестно').capitalize()}: {counts[key]}"
                for key in ordered_statuses
                if key in counts
            )
            if poll:
                outcome += (
                    f"; Да: {poll.get('yes', 0)}, Нет: {poll.get('no', 0)}, "
                    f"без ответа: {poll.get('no_response', 0)}"
                )
            try:
                created_at = parse_center(item.get("created_at", "")).strftime(
                    "%d.%m.%Y %H:%M"
                )
            except (TypeError, ValueError):
                created_at = str(item.get("created_at", ""))
            values = [
                created_at,
                CAMPAIGN_TYPE_LABELS.get(
                    str(item.get("type", "")), "Рассылка"
                ),
                str(item.get("title", "")),
                CAMPAIGN_STATUS_LABELS.get(
                    str(item.get("status", "")), "Неизвестно"
                ),
                outcome,
            ]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, int(item["id"]))
                    cell.setData(Qt.ItemDataRole.UserRole + 1, str(item.get("type", "")))
                self.campaigns.setItem(row, column, cell)
            actions: list[QPushButton] = []
            if item.get("type") == "custom_poll":
                actions.append(
                    _button(
                        "Открыть",
                        lambda campaign_id=int(item["id"]): self.open_poll(campaign_id),
                    )
                )
            if counts.get("failed"):
                retry_button = _button(
                    "Повторить",
                    lambda campaign_id=int(item["id"]): self._run(
                        lambda: self.api.retry_communication_campaign(campaign_id),
                        lambda _result: self.refresh(),
                    ),
                )
                retry_button.setToolTip("Повторить сообщения, завершившиеся ошибкой")
                actions.append(retry_button)
            if actions:
                container = QWidget()
                action_layout = QHBoxLayout(container)
                action_layout.setContentsMargins(4, 2, 4, 2)
                action_layout.setSpacing(5)
                for button in actions:
                    button.setProperty("density", "compact")
                    action_layout.addWidget(button)
                action_layout.addStretch(1)
                self.campaigns.setCellWidget(row, 5, container)
        self.campaigns.resizeRowsToContents()
        for row in range(self.campaigns.rowCount()):
            self.campaigns.setRowHeight(row, max(44, self.campaigns.rowHeight(row)))

    def _campaign_double_clicked(self, row: int, _column: int) -> None:
        item = self.campaigns.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole + 1) == "custom_poll":
            self.open_poll(int(item.data(Qt.ItemDataRole.UserRole)))

    def open_poll(self, campaign_id: int) -> None:
        self._run(
            lambda: self.api.communication_poll_details(campaign_id),
            self._show_poll_details,
        )

    def _show_poll_details(self, value: object) -> None:
        details = value if isinstance(value, dict) else {}
        parent = self.schedule_dialog if self.schedule_dialog.isVisible() else self
        PollDetailsDialog(details, parent).exec()

    def _schedule_dates(self) -> tuple[str, str]:
        return (
            self.publish_from.date().toString("yyyy-MM-dd"),
            self.publish_to.date().toString("yyyy-MM-dd"),
        )

    def preview_schedule(self) -> None:
        self._run(
            lambda: self.api.publish_schedule(*self._schedule_dates(), preview=True),
            lambda result: QMessageBox.information(
                self.schedule_dialog,
                "Изменения расписания",
                f"Занятий: {result.get('lessons', 0)}\n"
                f"Затронуто получателей: {result.get('affected_recipients', 0)}\n"
                f"Без изменений: {result.get('unchanged_recipients', 0)}",
            ),
        )

    def publish_schedule(self) -> None:
        self._run(
            lambda: self.api.publish_schedule(*self._schedule_dates()),
            self._schedule_published,
        )

    def _schedule_published(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        QMessageBox.information(
            self.schedule_dialog,
            "Расписание опубликовано",
            f"Поставлено в очередь: {data.get('queued', 0)}",
        )
        self._run(self.api.communication_campaigns, self._campaigns_loaded)

    def load_dialogs(self) -> None:
        self._run(
            lambda: self.api.communication_conversations(self.dialog_search.text()),
            self._dialogs_loaded,
        )

    def _dialogs_loaded(self, value: object) -> None:
        rows = list(value) if isinstance(value, list) else []
        current = self.current_person_id
        self.dialogs.clear()
        for row in rows:
            unread = int(row.get("admin_unread_count") or 0)
            item = QListWidgetItem(str(row.get("full_name", "")))
            item.setData(Qt.ItemDataRole.UserRole, int(row["person_id"]))
            item.setData(Qt.ItemDataRole.UserRole + 1, row.get("full_name", ""))
            item.setData(Qt.ItemDataRole.UserRole + 2, unread)
            item.setData(Qt.ItemDataRole.UserRole + 3, row.get("last_message_preview", ""))
            item.setData(Qt.ItemDataRole.UserRole + 4, row.get("last_message_at"))
            item.setSizeHint(QSize(0, 64))
            self.dialogs.addItem(item)
            self._render_dialog_list_item(item)
            if current == int(row["person_id"]):
                self.dialogs.setCurrentItem(item)

    def _render_dialog_list_item(self, item: QListWidgetItem) -> None:
        container = QWidget()
        container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content = QVBoxLayout(container)
        content.setContentsMargins(9, 5, 9, 5)
        content.setSpacing(2)
        heading = QHBoxLayout()
        heading.setSpacing(7)
        name = QLabel(str(item.data(Qt.ItemDataRole.UserRole + 1)))
        name.setObjectName("dialogName")
        heading.addWidget(name)
        heading.addStretch(1)
        last_time = _dialog_time(item.data(Qt.ItemDataRole.UserRole + 4))
        if last_time:
            time_label = QLabel(last_time)
            time_label.setObjectName("dialogTime")
            heading.addWidget(time_label)
        unread = int(item.data(Qt.ItemDataRole.UserRole + 2) or 0)
        if unread:
            badge = QLabel(str(unread))
            badge.setObjectName("unreadBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.addWidget(badge)
        content.addLayout(heading)
        preview_text = _dialog_preview(
            str(item.data(Qt.ItemDataRole.UserRole + 1) or ""),
            item.data(Qt.ItemDataRole.UserRole + 3),
        )
        preview = QLabel(preview_text)
        preview.setObjectName("dialogPreview")
        content.addWidget(preview)
        self.dialogs.setItemWidget(item, container)

    def _dialog_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        self.current_person_id = int(item.data(Qt.ItemDataRole.UserRole))
        self.chat_title.setText(str(item.data(Qt.ItemDataRole.UserRole + 1)))
        if int(item.data(Qt.ItemDataRole.UserRole + 2) or 0):
            item.setData(Qt.ItemDataRole.UserRole + 2, 0)
            self._render_dialog_list_item(item)
        person_id = self.current_person_id
        self._run(
            lambda: self.api.communication_messages(person_id),
            lambda value: self._messages_loaded_for(person_id, value),
        )
        self._run(lambda: self.api.communication_mark_read(person_id))

    def _messages_loaded(self, value: object) -> None:
        self._messages_loaded_for(self.current_person_id, value)

    def _messages_loaded_for(self, person_id: int | None, value: object) -> None:
        if person_id is not None and person_id != self.current_person_id:
            return
        rows = list(value) if isinstance(value, list) else []
        pending = self.pending_replies.get(person_id or 0, [])
        delivered_texts = Counter(
            str(row.get("text", ""))
            for row in rows
            if row.get("direction") == "outbound"
        )
        remaining = []
        for row in pending:
            text = str(row.get("text", ""))
            if delivered_texts[text] > 0:
                delivered_texts[text] -= 1
            else:
                remaining.append(row)
        if person_id is not None:
            if remaining:
                self.pending_replies[person_id] = remaining
            else:
                self.pending_replies.pop(person_id, None)
        self.chat_messages = [*rows, *remaining]
        self._render_chat_messages()

    def _render_chat_messages(self) -> None:
        lines = [
            "<style>body{font-family:'Segoe UI';font-size:10pt;color:#0f2347;}"
            ".bubble{margin:4px 0;padding:9px 11px;border-radius:9px;}"
            ".incoming{background:#f1f5f9;} .outgoing{background:#e8f1ff;}"
            ".sender{font-weight:600;} .meta{color:#64748b;font-size:8pt;}"
            ".text{margin-top:4px;}</style>"
        ]
        for row in self.chat_messages:
            outbound = row.get("direction") == "outbound"
            who = "Вы" if outbound else "Клиент"
            raw_status = str(row.get("delivery_status", ""))
            status = html.escape(DELIVERY_STATUS_LABELS.get(raw_status, "неизвестно"))
            try:
                created_at = parse_center(row.get("created_at", "")).strftime(
                    "%d.%m.%Y %H:%M"
                )
            except (TypeError, ValueError):
                created_at = str(row.get("created_at", ""))
            created_at = html.escape(created_at)
            text = html.escape(str(row.get("text", ""))).replace("\n", "<br>")
            kind = MESSAGE_TYPE_LABELS.get(str(row.get("message_type", "text")))
            kind_text = f" · {html.escape(kind)}" if kind else ""
            alignment = "right" if outbound else "left"
            bubble = "outgoing" if outbound else "incoming"
            lines.append(
                f"<table width='100%' cellspacing='0' cellpadding='2'><tr>"
                f"<td align='{alignment}'><table width='78%' cellspacing='0' cellpadding='0'>"
                f"<tr><td class='bubble {bubble}'><span class='sender'>{who}</span>"
                f"<span class='meta'> · {created_at} · {status}{kind_text}</span>"
                f"<div class='text'>{text}</div></td></tr></table></td></tr></table>"
            )
        self.chat_history.setHtml("".join(lines))
        self.chat_history.verticalScrollBar().setValue(
            self.chat_history.verticalScrollBar().maximum()
        )

    def send_reply(self) -> None:
        text = self.reply_text.text().strip()
        if self.current_person_id is None or not text:
            return
        person_id = self.current_person_id
        self._run(
            lambda: self.api.communication_reply(person_id, text),
            lambda _result: self._reply_sent(person_id, text),
        )

    def _reply_sent(self, person_id: int, text: str) -> None:
        self.reply_text.clear()
        pending = {
            "direction": "outbound",
            "delivery_status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
            "text": text,
        }
        self.pending_replies.setdefault(person_id, []).append(pending)
        if self.current_person_id == person_id:
            self.chat_messages.append(pending)
            self._render_chat_messages()

    def load_confirmations(self) -> None:
        date_from = self.confirm_from.date().toString("yyyy-MM-dd")
        date_to = self.confirm_to.date().toString("yyyy-MM-dd")
        self._run(
            lambda: self.api.communication_confirmations(date_from, date_to),
            self._confirmations_loaded,
        )

    def _confirmations_loaded(self, value: object) -> None:
        self.confirmation_rows = list(value) if isinstance(value, list) else []
        self._render_confirmations()

    def _render_confirmations(self) -> None:
        rows = [
            item
            for item in self.confirmation_rows
            if not self.attention_only.isChecked() or item.get("needs_attention")
        ]
        self.confirmations.setRowCount(len(rows))
        for row, item in enumerate(rows):
            max_available = bool(item.get("max_available", True))
            request_sent = bool(item.get("request_sent"))
            if not max_available:
                status = "Запрос недоступен"
            elif not request_sent:
                status = "Запрос не отправлен"
            else:
                status = STATUS_LABELS.get(
                    item.get("status"), item.get("status", "")
                )
            reason = str(item.get("reason") or "—")
            if not max_available and reason == "—":
                reason = "Пользователь не подключён к MAX"
            student_answer = str(item.get("student_answer") or "—")
            guardian_answer = str(item.get("guardian_answer") or "—")
            values = [
                item.get("student_name", ""),
                item.get("lesson", ""),
                "Отправлен" if request_sent else "Не отправлен",
                ANSWER_LABELS.get(
                    student_answer,
                    student_answer,
                ),
                ", ".join(
                    ANSWER_LABELS.get(answer.strip(), answer.strip())
                    for answer in guardian_answer.split(",")
                ),
                status,
                reason,
            ]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setToolTip(str(text))
                if column in {2, 3, 4}:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.confirmations.setItem(row, column, cell)
        self.confirmations.resizeRowsToContents()
        for row in range(self.confirmations.rowCount()):
            self.confirmations.setRowHeight(
                row, max(42, self.confirmations.rowHeight(row))
            )

    def _settings_loaded(self, value: object) -> None:
        self.rules = list(value) if isinstance(value, list) else []
        self.settings.setRowCount(len(self.rules))
        for row, rule in enumerate(self.rules):
            self.settings.setItem(
                row,
                0,
                QTableWidgetItem(
                    EVENT_LABELS.get(rule.get("event_code"), "Другое событие")
                ),
            )
            self.settings.setItem(
                row,
                1,
                QTableWidgetItem(
                    CONTEXT_LABELS.get(
                        rule.get("recipient_context"), "Все"
                    )
                ),
            )
            offset = int(rule.get("offset_minutes", -1))
            self.settings.setItem(
                row,
                2,
                QTableWidgetItem("Сразу" if offset < 0 else str(offset)),
            )
            enabled = QTableWidgetItem()
            enabled.setFlags(enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            enabled.setCheckState(
                Qt.CheckState.Checked if rule.get("enabled") else Qt.CheckState.Unchecked
            )
            self.settings.setItem(row, 3, enabled)
            priority = SafeComboBox()
            for value in ("low", "normal", "high"):
                priority.addItem(PRIORITY_LABELS[value], value)
            priority.setCurrentIndex(
                max(0, priority.findData(str(rule.get("priority", "normal"))))
            )
            self.settings.setCellWidget(row, 4, priority)
            self.settings.setItem(row, 5, QTableWidgetItem(str(rule.get("quiet_start") or "")))
            self.settings.setItem(row, 6, QTableWidgetItem(str(rule.get("quiet_end") or "")))
            configuration = rule.get("configuration") or {}
            if rule.get("event_code") == "lesson_confirmation_request":
                follow_up = SafeComboBox()
                follow_up.addItem("Без повтора", "none")
                follow_up.addItem("Один повтор", "once")
                follow_up.setCurrentIndex(
                    max(0, follow_up.findData(configuration.get("follow_up", "once")))
                )
                self.settings.setCellWidget(row, 7, follow_up)
                self.settings.setItem(
                    row,
                    8,
                    QTableWidgetItem(
                        str(configuration.get("follow_up_offset_minutes", 180))
                    ),
                )
            else:
                self.settings.setItem(row, 7, QTableWidgetItem("—"))
                self.settings.setItem(row, 8, QTableWidgetItem("—"))
        self._current_settings_event = None
        self._render_settings_events()
        self._render_person_settings()

    def _settings_people_loaded(self, value: object) -> None:
        self.people = list(value) if isinstance(value, list) else []
        current = self.pending_settings_person_id or self.settings_person.currentData()
        self.settings_person.blockSignals(True)
        self.settings_person.clear()
        self.settings_person.addItem("Выберите человека", None)
        for person in self.people:
            name = str(person.get("full_name", ""))
            phone = str(person.get("phone", ""))
            phone_digits = "".join(character for character in phone if character.isdigit())
            self.settings_person.addItem(name, int(person["id"]))
            self.settings_person.setItemData(
                self.settings_person.count() - 1,
                f"{name} {phone} {phone_digits} {phone_digits[-4:]}",
                PERSON_SEARCH_ROLE,
            )
        index = self.settings_person.findData(current)
        self.settings_person.setCurrentIndex(max(0, index))
        self.settings_person.blockSignals(False)
        self.pending_settings_person_id = None
        if index >= 0:
            self.load_person_settings()

    def load_person_settings(self) -> None:
        person_id = self.settings_person.currentData()
        if person_id is None:
            self.person_settings = {}
            self.person_overrides.setRowCount(0)
            self._render_person_settings()
            return
        person = next((item for item in self.people if int(item["id"]) == person_id), {})
        self.settings_target.blockSignals(True)
        self.settings_target.clear()
        self.settings_target.addItem("Все контексты человека", None)
        for child in person.get("students") or []:
            self.settings_target.addItem(
                f"Как родитель: {child.get('full_name', '')}", int(child["id"])
            )
        self.settings_target.blockSignals(False)
        self._run(
            lambda: self.api.communication_person_settings(int(person_id)),
            lambda value: self._person_settings_loaded_for(int(person_id), value),
        )

    def _person_settings_loaded(self, value: object) -> None:
        self.person_settings = value if isinstance(value, dict) else {}
        self._render_person_settings()

    def _person_settings_loaded_for(self, person_id: int, value: object) -> None:
        if self.settings_person.currentData() != person_id:
            return
        self._person_settings_loaded(value)

    def _render_person_settings(self) -> None:
        if not hasattr(self, "person_overrides"):
            return
        person_selected = self.settings_person.currentData() is not None
        self.settings_target.setEnabled(person_selected)
        self.save_person_settings_button.setEnabled(person_selected)
        self.person_empty_state.setVisible(not person_selected)
        self.person_overrides.setVisible(person_selected)
        self.save_person_settings_button.setVisible(person_selected)
        if not person_selected:
            self.person_overrides.setRowCount(0)
            return
        child_id = self.settings_target.currentData()
        rules = [
            rule
            for rule in self.rules
            if child_id is None or rule.get("recipient_context") == "guardian"
        ]
        source_key = "guardian_child_overrides" if child_id is not None else "overrides"
        existing = list(self.person_settings.get(source_key) or [])
        self.person_rule_rows = rules
        self.person_overrides.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            match = next(
                (
                    item
                    for item in existing
                    if item.get("event_code") == rule.get("event_code")
                    and item.get("offset_minutes") == rule.get("offset_minutes")
                    and (
                        child_id is not None
                        or item.get("recipient_context") == rule.get("recipient_context")
                    )
                    and (child_id is None or item.get("student_person_id") == child_id)
                ),
                None,
            )
            values = [
                EVENT_LABELS.get(rule.get("event_code"), "Другое событие"),
                CONTEXT_LABELS.get(rule.get("recipient_context"), ""),
                (
                    "Сразу"
                    if int(rule.get("offset_minutes", -1)) < 0
                    else str(rule.get("offset_minutes", -1))
                ),
            ]
            for column, text in enumerate(values):
                self.person_overrides.setItem(row, column, QTableWidgetItem(str(text)))
            state = SafeComboBox()
            state.addItem("Наследовать", "inherit")
            state.addItem("Включить", "on")
            state.addItem("Отключить", "off")
            state.setCurrentIndex(max(0, state.findData((match or {}).get("state", "inherit"))))
            self.person_overrides.setCellWidget(row, 3, state)

    def save_person_settings(self) -> None:
        person_id = self.settings_person.currentData()
        if person_id is None:
            QMessageBox.information(self, "Настройки", "Сначала выберите человека.")
            return
        child_id = self.settings_target.currentData()
        retained = []
        if child_id is None:
            retained.extend(self.person_settings.get("guardian_child_overrides") or [])
        else:
            retained.extend(self.person_settings.get("overrides") or [])
            retained.extend(
                item
                for item in (self.person_settings.get("guardian_child_overrides") or [])
                if item.get("student_person_id") != child_id
            )
        for row, rule in enumerate(self.person_rule_rows):
            state = self.person_overrides.cellWidget(row, 3)
            value = state.currentData() if isinstance(state, QComboBox) else "inherit"
            if value == "inherit":
                continue
            retained.append(
                {
                    "recipient_context": rule.get("recipient_context"),
                    "event_code": rule.get("event_code"),
                    "offset_minutes": rule.get("offset_minutes", -1),
                    "state": value,
                    "student_person_id": child_id,
                    "configuration": {},
                }
            )
        self._run(
            lambda: self.api.save_communication_person_settings(int(person_id), retained),
            lambda result: (
                self._person_settings_loaded(result),
                QMessageBox.information(self, "Настройки", "Индивидуальные настройки сохранены."),
            ),
        )

    def save_settings(self) -> None:
        self._store_current_settings_editor()
        payload = []
        for row, source in enumerate(self.rules):
            priority = self.settings.cellWidget(row, 4)
            configuration = dict(source.get("configuration") or {})
            if source.get("event_code") == "lesson_confirmation_request":
                follow_up = self.settings.cellWidget(row, 7)
                configuration["follow_up"] = (
                    follow_up.currentData() if isinstance(follow_up, QComboBox) else "once"
                )
                try:
                    configuration["follow_up_offset_minutes"] = max(
                        61, int(self.settings.item(row, 8).text())
                    )
                except (AttributeError, ValueError):
                    configuration["follow_up_offset_minutes"] = 180
            payload.append(
                {
                    "event_code": source["event_code"],
                    "recipient_context": source["recipient_context"],
                    "offset_minutes": int(source.get("offset_minutes", -1)),
                    "enabled": self.settings.item(row, 3).checkState() == Qt.CheckState.Checked,
                    "requires_confirmation": source.get("requires_confirmation", False),
                    "priority": priority.currentData()
                    if isinstance(priority, QComboBox)
                    else "normal",
                    "quiet_hours_policy": source.get("quiet_hours_policy", "defer"),
                    "quiet_start": self.settings.item(row, 5).text() or None,
                    "quiet_end": self.settings.item(row, 6).text() or None,
                    "configuration": configuration,
                }
            )
        self._run(
            lambda: self.api.save_communication_global_settings(payload),
            lambda result: (
                self._settings_loaded(result),
                QMessageBox.information(self, "Настройки", "Настройки сохранены."),
            ),
        )

    def shutdown(self) -> None:
        self._closing = True
        self.timer.stop()
        self.schedule_dialog.close()
        self.pool.clear()
        self.pool.waitForDone(16_000)
        self._workers.clear()
