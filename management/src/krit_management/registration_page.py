"""Correspondence and contract registration journals."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from PySide6.QtCore import QThreadPool
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
        ("incoming_number", "Наш входящий №"),
        ("incoming_document_number", "№ письма отправителя"),
        ("incoming_document_date", "Дата письма отправителя"),
        ("notes", "Примечание"),
    ),
    "contract": (
        ("student_name", "Ученик (ФИО)"),
        ("service_period", "Срок оказания услуг"),
        ("monthly_price", "Стоимость услуг в месяц"),
        ("amendments", "Соглашения об изменении цены"),
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
        self.resize(680, 600)
        self.kind = kind
        self.record = record or {}
        layout = QVBoxLayout(self)
        content = QWidget()
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        registered = self.record.get("registration_date")
        date_label = (
            date.fromisoformat(registered).strftime("%d.%m.%Y")
            if registered else "Программа присвоит при сохранении"
        )
        if record:
            self.registration_date = QLineEdit(date_label)
            self.registration_date.setPlaceholderText("ДД.ММ.ГГГГ")
            form.addRow("Дата регистрации", self.registration_date)
            self.registration_number = QLineEdit(str(record["registration_number"]))
            form.addRow("Регистрационный №", self.registration_number)
        else:
            form.addRow("Дата регистрации", QLabel(date_label))
        self.document_date = QLineEdit()
        document_date = self.record.get("document_date")
        if document_date:
            self.document_date.setText(date.fromisoformat(document_date).strftime("%d.%m.%Y"))
        self.document_date.setPlaceholderText("ДД.ММ.ГГГГ")
        form.addRow(
            "Дата письма отправителя" if kind == "incoming" else "Дата подписания",
            self.document_date,
        )
        self.document_number = QLineEdit(str(self.record.get("document_number") or ""))
        if kind == "incoming":
            form.addRow("Номер письма отправителя", self.document_number)
        self.counterparty = QTextEdit(str(self.record.get("counterparty") or ""))
        self.counterparty.setFixedHeight(64)
        form.addRow(
            {"incoming": "Отправитель", "outgoing": "Адресат", "contract": "Заказчик"}[kind],
            self.counterparty,
        )
        self.subject = QTextEdit(str(self.record.get("subject") or ""))
        self.subject.setFixedHeight(80)
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
        self.extra: dict[str, QLineEdit | QTextEdit] = {}
        details = self.record.get("details") or {}
        if kind == "contract" and details.get("source_number"):
            form.addRow("№ в старом журнале", QLabel(str(details["source_number"])))
        if kind == "contract" and details.get("source_date"):
            form.addRow("Дата в старом журнале", QLabel(str(details["source_date"])))
        for key, label in FIELDS[kind]:
            if key in {"notes", "result", "amendments", "service_period"}:
                field = QTextEdit(str(details.get(key) or ""))
                field.setFixedHeight(68)
            else:
                field = QLineEdit(str(details.get(key) or ""))
            if key in {"received_date", "sent_date", "incoming_document_date", "due_date"}:
                field.setPlaceholderText("ДД.ММ.ГГГГ")
            form.addRow(label, field)
            self.extra[key] = field
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
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
        details.update({key: self._value(field) for key, field in self.extra.items()})
        result = {
            "document_date": document_date,
            "document_number": self.document_number.text().strip() or None
            if self.kind == "incoming" else None,
            "counterparty": self._value(self.counterparty),
            "subject": self._value(self.subject),
            "person_id": self.person.currentData() if self.kind == "contract" else None,
            "details": details,
        }
        if self.record:
            result["registration_number"] = self.registration_number.text().strip()
            result["registration_date"] = datetime.strptime(
                self.registration_date.text().strip(), "%d.%m.%Y"
            ).date().isoformat()
        return result

    @staticmethod
    def _value(field: QLineEdit | QTextEdit) -> str:
        return (field.text() if isinstance(field, QLineEdit) else field.toPlainText()).strip()

    def _validate_and_accept(self) -> None:
        if not self._value(self.counterparty) or not self._value(self.subject):
            QMessageBox.warning(self, "Не заполнены поля", "Укажите корреспондента и содержание.")
            return
        if not self.record and self.kind != "incoming" and not self.document_date.text().strip():
            QMessageBox.warning(self, "Не заполнена дата", "Укажите дату подписания документа.")
            return
        if self.kind == "incoming":
            received = self._value(self.extra["received_date"])
            try:
                datetime.strptime(received, "%d.%m.%Y")
            except ValueError:
                QMessageBox.warning(self, "Неверная дата", "Укажите дату получения как ДД.ММ.ГГГГ.")
                return
        try:
            self.payload()
        except ValueError:
            QMessageBox.warning(self, "Неверная дата", "Введите дату документа как ДД.ММ.ГГГГ.")
            return
        self.accept()


class RegistrationPage(QWidget):
    def __init__(
        self, api: ManagementApi, parent: QWidget | None = None, *, can_manage: bool = False,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.can_manage = can_manage
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
            if can_manage:
                actions.addWidget(edit)
                delete = QPushButton("Удалить выбранное")
                delete.clicked.connect(lambda _checked=False, key=kind: self._delete(key))
                actions.addWidget(delete)
            export = QPushButton("Экспорт в Word")
            export.clicked.connect(lambda _checked=False, key=kind: self._export(key))
            actions.addWidget(export)
            actions.addStretch(1)
            page_layout.addLayout(actions)
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["Рег. №", "Дата", "Корреспондент", "Содержание"])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setWordWrap(True)
            table.verticalHeader().setMinimumSectionSize(44)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            table.setColumnWidth(0, 130)
            table.setColumnWidth(1, 100)
            table.setColumnWidth(2, 260)
            if can_manage:
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
            registered = record.get("registration_date")
            display_date = date.fromisoformat(registered).strftime("%d.%m.%Y") if registered else ""
            for col, value in enumerate((
                f"№ {record.get('registration_number') or ''}", display_date,
                record.get("counterparty"), record.get("subject"),
            )):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                table.setItem(row, col, item)
        table.resizeRowsToContents()

    def _edit(self, kind: str, *, selected: bool = False) -> None:
        if selected and not self.can_manage:
            return
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

    def _delete(self, kind: str) -> None:
        if not self.can_manage:
            return
        row = self.tables[kind].currentRow()
        records = self.records.get(kind, [])
        if not 0 <= row < len(records):
            QMessageBox.information(self, "Выберите запись", "Сначала выберите запись в журнале.")
            return
        record = records[row]
        if QMessageBox.question(
            self, "Удаление записи",
            f"Полностью удалить запись {record['registration_number']}? "
            "Отменить это действие нельзя.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.api.delete_registration_record(record["id"]),
                  lambda _result: self.refresh())

    def _export(self, kind: str) -> None:
        suggested = f"{dict(JOURNALS)[kind]}.docx"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Экспорт журнала", suggested, "Документ Word (*.docx)"
        )
        if path:
            self._run(
                lambda: export_journal(kind, self.api.registration_records(kind), path),
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
