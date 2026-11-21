from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from krit_management.registration_page import RegistrationPage


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
