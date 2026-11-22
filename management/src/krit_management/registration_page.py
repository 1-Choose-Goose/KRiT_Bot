"""Correspondence and contract registration journals."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .registration_export import export_journal
from .widgets import SearchableComboBox
from .workers import Worker

JOURNALS = (
    ("incoming", "Входящая корреспонденция"),
    ("outgoing", "Исходящая корреспонденция"),
    ("contract", "Регистрация договоров"),
)


def registration_year(record: dict[str, Any]) -> int | None:
    match = re.fullmatch(r"(?:вх|исх|дг)-(\d{4})/\d+", str(record.get("registration_number") or ""))
    if match:
        return int(match.group(1))
    registered = str(record.get("registration_date") or "")
    return int(registered[:4]) if re.fullmatch(r"\d{4}", registered[:4]) else None


FIELDS = {
    "incoming": (
        ("received_date", "Дата получения и регистрации"),
        ("channel", "Способ получения"),
        ("executor", "Исполнитель"),
        ("due_date", "Срок исполнения"),
        ("result", "Результат исполнения"),
        ("reply_number", "Ответы, исходящие №"),
        ("notes", "Примечание"),
    ),
    "outgoing": (
        ("channel", "Способ отправки"),
        ("signer", "Подписал"),
        ("executor", "Исполнитель"),
        ("notes", "Примечание"),
    ),
    "contract": (
        ("executor", "Исполнитель"),
        ("student_name", "Ученик (ФИО)"),
        ("service_period", "Срок оказания услуг"),
        ("monthly_price", "Стоимость услуг в месяц"),
        ("amendments", "Соглашения об изменении цены"),
        ("notes", "Примечание"),
    ),
}


class RegistrationDialog(QDialog):
    def __init__(
        self,
        kind: str,
        people: list[dict[str, Any]],
        record: dict[str, Any] | None = None,
        parent: QWidget | None = None,
        *,
        incoming_records: list[dict[str, Any]] | None = None,
        subjects: list[dict[str, Any]] | None = None,
        read_only: bool = False,
    ) -> None:
        super().__init__(parent)
        if read_only and record is None:
            raise ValueError("Для просмотра нужна зарегистрированная запись")
        self.setWindowTitle(
            "Просмотр записи"
            if read_only
            else "Исправить запись"
            if record
            else "Зарегистрировать документ"
        )
        self.setMinimumWidth(520)
        self.resize(680, 600)
        self.kind = kind
        self.record = record or {}
        self.people_by_id = {int(person["id"]): person for person in people}
        layout = QVBoxLayout(self)
        content = QWidget()
        content.setObjectName("registrationForm")
        content.setStyleSheet(
            "QWidget#registrationForm { background-color: #f4f6fa; }"
            "QWidget#registrationForm QLabel { color: #10254c; background: transparent; }"
        )
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        registered = self.record.get("registration_date")
        date_label = (
            date.fromisoformat(registered).strftime("%d.%m.%Y")
            if registered
            else "Программа присвоит при сохранении"
        )
        if record:
            self.registration_number = QLineEdit(str(record["registration_number"]))
            form.addRow("Регистрационный №", self.registration_number)
        self.document_date = QLineEdit() if record or kind != "contract" else None
        document_date = self.record.get("document_date")
        if document_date and self.document_date is not None:
            self.document_date.setText(date.fromisoformat(document_date).strftime("%d.%m.%Y"))
        if self.document_date is not None:
            self.document_date.setPlaceholderText("ДД.ММ.ГГГГ")
        if self.document_date is not None:
            form.addRow(
                "Дата письма отправителя"
                if kind == "incoming"
                else "Дата договора и регистрации"
                if kind == "contract"
                else "Дата подписания",
                self.document_date,
            )
        if kind == "outgoing":
            if record:
                self.sent_date = QLineEdit(date_label)
                self.sent_date.setPlaceholderText("ДД.ММ.ГГГГ")
                form.addRow("Дата отправки и регистрации", self.sent_date)
        self.document_number = QLineEdit(str(self.record.get("document_number") or ""))
        if kind == "incoming":
            form.addRow("Номер письма отправителя", self.document_number)
        self.counterparty = QTextEdit(str(self.record.get("counterparty") or ""))
        self.counterparty.setFixedHeight(64)
        form.addRow(
            {"incoming": "Отправитель", "outgoing": "Адресат", "contract": "Заказчик"}[kind],
            self.counterparty,
        )
        details = self.record.get("details") or {}
        if kind == "contract" and read_only:
            self.subject = QTextEdit(str(self.record.get("subject") or ""))
            self.subject.setFixedHeight(80)
            form.addRow("Услуга", self.subject)
        elif kind == "contract":
            subject_holder = QWidget()
            subject_layout = QVBoxLayout(subject_holder)
            subject_layout.setContentsMargins(0, 0, 0, 0)
            self.subject_search = QLineEdit()
            self.subject_search.setPlaceholderText("Поиск предмета…")
            self.subject_search.setClearButtonEnabled(True)
            self.subject_list = QListWidget()
            self.subject_list.setMinimumHeight(135)
            check_asset = Path(__file__).with_name("assets") / "checkbox_check.svg"
            self.subject_list.setStyleSheet(
                "QListWidget::indicator { width: 18px; height: 18px; "
                "border: 1px solid #65758e; border-radius: 4px; background: white; }"
                "QListWidget::indicator:checked { background: #1b63db; "
                "border-color: #1b63db; image: url(\"" + check_asset.as_posix() + "\"); }"
            )
            saved_ids = {
                value.strip()
                for value in str(details.get("subject_ids") or "").split(",")
                if value.strip()
            }
            legacy_names = {
                value.strip().replace(" ", "").casefold()
                for value in re.split(r",|\s+и\s+", str(self.record.get("subject") or ""))
            }
            for item in subjects or []:
                if not item.get("active", True):
                    continue
                choice = QListWidgetItem(str(item.get("name") or ""))
                choice.setData(Qt.ItemDataRole.UserRole, int(item["id"]))
                choice.setFlags(choice.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = (
                    str(item["id"]) in saved_ids
                    if saved_ids
                    else choice.text().replace(" ", "").casefold() in legacy_names
                )
                choice.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.subject_list.addItem(choice)
            self.subject_search.textChanged.connect(self._filter_subjects)
            subject_layout.addWidget(self.subject_search)
            subject_layout.addWidget(self.subject_list)
            form.addRow("Услуга", subject_holder)
        else:
            self.subject = QTextEdit(str(self.record.get("subject") or ""))
            self.subject.setFixedHeight(80)
            form.addRow("Краткое содержание", self.subject)
        self.person = SearchableComboBox(placeholder="Введите фамилию ученика")
        popup_style = (
            "QAbstractItemView { background: white; color: #172033; "
            "border: 1px solid #ccd5e2; selection-background-color: #e6efff; "
            "selection-color: #172033; }"
            "QAbstractItemView::item:selected { background: #e6efff; color: #172033; }"
        )
        self.person.view().setStyleSheet(popup_style)
        self.person.completer().popup().setStyleSheet(popup_style)
        self.person.addItem("Не связан с учеником", None)
        for person in people:
            if "student" in (person.get("roles") or []):
                self.person.addItem(str(person.get("full_name") or ""), person.get("id"))
        selected = self.person.findData(self.record.get("person_id"))
        if selected >= 0:
            self.person.setCurrentIndex(selected)
        if kind == "contract":
            form.addRow("Ученик в базе", self.person)
            self.customer_choice = SearchableComboBox(placeholder="Выберите заказчика из базы")
            self.customer_choice.view().setStyleSheet(popup_style)
            self.customer_choice.completer().popup().setStyleSheet(popup_style)
            self._populate_customer_choices(self.record.get("customer_person_id"))
            self._last_student_id = self.person.currentData()
            form.addRow("Заказчик в базе", self.customer_choice)
        self.extra: dict[str, QLineEdit | QTextEdit] = {}
        if kind == "outgoing" and read_only:
            form.addRow("Наш входящий №", QLineEdit(str(details.get("incoming_number") or "")))
            for key, label in (
                ("incoming_document_number", "№ письма отправителя"),
                ("incoming_document_date", "Дата письма отправителя"),
            ):
                form.addRow(label, QLineEdit(str(details.get(key) or "")))
        elif kind == "outgoing":
            self.incoming_records = {int(item["id"]): item for item in (incoming_records or [])}
            self.incoming_choice = QComboBox()
            self.incoming_choice.addItem("Без связи с входящим документом", None)
            for item in incoming_records or []:
                self.incoming_choice.addItem(
                    f"№ {item['registration_number']} · {item.get('counterparty') or ''}",
                    int(item["id"]),
                )
            saved_id = details.get("incoming_record_id")
            selected = self.incoming_choice.findData(
                int(saved_id) if saved_id and str(saved_id).isdigit() else None
            )
            if selected >= 0:
                self.incoming_choice.setCurrentIndex(selected)
            form.addRow("Ответ на входящий", self.incoming_choice)
            for key, label in (
                ("incoming_document_number", "№ письма отправителя"),
                ("incoming_document_date", "Дата письма отправителя"),
            ):
                field = QLineEdit(str(details.get(key) or ""))
                field.setReadOnly(True)
                form.addRow(label, field)
                self.extra[key] = field
            self.incoming_choice.currentIndexChanged.connect(self._incoming_changed)
            self._incoming_changed()
        if kind == "contract" and details.get("source_number"):
            form.addRow("№ в старом журнале", QLabel(str(details["source_number"])))
        if kind == "contract" and details.get("source_date"):
            form.addRow("Дата в старом журнале", QLabel(str(details["source_date"])))
        for key, label in FIELDS[kind]:
            if key in {"received_date", "reply_number", "executor"} and not record:
                continue
            if key in {"notes", "result", "amendments", "service_period"}:
                field = QTextEdit(str(details.get(key) or ""))
                field.setFixedHeight(68)
            else:
                field = QLineEdit(str(details.get(key) or ""))
            if key == "reply_number":
                field.setReadOnly(True)
            if key in {"received_date", "sent_date", "incoming_document_date", "due_date"}:
                field.setPlaceholderText("ДД.ММ.ГГГГ")
            form.addRow(label, field)
            self.extra[key] = field
        if kind == "contract" and not read_only:
            self._auto_student_name = ""
            self.person.currentIndexChanged.connect(self._person_changed)
            self.customer_choice.currentIndexChanged.connect(self._customer_changed)
        if kind == "contract":
            customer_row = form.takeRow(form.getWidgetPosition(self.customer_choice)[0])
            counterparty_row = form.takeRow(form.getWidgetPosition(self.counterparty)[0])
            form.insertRow(0, customer_row.labelItem.widget(), customer_row.fieldItem.widget())
            form.insertRow(
                1, counterparty_row.labelItem.widget(), counterparty_row.fieldItem.widget()
            )
        for field in content.findChildren(QTextEdit):
            field.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            field.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        if read_only:
            for field in content.findChildren(QLineEdit):
                field.setReadOnly(True)
            for field in content.findChildren(QTextEdit):
                field.setReadOnly(True)
            for field in content.findChildren(QComboBox):
                field.setEnabled(False)
            for field in content.findChildren(QListWidget):
                field.setEnabled(False)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
            if read_only
            else QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        if read_only:
            buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        else:
            buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
            buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> dict[str, Any]:
        raw_date = self.document_date.text().strip() if self.document_date is not None else ""
        document_date = (
            datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat() if raw_date else None
        )
        details = dict(self.record.get("details") or {})
        details.update({key: self._value(field) for key, field in self.extra.items()})
        if self.kind == "outgoing":
            selected_id = self.incoming_choice.currentData()
            details["incoming_record_id"] = str(selected_id) if selected_id else ""
            details["incoming_number"] = (
                self.incoming_records[int(selected_id)]["registration_number"]
                if selected_id
                else ""
            )
        if self.kind == "outgoing" and self.record:
            details["sent_date"] = self.sent_date.text().strip()
        if self.kind == "contract":
            details["subject_ids"] = ",".join(
                str(item.data(Qt.ItemDataRole.UserRole)) for item in self._selected_subjects()
            )
        result = {
            "document_date": document_date,
            "document_number": self.document_number.text().strip() or None
            if self.kind == "incoming"
            else None,
            "counterparty": self._value(self.counterparty),
            "subject": self._subject_value(),
            "person_id": self.person.currentData() if self.kind == "contract" else None,
            "customer_person_id": (
                self.customer_choice.currentData() if self.kind == "contract" else None
            ),
            "details": details,
        }
        if self.record:
            result["registration_number"] = self.registration_number.text().strip()
        return result

    @staticmethod
    def _value(field: QLineEdit | QTextEdit) -> str:
        return (field.text() if isinstance(field, QLineEdit) else field.toPlainText()).strip()

    def _incoming_changed(self) -> None:
        selected_id = self.incoming_choice.currentData()
        incoming = self.incoming_records.get(int(selected_id)) if selected_id else None
        self.extra["incoming_document_number"].setText(
            str(incoming.get("document_number") or "") if incoming else ""
        )
        document_date = incoming.get("document_date") if incoming else None
        self.extra["incoming_document_date"].setText(
            date.fromisoformat(document_date).strftime("%d.%m.%Y") if document_date else ""
        )

    def _filter_subjects(self, text: str) -> None:
        for index in range(self.subject_list.count()):
            item = self.subject_list.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())

    def _person_changed(self, _index: int) -> None:
        field = self.extra["student_name"]
        student_id = self.person.currentData()
        name = self.person.currentText() if student_id is not None else ""
        self.person.setToolTip(name)
        if name:
            field.setText(name)
            self._auto_student_name = name
        elif field.text().strip() == self._auto_student_name:
            field.clear()
            self._auto_student_name = ""
        customer_id = self.customer_choice.currentData()
        if customer_id is not None and customer_id == self._last_student_id:
            customer_id = student_id
        elif customer_id is None and student_id is not None:
            guardians = self.people_by_id[int(student_id)].get("guardians") or []
            if len(guardians) == 1 and int(guardians[0]["id"]) in self.people_by_id:
                customer_id = int(guardians[0]["id"])
        self._populate_customer_choices(customer_id)
        self._last_student_id = student_id
        if customer_id is not None:
            self._customer_changed(self.customer_choice.currentIndex())

    def _populate_customer_choices(self, preferred_id: int | None) -> None:
        student_id = self.person.currentData()
        student = self.people_by_id.get(int(student_id)) if student_id else None
        choice = self.customer_choice
        choice.blockSignals(True)
        choice.clear()
        choice.addItem("Ввести заказчика вручную", None)
        if student:
            choice.addItem(f"{student['full_name']} — ученик", int(student_id))
            choice.setItemData(
                choice.count() - 1, student["full_name"], Qt.ItemDataRole.ToolTipRole
            )
        linked_ids = (
            {int(person["id"]) for person in student.get("guardians", [])}
            if student else set()
        )
        parents = sorted(
            (self.people_by_id[person_id] for person_id in linked_ids
             if person_id in self.people_by_id),
            key=lambda person: person["full_name"].casefold(),
        )
        for parent in parents:
            choice.addItem(f"{parent['full_name']} — родитель", int(parent["id"]))
            choice.setItemData(choice.count() - 1, parent["full_name"], Qt.ItemDataRole.ToolTipRole)
        index = choice.findData(preferred_id)
        choice.setCurrentIndex(index if index >= 0 else 0)
        choice.blockSignals(False)

    def _customer_changed(self, _index: int) -> None:
        customer_id = self.customer_choice.currentData()
        self.customer_choice.setToolTip(self.customer_choice.currentText())
        customer = self.people_by_id.get(int(customer_id)) if customer_id else None
        if customer:
            self.counterparty.setPlainText(str(customer.get("full_name") or ""))

    def _selected_subjects(self) -> list[QListWidgetItem]:
        return [
            self.subject_list.item(index)
            for index in range(self.subject_list.count())
            if self.subject_list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _subject_value(self) -> str:
        if self.kind != "contract":
            return self._value(self.subject)
        selected = self._selected_subjects()
        return (
            ", ".join(item.text() for item in selected)
            if selected
            else str(self.record.get("subject") or "").strip()
        )

    def _validate_and_accept(self) -> None:
        if not self._value(self.counterparty) or not self._subject_value():
            QMessageBox.warning(self, "Не заполнены поля", "Укажите корреспондента и содержание.")
            return
        if self.kind == "contract" and not self.record and not self._selected_subjects():
            QMessageBox.warning(self, "Не выбран предмет", "Выберите предмет из справочника.")
            return
        if (
            not self.record
            and self.kind == "outgoing"
            and self.document_date is not None
            and not self.document_date.text().strip()
        ):
            QMessageBox.warning(self, "Не заполнена дата", "Укажите дату подписания документа.")
            return
        if self.kind == "incoming" and self.record:
            received = self._value(self.extra["received_date"])
            try:
                datetime.strptime(received, "%d.%m.%Y")
            except ValueError:
                QMessageBox.warning(self, "Неверная дата", "Укажите дату получения как ДД.ММ.ГГГГ.")
                return
        if self.kind == "outgoing" and self.record:
            try:
                datetime.strptime(self.sent_date.text().strip(), "%d.%m.%Y")
            except ValueError:
                QMessageBox.warning(self, "Неверная дата", "Укажите дату отправки как ДД.ММ.ГГГГ.")
                return
        try:
            self.payload()
        except ValueError:
            QMessageBox.warning(self, "Неверная дата", "Введите дату документа как ДД.ММ.ГГГГ.")
            return
        self.accept()


class RegistrationPage(QWidget):
    def __init__(
        self,
        api: ManagementApi,
        parent: QWidget | None = None,
        *,
        can_manage: bool = False,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.can_manage = can_manage
        self.people: list[dict[str, Any]] = []
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.visible_records: dict[str, list[dict[str, Any]]] = {}
        self.tables: dict[str, QTableWidget] = {}
        self.year_filters: dict[str, QComboBox] = {}
        self.search_fields: dict[str, QLineEdit] = {}
        self._year_initialized: set[str] = set()
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        for kind, label in JOURNALS:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            actions = QHBoxLayout()
            add = QPushButton("Зарегистрировать")
            add.clicked.connect(lambda _checked=False, key=kind: self._edit(key))
            actions.addWidget(add)
            edit = QPushButton("Исправить выбранное")
            edit.clicked.connect(lambda _checked=False, key=kind: self._edit(key, selected=True))
            if can_manage:
                actions.addWidget(edit)
                delete = QPushButton("Удалить выбранное")
                delete.clicked.connect(lambda _checked=False, key=kind: self._delete(key))
                actions.addWidget(delete)
            else:
                view = QPushButton("Просмотреть выбранное")
                view.clicked.connect(
                    lambda _checked=False, key=kind: self._edit(key, selected=True)
                )
                actions.addWidget(view)
            export = QPushButton("Экспорт в Word")
            export.clicked.connect(lambda _checked=False, key=kind: self._export(key))
            actions.addWidget(export)
            actions.addStretch(1)
            page_layout.addLayout(actions)
            filters = QHBoxLayout()
            filters.addWidget(QLabel("Год регистрации"))
            year_filter = QComboBox()
            year_filter.addItem("Все годы", None)
            year_filter.currentIndexChanged.connect(
                lambda _index, key=kind: self._apply_filters(key)
            )
            self.year_filters[kind] = year_filter
            filters.addWidget(year_filter)
            search = QLineEdit()
            search.setPlaceholderText("Поиск по номеру, корреспонденту или содержанию")
            search.setClearButtonEnabled(True)
            search.textChanged.connect(lambda _text, key=kind: self._apply_filters(key))
            self.search_fields[kind] = search
            filters.addWidget(search, 1)
            page_layout.addLayout(filters)
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["Рег. №", "Дата", "Корреспондент", "Содержание"])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setWordWrap(True)
            table.verticalHeader().hide()
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            table.setColumnWidth(0, 130)
            table.setColumnWidth(1, 100)
            table.setColumnWidth(2, 260)
            table.doubleClicked.connect(lambda _index, key=kind: self._edit(key, selected=True))
            self.tables[kind] = table
            page_layout.addWidget(table)
            self.tabs.addTab(page, label)
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        layout.addWidget(self.tabs)

    def set_people(self, people: list[dict[str, Any]]) -> None:
        self.people = people

    def refresh(self) -> None:
        kind = JOURNALS[self.tabs.currentIndex()][0]
        self._run(lambda: self.api.registration_records(kind), lambda rows: self._show(kind, rows))

    def _show(self, kind: str, rows: object) -> None:
        self.records[kind] = list(rows) if isinstance(rows, list) else []
        years = sorted(
            {year for record in self.records[kind] if (year := registration_year(record))},
            reverse=True,
        )
        year_filter = self.year_filters[kind]
        selected_year = year_filter.currentData()
        year_filter.blockSignals(True)
        year_filter.clear()
        year_filter.addItem("Все годы", None)
        for year in years:
            year_filter.addItem(str(year), year)
        if kind not in self._year_initialized and years:
            selected_year = years[0]
            self._year_initialized.add(kind)
        index = year_filter.findData(selected_year)
        year_filter.setCurrentIndex(index if index >= 0 else 0)
        year_filter.blockSignals(False)
        self._apply_filters(kind)

    def _apply_filters(self, kind: str) -> None:
        year = self.year_filters[kind].currentData()
        query = self.search_fields[kind].text().strip().casefold()

        def matches(record: dict[str, Any]) -> bool:
            if not query:
                return True
            number = str(record.get("registration_number") or "")
            if query.isdecimal():
                sequence = number.rsplit("/", 1)[-1] if "/" in number else ""
                return sequence.startswith(query)
            details = record.get("details") or {}
            return query in " ".join(
                str(value or "") for value in (
                    number, record.get("counterparty"), record.get("subject"),
                    details.get("student_name"),
                )
            ).casefold()

        records = [
            record
            for record in self.records.get(kind, [])
            if (year is None or registration_year(record) == year)
            and matches(record)
        ]
        self.visible_records[kind] = records
        table = self.tables[kind]
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            registered = (
                record.get("document_date")
                if kind == "contract"
                else record.get("registration_date")
            )
            display_date = date.fromisoformat(registered).strftime("%d.%m.%Y") if registered else ""
            for col, value in enumerate(
                (
                    f"№ {record.get('registration_number') or ''}",
                    display_date,
                    record.get("counterparty"),
                    record.get("subject"),
                )
            ):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                table.setItem(row, col, item)
        table.resizeRowsToContents()

    def _edit(self, kind: str, *, selected: bool = False) -> None:
        row = self.tables[kind].currentRow()
        records = self.visible_records.get(kind, [])
        if selected and not 0 <= row < len(records):
            QMessageBox.information(self, "Выберите запись", "Сначала выберите запись в журнале.")
            return
        record = records[row] if selected else None
        if record and not self.can_manage:
            self._open_editor(kind, record, [])
            return
        if kind == "outgoing":
            self._run(
                lambda: self.api.registration_records("incoming"),
                lambda incoming: self._open_editor(kind, record, incoming),
            )
            return
        if kind == "contract":
            self._run(
                self.api.learning_reference_data,
                lambda references: self._open_editor(
                    kind,
                    record,
                    [],
                    references.get("subjects", []) if isinstance(references, dict) else [],
                ),
            )
            return
        self._open_editor(kind, record, [])

    def _open_editor(
        self,
        kind: str,
        record: dict[str, Any] | None,
        incoming: object,
        subjects: object = None,
    ) -> None:
        dialog = RegistrationDialog(
            kind,
            self.people,
            record,
            self,
            incoming_records=incoming if isinstance(incoming, list) else [],
            subjects=subjects if isinstance(subjects, list) else [],
            read_only=bool(record and not self.can_manage),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if record and not self.can_manage:
            return
        payload = dialog.payload()
        if record:
            self._run(
                lambda: self.api.update_registration_record(record["id"], payload),
                lambda _result: self.refresh(),
            )
        else:
            self._run(
                lambda: self.api.create_registration_record(kind, payload),
                lambda _result: self.refresh(),
            )

    def _delete(self, kind: str) -> None:
        if not self.can_manage:
            return
        row = self.tables[kind].currentRow()
        records = self.visible_records.get(kind, [])
        if not 0 <= row < len(records):
            QMessageBox.information(self, "Выберите запись", "Сначала выберите запись в журнале.")
            return
        record = records[row]
        if (
            QMessageBox.question(
                self,
                "Удаление записи",
                f"Полностью удалить запись {record['registration_number']}? "
                "Отменить это действие нельзя.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            lambda: self.api.delete_registration_record(record["id"]),
            lambda _result: self.refresh(),
        )

    def _export(self, kind: str) -> None:
        selected_year = self.year_filters[kind].currentData()
        suffix = f" {selected_year}" if selected_year else ""
        suggested = f"{dict(JOURNALS)[kind]}{suffix}.docx"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Экспорт журнала", suggested, "Документ Word (*.docx)"
        )
        if path:
            self._run(
                lambda: export_journal(kind, self.visible_records.get(kind, []), path),
                lambda _result: QMessageBox.information(self, "Экспорт завершён", path),
            )

    def _run(self, fn, done) -> None:
        if self._closing:
            return
        worker = Worker(fn)
        self._workers.add(worker)

        def finished(result: object) -> None:
            self._workers.discard(worker)
            if not self._closing:
                done(result)

        def failed(message: str) -> None:
            self._workers.discard(worker)
            if not self._closing:
                QMessageBox.critical(self, "Ошибка регистрации", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def shutdown(self) -> None:
        self._closing = True
        self.pool.clear()
        self.pool.waitForDone(16_000)
        self._workers.clear()
