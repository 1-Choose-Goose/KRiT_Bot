from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

from krit_management.registration_page import RegistrationDialog, RegistrationPage


def test_registration_edit_and_delete_controls_are_superadmin_only() -> None:
    _app = QApplication.instance() or QApplication([])
    ordinary = RegistrationPage(object(), can_manage=False)
    elevated = RegistrationPage(object(), can_manage=True)
    try:
        ordinary_labels = {button.text() for button in ordinary.findChildren(QPushButton)}
        elevated_labels = {button.text() for button in elevated.findChildren(QPushButton)}
        assert "Зарегистрировать" in ordinary_labels
        assert "Экспорт в Word" in ordinary_labels
        assert "Просмотреть выбранное" in ordinary_labels
        assert "Исправить выбранное" not in ordinary_labels
        assert "Удалить выбранное" not in ordinary_labels
        assert {"Исправить выбранное", "Удалить выбранное"} <= elevated_labels
        assert all(table.wordWrap() for table in elevated.tables.values())
        assert all(table.verticalHeader().isHidden() for table in elevated.tables.values())
    finally:
        ordinary.shutdown()
        elevated.shutdown()
        ordinary.close()
        elevated.close()


def test_contract_uses_one_visible_date_and_readable_form_labels() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog(
        "contract",
        [],
        {
            "id": 1,
            "registration_number": "дг-2026/1",
            "registration_date": "2026-11-21",
            "document_date": "2026-08-03",
            "counterparty": "Заказчик",
            "subject": "Услуга",
            "details": {},
        },
    )
    try:
        labels = {label.text() for label in dialog.findChildren(QLabel)}
        assert "Дата договора и регистрации" in labels
        assert "Дата регистрации" not in labels
        form = dialog.findChild(QWidget, "registrationForm")
        assert form is not None
        assert "background-color: #f4f6fa" in form.styleSheet()
        assert "color: #10254c" in form.styleSheet()
        assert dialog.payload()["document_date"] == "2026-08-03"
        assert "registration_date" not in dialog.payload()
    finally:
        dialog.close()


def test_registered_card_is_read_only_for_administrator_and_director() -> None:
    _app = QApplication.instance() or QApplication([])
    record = {
        "id": 8,
        "registration_number": "вх-2026/8",
        "registration_date": "2026-11-21",
        "document_date": "2026-11-20",
        "counterparty": "Отправитель",
        "subject": "Содержание",
        "document_number": "18",
        "details": {"received_date": "21.11.2026"},
    }
    dialog = RegistrationDialog("incoming", [], record, read_only=True)
    try:
        assert dialog.windowTitle() == "Просмотр записи"
        assert all(field.isReadOnly() for field in dialog.findChildren(QLineEdit))
        assert all(field.isReadOnly() for field in dialog.findChildren(QTextEdit))
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(QDialogButtonBox.StandardButton.Close) is not None
        assert buttons.button(QDialogButtonBox.StandardButton.Save) is None
    finally:
        dialog.close()


def test_outgoing_creation_omits_automatic_date_placeholder() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog("outgoing", [])
    try:
        labels = {label.text() for label in dialog.findChildren(QLabel)}
        assert "Дата отправки и регистрации" not in labels
        assert "Дата подписания" in labels
    finally:
        dialog.close()


def test_incoming_date_and_executor_are_assigned_on_registration() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog("incoming", [])
    try:
        labels = {label.text() for label in dialog.findChildren(QLabel)}
        assert "Дата получения и регистрации" not in labels
        assert "Исполнитель" not in labels
        assert "received_date" not in dialog.payload()["details"]
    finally:
        dialog.close()


def test_year_archive_and_search_keep_correct_record_selection() -> None:
    _app = QApplication.instance() or QApplication([])
    page = RegistrationPage(object(), can_manage=False)
    try:
        rows = [
            {
                "id": 1,
                "registration_number": "вх-2025/1",
                "registration_date": "2025-03-01",
                "counterparty": "Старый отправитель",
                "subject": "Письмо",
            },
            {
                "id": 2,
                "registration_number": "вх-2026/6",
                "registration_date": "2026-01-02",
                "counterparty": "Новый отправитель",
                "subject": "Запрос",
            },
        ]
        page._show("incoming", rows)
        assert page.year_filters["incoming"].currentData() == 2026
        assert [item["id"] for item in page.visible_records["incoming"]] == [2]
        page.year_filters["incoming"].setCurrentIndex(0)
        assert page.tables["incoming"].rowCount() == 2
        page.search_fields["incoming"].setText("старый")
        assert [item["id"] for item in page.visible_records["incoming"]] == [1]
        page._show("incoming", rows)
        assert page.year_filters["incoming"].currentData() is None
        assert [item["id"] for item in page.visible_records["incoming"]] == [1]
        page.search_fields["incoming"].setText("6")
        assert page.tables["incoming"].rowCount() == 1
        assert [item["id"] for item in page.visible_records["incoming"]] == [2]
    finally:
        page.shutdown()
        page.close()


def test_non_superadmin_opens_selected_card_without_loading_references() -> None:
    _app = QApplication.instance() or QApplication([])
    page = RegistrationPage(object(), can_manage=False)
    try:
        record = {"id": 3, "registration_number": "дг-2026/3",
                  "registration_date": "2026-11-21", "counterparty": "Заказчик",
                  "subject": "Математика"}
        page._show("contract", [record])
        page.tables["contract"].setCurrentCell(0, 0)
        opened = []
        page._open_editor = lambda kind, item, incoming: opened.append((kind, item))
        page._edit("contract", selected=True)
        assert opened == [("contract", record)]
    finally:
        page.shutdown()
        page.close()


def test_read_only_outgoing_card_displays_saved_incoming_reference() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog(
        "outgoing", [],
        {"id": 4, "registration_number": "исх-2026/4",
         "registration_date": "2026-11-21", "document_date": "2026-11-21",
         "counterparty": "Адресат", "subject": "Ответ",
         "details": {"incoming_number": "вх-2026/8",
                     "incoming_document_number": "18",
                     "incoming_document_date": "20.11.2026"}},
        read_only=True,
    )
    try:
        values = [field.text() for field in dialog.findChildren(QLineEdit)]
        assert "вх-2026/8" in values
        assert "18" in values
        assert "20.11.2026" in values
    finally:
        dialog.close()


def test_contract_selects_multiple_subjects_and_finds_student_by_surname() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog(
        "contract",
        [
            {"id": 7, "full_name": "Акуляева Ирина", "roles": ["student"],
             "guardians": [{"id": 8, "full_name": "Акуляева Анна"}]},
            {"id": 8, "full_name": "Акуляева Анна", "roles": ["parent"]},
        ],
        subjects=[
            {"id": 2, "name": "Математика", "active": True},
            {"id": 3, "name": "Химия", "active": True},
        ],
    )
    try:
        dialog.counterparty.setPlainText("Заказчик")
        dialog.subject_search.setText("хим")
        assert dialog.subject_list.item(0).isHidden()
        assert not dialog.subject_list.item(1).isHidden()
        dialog.subject_list.item(0).setCheckState(Qt.CheckState.Checked)
        dialog.subject_list.item(1).setCheckState(Qt.CheckState.Checked)
        assert "#1b63db" in dialog.subject_list.styleSheet()
        dialog.person._search("Аку")
        assert dialog.person._proxy.rowCount() == 1
        assert "background: white" in dialog.person.completer().popup().styleSheet()
        dialog.person.setCurrentIndex(1)
        assert dialog.extra["student_name"].text() == "Акуляева Ирина"
        parent_index = dialog.customer_choice.findData(8)
        assert "Родитель ученика" in dialog.customer_choice.itemText(parent_index)
        dialog.customer_choice.setCurrentIndex(parent_index)
        assert dialog.counterparty.toPlainText() == "Акуляева Анна"
        assert dialog.payload()["customer_person_id"] == 8
        dialog.customer_choice.setCurrentIndex(dialog.customer_choice.findData(7))
        assert dialog.counterparty.toPlainText() == "Акуляева Ирина"
        assert dialog.payload()["customer_person_id"] == 7
        assert dialog.payload()["subject"] == "Математика, Химия"
        assert dialog.payload()["details"]["subject_ids"] == "2,3"
    finally:
        dialog.close()
