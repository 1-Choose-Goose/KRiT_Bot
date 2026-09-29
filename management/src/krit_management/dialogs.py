from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .widgets import matches_word_prefix

ROLE_LABELS = {"student": "Ученик", "parent": "Родитель", "teacher": "Учитель"}
ATTENDANCE_LABELS = {
    "expected": "Ожидается",
    "present": "Присутствует",
    "late": "Опоздал",
    "absent": "Не пришёл",
    "left_early": "Ушёл раньше",
    "excused": "Отменено",
}


def person_roles(person: dict[str, Any]) -> list[str]:
    roles = person.get("roles")
    if isinstance(roles, list):
        return [str(role) for role in roles]
    return [str(person["role"])] if person.get("role") else []


class LoginDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Вход · КРиТ")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)
        title = QLabel("Вход в систему")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.username = QLineEdit("admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Логин", self.username)
        form.addRow("Пароль", self.password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Войти")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setProperty("kind", "secondary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password.returnPressed.connect(self._accept_if_valid)
        self.password.setFocus()

    def _accept_if_valid(self) -> None:
        if self.username.text().strip() and self.password.text():
            self.accept()
        else:
            self.password.setFocus(Qt.FocusReason.OtherFocusReason)


class PersonPickerDialog(QDialog):
    def __init__(self, people: list[dict[str, Any]], parent=None) -> None:
        super().__init__(parent)
        self.people = people
        self.setWindowTitle("Выбор клиента")
        self.setMinimumSize(480, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по ФИО или телефону")
        self.search.textChanged.connect(self._render)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Выбрать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setProperty("kind", "secondary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._render()

    def _render(self) -> None:
        query = self.search.text().casefold().strip()
        self.list.clear()
        for person in self.people:
            full_name = str(person.get("full_name", ""))
            phone = str(person.get("phone", ""))
            if query and not (
                matches_word_prefix(query, full_name) or query in phone.casefold()
            ):
                continue
            item = QListWidgetItem(f"{person.get('full_name', '')}   {person.get('phone', '')}")
            item.setData(Qt.ItemDataRole.UserRole, person)
            self.list.addItem(item)

    def selected_person(self) -> dict[str, Any] | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class PersonDialog(QDialog):
    def __init__(
        self,
        person: dict[str, Any] | None = None,
        parent=None,
        *,
        available_people: list[dict[str, Any]] | None = None,
        open_related: Callable[[dict[str, Any]], None] | None = None,
        load_learning_history: Callable[[int, list[str], Callable[[dict[str, Any]], None]], None]
        | None = None,
        open_lesson: Callable[[int], None] | None = None,
        allow_relations: bool = True,
    ) -> None:
        super().__init__(parent)
        self.person = person or {}
        self.available_people = available_people or []
        self.open_related = open_related
        self.load_learning_history = load_learning_history
        self.open_lesson = open_lesson
        self._history_loaded = False
        self.allow_relations = allow_relations
        self.relation_states: dict[str, list[dict[str, Any]]] = {
            "parent": [dict(item) for item in self.person.get("guardians", [])],
            "student": [dict(item) for item in self.person.get("students", [])],
        }
        self.setWindowTitle("Карточка клиента")
        self.setMinimumSize(760, 480)
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("Карточка клиента")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        self.sections = QTabWidget()
        self.sections.setObjectName("clientTabs")
        main_page = QWidget()
        main_layout = QVBoxLayout(main_page)
        main_layout.setContentsMargins(8, 12, 8, 8)
        form = QFormLayout()
        self.full_name = QLineEdit(str(self.person.get("full_name", "")))
        self.phone = QLineEdit()
        self.phone.setInputMask("+7 (000) 000-00-00;_")
        digits = "".join(c for c in str(self.person.get("phone", "")) if c.isdigit())
        if len(digits) == 11 and digits[0] in {"7", "8"}:
            digits = digits[1:]
        self.phone.setText(digits[:10])
        roles = person_roles(self.person)
        self.role_checks: dict[str, QCheckBox] = {}
        roles_widget = QWidget()
        roles_layout = QHBoxLayout(roles_widget)
        roles_layout.setContentsMargins(0, 0, 0, 0)
        for value, label in ROLE_LABELS.items():
            check = QCheckBox(label)
            check.setChecked(value in (roles or ["student"]))
            check.toggled.connect(self._role_changed)
            self.role_checks[value] = check
            roles_layout.addWidget(check)
        roles_layout.addStretch(1)
        max_user_id = self.person.get("max_user_id")
        self.max_user_id = QLineEdit("Отсутствует" if max_user_id is None else str(max_user_id))
        self.max_user_id.setReadOnly(True)
        self.authorization = QLabel("Авторизован" if max_user_id is not None else "Не авторизован")
        self.active = QCheckBox("Доступ к боту")
        self.active.setChecked(bool(self.person.get("active", True)))
        form.addRow("ФИО", self.full_name)
        form.addRow("Телефон", self.phone)
        form.addRow("Роли", roles_widget)
        form.addRow("ID в MAX", self.max_user_id)
        form.addRow("Статус MAX", self.authorization)
        form.addRow("", self.active)
        main_layout.addLayout(form)
        main_layout.addStretch(1)
        self.sections.addTab(main_page, "Основное")

        relations_page = QWidget()
        relations_layout = QVBoxLayout(relations_page)
        relations_layout.setContentsMargins(8, 12, 8, 8)
        self.relation_widgets: list[Any] = []
        relation_header = QHBoxLayout()
        self.relation_mode = QComboBox()
        self.relation_mode.currentIndexChanged.connect(lambda _index: self._render_relations())
        relation_header.addWidget(self.relation_mode)
        relation_header.addStretch(1)
        for label, kind, callback in (
            ("Добавить", "secondary", self._add_existing),
            ("Создать", "secondary", self._create_related),
            ("Убрать связь", "danger", self._remove_related),
        ):
            button = QPushButton(label)
            button.setProperty("kind", kind)
            button.setFixedWidth(max(88, button.fontMetrics().horizontalAdvance(label) + 36))
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.clicked.connect(callback)
            relation_header.addWidget(button)
            self.relation_widgets.append(button)
        self.relation_widgets.append(self.relation_mode)
        relations_layout.addLayout(relation_header)
        self.related_list = QListWidget()
        self.related_list.setMaximumHeight(130)
        self.related_list.itemDoubleClicked.connect(self._open_related)
        relations_layout.addWidget(self.related_list)
        self.relation_widgets.append(self.related_list)
        self.sections.addTab(relations_page, "Связи")

        if self.person.get("id") and ({"student", "teacher"} & set(roles)):
            learning_page = QWidget()
            learning_layout = QVBoxLayout(learning_page)
            learning_layout.setContentsMargins(8, 12, 8, 8)
            self.learning_status = QLabel(
                "История загрузится при открытии этого раздела."
            )
            learning_layout.addWidget(self.learning_status)
            self.learning_history = QTableWidget(0, 8)
            self.learning_history.setHorizontalHeaderLabels(
                [
                    "Роль",
                    "Дата",
                    "Предмет",
                    "Преподаватель / кабинет",
                    "План",
                    "Факт",
                    "Посещение",
                    "Отмена",
                ]
            )
            self.learning_history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.learning_history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.learning_history.verticalHeader().setVisible(False)
            self.learning_history.horizontalHeader().setStretchLastSection(True)
            self.learning_history.itemDoubleClicked.connect(self._open_history_lesson)
            learning_layout.addWidget(self.learning_history, 1)
            self.presence_history = QTableWidget(0, 2)
            self.presence_history.setHorizontalHeaderLabels(["Приход в клуб", "Уход из клуба"])
            self.presence_history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.presence_history.verticalHeader().setVisible(False)
            self.presence_history.horizontalHeader().setStretchLastSection(True)
            learning_layout.addWidget(self.presence_history)
            self.sections.addTab(learning_page, "Учебный процесс")
        layout.addWidget(self.sections, 1)
        self.sections.currentChanged.connect(self._section_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setProperty("kind", "secondary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._role_changed()

    def _section_changed(self, index: int) -> None:
        if self.sections.tabText(index) != "Учебный процесс" or self._history_loaded:
            return
        person_id = self.person.get("id")
        if person_id is None or self.load_learning_history is None:
            return
        self._history_loaded = True
        self.learning_status.setText("Загрузка истории…")
        self.load_learning_history(
            int(person_id), person_roles(self.person), self.set_learning_history
        )

    def set_learning_history(self, result: dict[str, Any]) -> None:
        rows: list[tuple[str, dict[str, Any]]] = []
        rows.extend(("Ученик", item) for item in result.get("student", {}).get("lessons", []))
        rows.extend(
            ("Преподаватель", item)
            for item in result.get("teacher", {}).get("lessons", [])
        )
        rows.sort(key=lambda pair: str(pair[1].get("start_at", "")), reverse=True)
        self.learning_history.setRowCount(len(rows))
        for row, (role, lesson) in enumerate(rows):
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            end = datetime.fromisoformat(lesson["end_at"]).astimezone()
            actual_start = lesson.get("actual_start_at")
            actual_end = lesson.get("actual_end_at")
            fact = "—"
            if actual_start:
                fact_start = datetime.fromisoformat(actual_start).astimezone()
                fact = f"{fact_start:%H:%M}"
                if actual_end:
                    fact_end = datetime.fromisoformat(actual_end).astimezone()
                    fact += f"–{fact_end:%H:%M}"
            attendance = ATTENDANCE_LABELS.get(
                str(lesson.get("attendance_status", "")),
                str(lesson.get("attendance_status", "—")),
            )
            cancellation = "—"
            if lesson.get("cancelled_by"):
                actor = {
                    "student": "учеником",
                    "guardian": "родителем",
                    "administrator": "администратором",
                }.get(lesson.get("cancelled_by"), "неизвестно кем")
                cancelled_at = lesson.get("cancelled_at")
                when = (
                    datetime.fromisoformat(cancelled_at).astimezone().strftime(
                        "%d.%m.%Y %H:%M"
                    )
                    if cancelled_at
                    else "время не указано"
                )
                cancellation = (
                    f"{actor}, {when}: "
                    f"{lesson.get('cancellation_reason') or 'без причины'}"
                )
            values = [
                role,
                f"{start:%d.%m.%Y}",
                lesson.get("subject_name_snapshot", ""),
                (
                    f"{lesson.get('teacher_name_snapshot', '')} · "
                    f"{lesson.get('room_name_snapshot', '')}"
                ),
                f"{start:%H:%M}–{end:%H:%M}",
                fact,
                attendance,
                cancellation,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, lesson.get("id"))
                self.learning_history.setItem(row, column, cell)
        presence = result.get("student", {}).get("presence", [])
        self.presence_history.setRowCount(len(presence))
        for row, item in enumerate(presence):
            for column, key in enumerate(("arrived_at", "left_at")):
                value = item.get(key)
                text_value = (
                    datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
                    if value
                    else "—"
                )
                self.presence_history.setItem(row, column, QTableWidgetItem(text_value))
        self.learning_status.setText(
            "Дважды щёлкните по занятию, чтобы открыть его карточку."
        )

    def _open_history_lesson(self, item: QTableWidgetItem) -> None:
        lesson_id = item.data(Qt.ItemDataRole.UserRole)
        if lesson_id is not None and self.open_lesson is not None:
            self.open_lesson(int(lesson_id))

    def _target_role(self) -> str | None:
        value = self.relation_mode.currentData()
        return str(value) if value else None

    def _role_changed(self) -> None:
        previous = self._target_role()
        targets = []
        if self.role_checks["student"].isChecked():
            targets.append(("Родители", "parent"))
        if self.role_checks["parent"].isChecked():
            targets.append(("Ученики", "student"))
        self.relation_mode.blockSignals(True)
        self.relation_mode.clear()
        for label, value in targets:
            self.relation_mode.addItem(label, value)
        previous_index = self.relation_mode.findData(previous)
        if previous_index >= 0:
            self.relation_mode.setCurrentIndex(previous_index)
        self.relation_mode.blockSignals(False)
        enabled = self.allow_relations and bool(targets)
        for widget in self.relation_widgets:
            widget.setVisible(enabled)
        if not enabled:
            QTimer.singleShot(0, self._resize_to_content)
            return
        self._render_relations()
        QTimer.singleShot(0, self._resize_to_content)

    def _resize_to_content(self) -> None:
        self.layout().activate()
        self.resize(max(self.width(), self.minimumWidth()), self.sizeHint().height())

    def _render_relations(self) -> None:
        self.related_list.clear()
        target = self._target_role()
        for person in self.relation_states.get(target or "", []):
            item = QListWidgetItem(f"{person.get('full_name', '')}   {person.get('phone', '')}")
            item.setData(Qt.ItemDataRole.UserRole, person)
            self.related_list.addItem(item)

    def _add_existing(self) -> None:
        target = self._target_role()
        related_people = self.relation_states.get(target or "", [])
        related_ids = {item.get("id") for item in related_people}
        candidates = [
            person
            for person in self.available_people
            if target in person_roles(person)
            and person.get("id") != self.person.get("id")
            and person.get("id") not in related_ids
        ]
        picker = PersonPickerDialog(candidates, self)
        if picker.exec():
            selected = picker.selected_person()
            if selected:
                related_people.append(selected)
                self._render_relations()

    def _create_related(self) -> None:
        target = self._target_role()
        if target is None:
            return
        dialog = PersonDialog({"roles": [target], "active": True}, self, allow_relations=False)
        if dialog.exec():
            payload = dialog.payload()
            self.relation_states[target].append(
                {
                    "full_name": payload["full_name"],
                    "phone": payload["phone"],
                    "_pending_payload": payload,
                }
            )
            self._render_relations()

    def _remove_related(self) -> None:
        row = self.related_list.currentRow()
        target = self._target_role()
        if row >= 0 and target:
            self.relation_states[target].pop(row)
            self._render_relations()

    def _open_related(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.ItemDataRole.UserRole)
        if self.open_related and person.get("id"):
            full = next(
                (entry for entry in self.available_people if entry.get("id") == person["id"]),
                person,
            )
            self.open_related(full)

    def _accept_if_valid(self) -> None:
        if (
            len(self.full_name.text().strip()) >= 3
            and self.phone.hasAcceptableInput()
            and any(check.isChecked() for check in self.role_checks.values())
        ):
            self.accept()

    def payload(self) -> dict[str, Any]:
        digits = "".join(c for c in self.phone.text() if c.isdigit())
        return {
            "full_name": " ".join(self.full_name.text().split()),
            "phone": "+" + digits,
            "roles": [role for role, check in self.role_checks.items() if check.isChecked()],
            "active": self.active.isChecked(),
        }

    def relation_state(self) -> dict[str, tuple[list[int], list[dict[str, Any]]]]:
        result = {}
        for target, people in self.relation_states.items():
            existing = [int(item["id"]) for item in people if item.get("id")]
            pending = [item["_pending_payload"] for item in people if "_pending_payload" in item]
            result[target] = (existing, pending)
        return result
