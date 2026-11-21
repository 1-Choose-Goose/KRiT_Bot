from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

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
        assert "Исправить выбранное" not in ordinary_labels
        assert "Удалить выбранное" not in ordinary_labels
        assert {"Исправить выбранное", "Удалить выбранное"} <= elevated_labels
        assert all(table.wordWrap() for table in elevated.tables.values())
    finally:
        ordinary.shutdown()
        elevated.shutdown()
        ordinary.close()
        elevated.close()


def test_contract_uses_one_visible_date_and_readable_form_labels() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog("contract", [], {
        "id": 1, "registration_number": "дг-2026/1",
        "registration_date": "2026-11-21", "document_date": "2026-08-03",
        "counterparty": "Заказчик", "subject": "Услуга", "details": {},
    })
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


def test_contract_selects_multiple_subjects_and_finds_student_by_surname() -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = RegistrationDialog(
        "contract",
        [{"id": 7, "full_name": "Акуляева Ирина", "roles": ["student"]}],
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
        dialog.person.setEditText("Аку")
        assert dialog.person.completer().filterMode() == Qt.MatchFlag.MatchContains
        assert dialog.payload()["subject"] == "Математика, Химия"
        assert dialog.payload()["details"]["subject_ids"] == "2,3"
    finally:
        dialog.close()
