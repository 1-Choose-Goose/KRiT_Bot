from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout,
)

ROLE_LABELS = {"student": "Ученик", "parent": "Родитель", "teacher": "Учитель"}


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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
            haystack = f"{person.get('full_name', '')} {person.get('phone', '')}".casefold()
            if query and query not in haystack:
                continue
            item = QListWidgetItem(f"{person.get('full_name', '')}   {person.get('phone', '')}")
            item.setData(Qt.ItemDataRole.UserRole, person)
            self.list.addItem(item)

    def selected_person(self) -> dict[str, Any] | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class PersonDialog(QDialog):
    def __init__(self, person: dict[str, Any] | None = None, parent=None, *,
                 available_people: list[dict[str, Any]] | None = None,
                 open_related: Callable[[dict[str, Any]], None] | None = None,
                 allow_relations: bool = True) -> None:
        super().__init__(parent)
        self.person = person or {}
        self.available_people = available_people or []
        self.open_related = open_related
        self.allow_relations = allow_relations
        self.related_people: list[dict[str, Any]] = []
        self._loaded_relation_role: str | None = None
        self.setWindowTitle("Карточка клиента")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("Карточка клиента")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.full_name = QLineEdit(str(self.person.get("full_name", "")))
        self.phone = QLineEdit()
        self.phone.setInputMask("+7 (000) 000-00-00;_")
        digits = "".join(c for c in str(self.person.get("phone", "")) if c.isdigit())
        if len(digits) == 11 and digits[0] in {"7", "8"}:
            digits = digits[1:]
        self.phone.setText(digits[:10])
        self.role = QComboBox()
        for value, label in ROLE_LABELS.items():
            self.role.addItem(label, value)
        roles = person_roles(self.person)
        self.role.setCurrentIndex(max(0, self.role.findData(roles[0] if roles else "student")))
        self.role.currentIndexChanged.connect(self._role_changed)
        max_user_id = self.person.get("max_user_id")
        self.max_user_id = QLineEdit("Отсутствует" if max_user_id is None else str(max_user_id))
        self.max_user_id.setReadOnly(True)
        self.authorization = QLabel("Авторизован" if max_user_id is not None else "Не авторизован")
        self.active = QCheckBox("Доступ к боту")
        self.active.setChecked(bool(self.person.get("active", True)))
        form.addRow("ФИО", self.full_name)
        form.addRow("Телефон", self.phone)
        form.addRow("Категория", self.role)
        form.addRow("ID в MAX", self.max_user_id)
        form.addRow("Статус MAX", self.authorization)
        form.addRow("", self.active)
        layout.addLayout(form)

        self.relation_widgets: list[Any] = []
        relation_header = QHBoxLayout()
        self.relation_title = QLabel()
        self.relation_title.setObjectName("sectionTitle")
        relation_header.addWidget(self.relation_title)
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
        self.relation_widgets.append(self.relation_title)
        layout.addLayout(relation_header)
        self.related_list = QListWidget()
        self.related_list.setMaximumHeight(130)
        self.related_list.itemDoubleClicked.connect(self._open_related)
        layout.addWidget(self.related_list)
        self.relation_widgets.append(self.related_list)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setProperty("kind", "secondary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._role_changed()

    def _target_role(self) -> str | None:
        role = str(self.role.currentData())
        return "parent" if role == "student" else "student" if role == "parent" else None

    def _role_changed(self) -> None:
        target = self._target_role()
        enabled = self.allow_relations and target is not None
        for widget in self.relation_widgets:
            widget.setVisible(enabled)
        if not enabled:
            QTimer.singleShot(0, self._resize_to_content)
            return
        self.relation_title.setText("Родители" if target == "parent" else "Ученики")
        if self._loaded_relation_role != target:
            original_roles = person_roles(self.person)
            source = (
                self.person.get("guardians" if target == "parent" else "students", [])
                if str(self.role.currentData()) in original_roles
                else []
            )
            self.related_people = [dict(item) for item in source]
            self._loaded_relation_role = target
        self._render_relations()
        QTimer.singleShot(0, self._resize_to_content)

    def _resize_to_content(self) -> None:
        self.layout().activate()
        self.resize(max(self.width(), self.minimumWidth()), self.sizeHint().height())

    def _render_relations(self) -> None:
        self.related_list.clear()
        for person in self.related_people:
            item = QListWidgetItem(f"{person.get('full_name', '')}   {person.get('phone', '')}")
            item.setData(Qt.ItemDataRole.UserRole, person)
            self.related_list.addItem(item)

    def _add_existing(self) -> None:
        target = self._target_role()
        related_ids = {item.get("id") for item in self.related_people}
        candidates = [person for person in self.available_people
                      if target in person_roles(person)
                      and person.get("id") != self.person.get("id")
                      and person.get("id") not in related_ids]
        picker = PersonPickerDialog(candidates, self)
        if picker.exec():
            selected = picker.selected_person()
            if selected:
                self.related_people.append(selected)
                self._render_relations()

    def _create_related(self) -> None:
        target = self._target_role()
        if target is None:
            return
        dialog = PersonDialog({"roles": [target], "active": True}, self, allow_relations=False)
        if dialog.exec():
            payload = dialog.payload()
            self.related_people.append({"full_name": payload["full_name"], "phone": payload["phone"], "_pending_payload": payload})
            self._render_relations()

    def _remove_related(self) -> None:
        row = self.related_list.currentRow()
        if row >= 0:
            self.related_people.pop(row)
            self._render_relations()

    def _open_related(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.ItemDataRole.UserRole)
        if self.open_related and person.get("id"):
            full = next((entry for entry in self.available_people if entry.get("id") == person["id"]), person)
            self.open_related(full)

    def _accept_if_valid(self) -> None:
        if len(self.full_name.text().strip()) >= 3 and self.phone.hasAcceptableInput():
            self.accept()

    def payload(self) -> dict[str, Any]:
        digits = "".join(c for c in self.phone.text() if c.isdigit())
        return {"full_name": " ".join(self.full_name.text().split()), "phone": "+" + digits,
                "roles": [self.role.currentData()], "active": self.active.isChecked()}

    def relation_state(self) -> tuple[list[int], list[dict[str, Any]]]:
        existing = [int(item["id"]) for item in self.related_people if item.get("id")]
        pending = [item["_pending_payload"] for item in self.related_people if "_pending_payload" in item]
        return existing, pending
