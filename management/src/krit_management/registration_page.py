"""Correspondence and contract registration journals."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
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
        ("channel", "Способ отправки"),
        ("signer", "Подписал"),
        ("responsible", "Исполнитель"),
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
        self,
        kind: str,
        people: list[dict[str, Any]],
        record: dict[str, Any] | None = None,
        parent: QWidget | None = None,
        *,
        incoming_records: list[dict[str, Any]] | None = None,
        subjects: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Исправить запись" if record else "Зарегистрировать документ")
        self.setMinimumWidth(520)
        self.resize(680, 600)
        self.kind = kind
        self.record = record or {}
        layout = QVBoxLayout(self)
        content = QWidget()
        content.setObjectName("registrationForm")
        content.setStyleSheet(
            "QWidget#registrationForm { background-color: #f4f6fa; }"
            "QWidget#registrationForm QLabel { color: #10254c; background: transparent; }"
        )
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        registered = self.record.get("registration_date")
        date_label = (
            date.fromisoformat(registered).strftime("%d.%m.%Y")
            if registered
            else "Программа присвоит при сохранении"
        )
        if record and kind == "incoming":
            self.registration_date = QLineEdit(date_label)
            self.registration_date.setPlaceholderText("ДД.ММ.ГГГГ")
            form.addRow("Дата регистрации", self.registration_date)
        elif kind == "incoming":
            form.addRow("Дата регистрации", QLabel(date_label))
        if record:
            self.registration_number = QLineEdit(str(record["registration_number"]))
            form.addRow("Регистрационный №", self.registration_number)
        self.document_date = QLineEdit() if record or kind != "contract" else None
        document_date = self.record.get("document_date")
        if document_date and self.document_date is not None:
            self.document_date.setText(date.fromisoformat(document_date).strftime("%d.%m.%Y"))
        if self.document_date is not None:
            self.document_date.setPlaceholderText("ДД.ММ.ГГГГ")
        form.addRow(
            "Дата письма отправителя"
            if kind == "incoming"
            else "Дата договора и регистрации"
            if kind == "contract"
            else "Дата подписания",
            self.document_date if self.document_date is not None else QLabel(date_label),
        )
        if kind == "outgoing":
            if record:
                self.sent_date = QLineEdit(date_label)
                self.sent_date.setPlaceholderText("ДД.ММ.ГГГГ")
                form.addRow("Дата отправки и регистрации", self.sent_date)
            else:
                form.addRow("Дата отправки и регистрации", QLabel(date_label))
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
        if kind == "contract":
            subject_holder = QWidget()
            subject_layout = QVBoxLayout(subject_holder)
            subject_layout.setContentsMargins(0, 0, 0, 0)
            self.subject_search = QLineEdit()
            self.subject_search.setPlaceholderText("Поиск предмета…")
            self.subject_search.setClearButtonEnabled(True)
            self.subject_list = QListWidget()
            self.subject_list.setMinimumHeight(135)
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
        self.person = QComboBox()
        self.person.setEditable(True)
        self.person.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.person.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.person.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.person.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
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
        if kind == "outgoing":
            self.incoming_records = {int(item["id"]): item for item in (incoming_records or [])}
            self.incoming_choice = QComboBox()
            self.incoming_choice.addItem("Не связан с входящим документом", None)
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
            form.addRow("Наш входящий №", self.incoming_choice)
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
            "details": details,
        }
        if self.record:
            result["registration_number"] = self.registration_number.text().strip()
            if self.kind == "incoming":
                result["registration_date"] = (
                    datetime.strptime(self.registration_date.text().strip(), "%d.%m.%Y")
                    .date()
                    .isoformat()
                )
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
        if self.kind == "incoming":
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
            registered = (
                record.get("document_date") if kind == "contract"
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
        if selected and not self.can_manage:
            return
        row = self.tables[kind].currentRow()
        records = self.records.get(kind, [])
        if selected and not 0 <= row < len(records):
            QMessageBox.information(self, "Выберите запись", "Сначала выберите запись в журнале.")
            return
        record = records[row] if selected else None
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
        )
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
