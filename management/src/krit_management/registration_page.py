"""Correspondence and contract registration journals."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from PySide6.QtCore import QDate, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .workers import Worker

JOURNALS = (
    ("incoming", "Входящая корреспонденция"),
    ("outgoing", "Исходящая корреспонденция"),
    ("contract", "Регистрация договоров"),
)
FIELDS = {
    "incoming": (
        ("received_date", "Дата получения"),
        ("channel", "Способ получения"),
        ("responsible", "Ответственный"),
        ("due_date", "Срок исполнения"),
        ("result", "Результат исполнения"),
        ("reply_number", "Ответ, исходящий №"),
        ("notes", "Примечание"),
    ),
    "outgoing": (
        ("sent_date", "Дата отправки"),
        ("channel", "Способ отправки"),
        ("signer", "Подписал"),
        ("responsible", "Исполнитель"),
        ("incoming_number", "В ответ на входящий №"),
        ("notes", "Примечание"),
    ),
    "contract": (
        ("student_name", "Ученик (ФИО)"),
        ("service_period", "Срок оказания услуг"),
        ("monthly_price", "Стоимость услуг в месяц"),
        ("amendments", "Соглашения об изменении цены"),
        ("source_number", "№ в исходном журнале"),
        ("notes", "Примечание"),
    ),
}


class RegistrationDialog(QDialog):
    def __init__(
        self, kind: str, people: list[dict[str, Any]], record: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Исправить запись" if record else "Зарегистрировать документ")
        self.setMinimumWidth(520)
        self.kind = kind
        self.record = record or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.registration_date = QDateEdit()
        self.registration_date.setCalendarPopup(True)
        self.registration_date.setDisplayFormat("dd.MM.yyyy")
        registered = self.record.get("registration_date")
        self.registration_date.setDate(
            QDate.fromString(registered, "yyyy-MM-dd") if registered else QDate.currentDate()
        )
        form.addRow("Дата регистрации", self.registration_date)
        self.document_date = QLineEdit()
        document_date = self.record.get("document_date")
        if document_date:
            self.document_date.setText(date.fromisoformat(document_date).strftime("%d.%m.%Y"))
        self.document_date.setPlaceholderText("ДД.ММ.ГГГГ")
        form.addRow("Дата документа", self.document_date)
        self.document_number = QLineEdit(str(self.record.get("document_number") or ""))
        if kind != "outgoing":
            form.addRow("Номер на документе", self.document_number)
        self.counterparty = QLineEdit(str(self.record.get("counterparty") or ""))
        form.addRow(
            {"incoming": "Отправитель", "outgoing": "Адресат", "contract": "Заказчик"}[kind],
            self.counterparty,
        )
        self.subject = QLineEdit(str(self.record.get("subject") or ""))
        form.addRow("Услуга" if kind == "contract" else "Краткое содержание", self.subject)
        self.person = QComboBox()
        self.person.addItem("Не связан с учеником", None)
        for person in people:
            if "student" in (person.get("roles") or []):
                self.person.addItem(str(person.get("full_name") or ""), person.get("id"))
        selected = self.person.findData(self.record.get("person_id"))
        if selected >= 0:
            self.person.setCurrentIndex(selected)
        if kind == "contract":
            form.addRow("Ученик в базе", self.person)
        self.extra: dict[str, QLineEdit] = {}
        details = self.record.get("details") or {}
        for key, label in FIELDS[kind]:
            field = QLineEdit(str(details.get(key) or ""))
            form.addRow(label, field)
            self.extra[key] = field
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> dict[str, Any]:
        raw_date = self.document_date.text().strip()
        document_date = (
            datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat() if raw_date else None
        )
        details = dict(self.record.get("details") or {})
        details.update({key: field.text().strip() for key, field in self.extra.items()})
        return {
            "registration_date": self.registration_date.date().toString("yyyy-MM-dd"),
            "document_date": document_date,
            "document_number": self.document_number.text().strip() or None,
            "counterparty": self.counterparty.text().strip(),
            "subject": self.subject.text().strip(),
            "person_id": self.person.currentData() if self.kind == "contract" else None,
            "details": details,
        }

    def _validate_and_accept(self) -> None:
        if not self.counterparty.text().strip() or not self.subject.text().strip():
            QMessageBox.warning(self, "Не заполнены поля", "Укажите корреспондента и содержание.")
            return
        try:
            self.payload()
        except ValueError:
            QMessageBox.warning(self, "Неверная дата", "Введите дату документа как ДД.ММ.ГГГГ.")
            return
        self.accept()


class RegistrationPage(QWidget):
    def __init__(self, api: ManagementApi, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.people: list[dict[str, Any]] = []
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.tables: dict[str, QTableWidget] = {}
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
            actions.addWidget(edit)
            actions.addStretch(1)
            page_layout.addLayout(actions)
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["Рег. №", "Дата", "Корреспондент", "Содержание"])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
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
        table = self.tables[kind]
        table.setRowCount(len(self.records[kind]))
        for row, record in enumerate(self.records[kind]):
            for col, value in enumerate((
                record.get("registration_number"), record.get("registration_date"),
                record.get("counterparty"), record.get("subject"),
            )):
                table.setItem(row, col, QTableWidgetItem(str(value or "")))

    def _edit(self, kind: str, *, selected: bool = False) -> None:
        row = self.tables[kind].currentRow()
        records = self.records.get(kind, [])
        if selected and not 0 <= row < len(records):
            QMessageBox.information(self, "Выберите запись", "Сначала выберите запись в журнале.")
            return
        record = records[row] if selected else None
        dialog = RegistrationDialog(kind, self.people, record, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
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
